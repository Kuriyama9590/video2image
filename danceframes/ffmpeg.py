"""ffmpeg/ffprobe 封装: 探测 / 采样抽帧(带缩放) / 精确导出.

本地只做编解码, 不做任何图像质量计算.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"命令失败({p.returncode}): {' '.join(cmd)}\n{p.stderr[:2000]}")
    return p.stdout


@dataclass
class VideoInfo:
    duration: float
    width: int   # 显示尺寸 (已考虑旋转元数据)
    height: int
    fps: float
    portrait: bool


def probe(video: Path) -> VideoInfo:
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,side_data_list:format=duration",
        "-of", "json", str(video),
    ])
    data = json.loads(out)
    st = data["streams"][0]
    w, h = int(st["width"]), int(st["height"])

    rotation = 0
    for sd in st.get("side_data_list") or []:
        rotation = int(sd.get("rotation") or 0)
    if abs(rotation) in (90, 270):
        w, h = h, w

    num, den = st["avg_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return VideoInfo(float(data["format"]["duration"]), w, h, fps, h >= w)


# 长边缩放到 long_edge, 短边自动等比 (保持偶数避免某些像素格式问题)
_SCALE = "scale='if(gt(iw,ih),-2,{L})':'if(gt(iw,ih),{L},-2)'"


def _extract_timed(video: Path, out_dir: Path, pattern: str, start: float,
                   duration: float | None, fps: float, long_edge: int,
                   jpeg_q: int) -> list[Path]:
    """按指定 fps 抽帧并缩放, 返回按序帧文件列表."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(pattern.replace("%05d", "*").replace("%04d", "*").replace("%03d", "*")):
        old.unlink()
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vf", f"fps={fps},{_SCALE.format(L=long_edge)}",
            "-q:v", str(jpeg_q), str(out_dir / pattern)]
    _run(cmd)
    return sorted(out_dir.glob(pattern.replace("%05d", "*").replace("%04d", "*").replace("%03d", "*")))


def sample_uniform(video: Path, out_dir: Path, interval: float,
                   long_edge: int) -> list[tuple[float, Path]]:
    """整段均匀采样: 第 i 帧 (0-based) 对应时间 i*interval."""
    frames = _extract_timed(video, out_dir, "f_%05d.jpg", 0.0, None,
                            1.0 / interval, long_edge, 4)
    return [(i * interval, p) for i, p in enumerate(frames)]


def sample_window(video: Path, out_dir: Path, start: float, duration: float,
                  step: float, long_edge: int, tag: str) -> list[tuple[float, Path]]:
    """窗口内高密度采样: 第 i 帧对应时间 start + i*step."""
    frames = _extract_timed(video, out_dir, f"{tag}_%04d.jpg", start, duration,
                            1.0 / step, long_edge, 4)
    return [(start + i * step, p) for i, p in enumerate(frames)]


def export_frame(video: Path, ts: float, dst: Path, png: bool) -> None:
    """按时间戳精确导出原始分辨率的单帧成品."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ts:.3f}", "-i", str(video),
           "-frames:v", "1"]
    if not png:
        cmd += ["-q:v", "2"]
    cmd.append(str(dst))
    _run(cmd)
