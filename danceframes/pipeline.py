"""流程编排: 探测 -> 均匀采样 -> LLM密集评分 -> 峰值精修 -> LLM全局精选 -> 导出."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg
from .config import Settings
from .enhance import enhance_image, make_grid
from .llm import ArkClient, global_select, score_frames_concurrent


def _fmt_ts(ts: float) -> str:
    m, s = divmod(ts, 60)
    return f"{int(m):02d}:{s:04.1f}"


def _log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class RunResult:
    video: Path
    ok: bool
    finals: list[dict] = field(default_factory=list)
    error: str = ""
    stats: dict = field(default_factory=dict)


def run(video: Path, s: Settings) -> RunResult:
    t0 = time.time()
    video = video.resolve()
    if not video.exists():
        return RunResult(video, False, error=f"文件不存在: {video}")

    out_root = s.out_dir / video.stem
    work = out_root / "work"
    finals_dir = out_root / "finals"

    client = ArkClient(s)

    # ① 探测
    info = ffmpeg.probe(video)
    orient = "竖屏" if info.portrait else "横屏(仍继续处理)"
    _log(f"  [1/6] 探测: {info.duration:.1f}s {info.width}x{info.height} {orient} @ {info.fps:.1f}fps")

    # ② 均匀采样
    coarse = ffmpeg.sample_uniform(video, work / "coarse", s.interval, s.upload_long_edge)
    _log(f"  [2/6] 均匀采样 interval={s.interval}s -> {len(coarse)} 帧")

    # ③ LLM 密集评分
    _log(f"  [3/6] LLM 密集评分 ({len(coarse)} 帧, {s.workers} 并发) ...")
    coarse_scores = score_frames_concurrent(client, coarse, refine=False)
    scored = sum(1 for r in coarse_scores if r)
    _log(f"        完成 {scored}/{len(coarse)}")

    # (ts, file, scores) 按 overall 降序
    ranked = sorted(
        ((ts, p, r) for (ts, p), r in zip(coarse, coarse_scores) if r),
        key=lambda x: x[2]["overall"], reverse=True,
    )
    if not ranked:
        return _finish(RunResult(video, False,
                      error="密集评分全部失败, 请检查 API 配置/额度"),
                       client, t0)

    # ④ 峰值精修: 互不重叠的 Top-K 时刻, 窗口内高密度重抽 + 二次评分
    moments: list[float] = []
    for ts, _p, _r in ranked:
        if all(abs(ts - t) > s.interval for t in moments):
            moments.append(ts)
        if len(moments) >= s.refine_k:
            break

    candidates: list[dict] = []
    refine_report: list[dict] = []
    refine_frame_total = 0
    refine_scored_total = 0
    span_dur = 2 * s.refine_span
    for k, center in enumerate(sorted(moments)):
        start = max(0.0, min(center - s.refine_span, info.duration - span_dur))
        window = ffmpeg.sample_window(video, work / "refine", start, span_dur,
                                      s.refine_step, s.upload_long_edge, f"w{k:02d}")
        refine_frame_total += len(window)
        w_scores = score_frames_concurrent(client, window, refine=True)
        refine_scored_total += sum(1 for r in w_scores if r)

        w_ranked = [(ts, p, r) for (ts, p), r in zip(window, w_scores) if r]
        w_report = {"center": round(center, 2), "window_start": round(start, 2),
                    "frames": len(window), "scored": len(w_ranked), "kept": False}
        if w_ranked:
            best_ts, best_file, best = max(
                w_ranked, key=lambda x: (x[2]["overall"], x[2]["clarity"]))
            w_report.update(best_ts=round(best_ts, 2),
                            best_overall=best["overall"])
            if best["overall"] >= s.min_score:
                w_report["kept"] = True
                candidates.append({"ts": best_ts, "file": str(best_file),
                                   **best, "from": "refine"})
        refine_report.append(w_report)
    kept = sum(1 for r in refine_report if r["kept"])
    _log(f"  [4/6] 峰值精修 {len(moments)} 个时刻 (±{s.refine_span}s/{s.refine_step}s) "
         f"-> {refine_frame_total} 帧二次评分, {kept} 个时刻达标 (min_score={s.min_score})")

    # 候选不足 top 时从粗评分回填 (忽略 min_score, 保证有产出)
    for ts, p, r in ranked:
        if len(candidates) >= s.top:
            break
        if any(abs(ts - c["ts"]) < s.refine_span for c in candidates):
            continue
        candidates.append({"ts": ts, "file": str(p), **r, "from": "coarse"})

    candidates.sort(key=lambda c: c["ts"])
    for i, c in enumerate(candidates):
        c["id"] = i

    # ⑤ LLM 全局精选 (跨时刻去重 + 多样性)
    sel_dir = work / "select"
    sel_dir.mkdir(parents=True, exist_ok=True)
    grid_items = []
    for c in candidates:
        dst = sel_dir / f"c{c['id']:02d}.jpg"
        dst.write_bytes(Path(c["file"]).read_bytes())
        grid_items.append((f"#{c['id']} {_fmt_ts(c['ts'])}", dst))
    grid_path = make_grid(grid_items, work / "select_grid.jpg",
                          cols=min(4, max(1, len(candidates))))
    _log(f"  [5/6] LLM 全局精选 ({len(candidates)} 个候选 -> 选 {s.top}) ...")
    selection = global_select(client, grid_path, candidates, s.top)
    _log(f"        模型选择 {len(selection['selected'])} 帧: {selection['selected']}")

    if selection["selected"]:
        chosen_ids = selection["selected"]
        fallback = False
    else:  # 全局精选失败时按 overall 回退
        chosen_ids = [c["id"] for c in
                      sorted(candidates, key=lambda c: c["overall"], reverse=True)[:s.top]]
        fallback = True

    # ⑥ 导出成品 (原始分辨率精确 seek)
    finals_dir.mkdir(parents=True, exist_ok=True)
    ext = "png" if s.png else "jpg"
    by_id = {c["id"]: c for c in candidates}
    final_entries: list[dict] = []
    for rank, cid in enumerate(chosen_ids, 1):
        c = by_id[cid]
        name = f"f{rank:02d}_t{c['ts']:.2f}s.{ext}"
        dst = finals_dir / name
        ffmpeg.export_frame(video, c["ts"], dst, s.png)
        entry = {"rank": rank, "id": cid, "ts": round(c["ts"], 2),
                 "overall": c["overall"], "reason": c["reason"], "file": str(dst)}
        if s.enhance:
            try:
                enh = out_root / "finals_enhanced" / name
                enhance_image(dst, enh)
                entry["enhanced"] = str(enh)
            except Exception as e:  # 增强失败不影响成品
                entry["enhance_error"] = str(e)
        final_entries.append(entry)
        _log(f"        f{rank:02d}  t={c['ts']:.2f}s  overall={c['overall']:.0f}  {c['reason']}")
    _log(f"  [6/6] 导出 {len(chosen_ids)} 张成品 -> {finals_dir}"
         + (" [回退: 全局精选未生效, 按分数排序]" if fallback else ""))

    # 成品总览拼图 + 报告
    try:
        make_grid([(f"f{e['rank']:02d} {_fmt_ts(e['ts'])}", Path(e["file"]))
                   for e in final_entries],
                  out_root / "contact_sheet.jpg", cols=4)
    except Exception:
        pass

    report = {
        "video": str(video),
        "video_info": {"duration": info.duration, "width": info.width,
                       "height": info.height, "fps": round(info.fps, 3),
                       "portrait": info.portrait},
        "settings": s.to_dict(),
        "stats": {"coarse_frames": len(coarse), "coarse_scored": scored,
                  "refine_windows": len(moments), "refine_frames": refine_frame_total,
                  "refine_scored": refine_scored_total, "candidates": len(candidates),
                  "api_calls": client.calls, "api_failures": client.failures},
        "coarse_scores": [{"ts": round(ts, 2), "file": str(p), **r}
                          for (ts, p), r in zip(coarse, coarse_scores) if r],
        "refine_windows_detail": refine_report,
        "candidates": candidates,
        "selection": selection,
    }
    (out_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    result = RunResult(video, True)
    result.finals = final_entries
    return _finish(result, client, t0)


def _finish(result: RunResult, client: ArkClient, t0: float) -> RunResult:
    result.stats = {"api_calls": client.calls, "api_failures": client.failures,
                    "elapsed": round(time.time() - t0, 1)}
    _log(f"  完成: {client.calls} 次 API 调用, 失败 {client.failures}, "
         f"耗时 {result.stats['elapsed']:.0f}s")
    return result
