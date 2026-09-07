"""CLI 入口: python -m danceframes <video...> [选项]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import pipeline
from .config import Settings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="danceframes",
        description="竖屏舞蹈视频 -> 精选写真: LLM 密集评分 + 峰值精修 + 全局精选",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("videos", nargs="+", type=Path, help="输入视频文件 (可多个)")
    p.add_argument("-o", "--out", type=Path, default=Path("out"), help="输出根目录")
    p.add_argument("--top", type=int, default=12, help="最终输出写真张数")
    p.add_argument("--interval", type=float, default=0.5, help="粗采样间隔(秒)")
    p.add_argument("--refine-k", type=int, default=15, help="进入峰值精修的时刻数")
    p.add_argument("--refine-span", type=float, default=0.6, help="精修窗口半径(秒)")
    p.add_argument("--refine-step", type=float, default=0.1, help="精修采样步长(秒)")
    p.add_argument("--min-score", type=float, default=6.0, help="精修段候选最低overall分")
    p.add_argument("--workers", type=int, default=6, help="API 并发数")
    p.add_argument("--upload-long-edge", type=int, default=768,
                   help="送LLM评分的图片长边像素")
    p.add_argument("--enhance", action="store_true", help="额外输出增强版 (对比度+锐化)")
    p.add_argument("--png", action="store_true", help="成品用 PNG 而非 JPEG")
    p.add_argument("--api-key", help="覆盖 ARK_API_KEY")
    p.add_argument("--base-url", help="覆盖 ARK_BASE_URL")
    p.add_argument("--model", help="覆盖 ARK_MODEL")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    s = Settings(
        out_dir=args.out, top=args.top, interval=args.interval,
        refine_k=args.refine_k, refine_span=args.refine_span,
        refine_step=args.refine_step, min_score=args.min_score,
        upload_long_edge=args.upload_long_edge, workers=args.workers,
        enhance=args.enhance, png=args.png,
    )
    if args.api_key:
        s.api_key = args.api_key
    if args.base_url:
        s.base_url = args.base_url
    if args.model:
        s.model = args.model

    failed = 0
    for i, video in enumerate(args.videos):
        if len(args.videos) > 1:
            print(f"({i + 1}/{len(args.videos)}) {video}")
        try:
            r = pipeline.run(video, s)
            if not r.ok:
                print(f"  失败: {r.error}", file=sys.stderr)
                failed += 1
        except Exception as e:
            print(f"  失败(未预期异常): {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
