"""运行配置: 命令行参数 + .env 环境变量."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # API (优先级: 命令行 > .env > 默认值)
    api_key: str = field(default_factory=lambda: os.environ.get("ARK_API_KEY", ""))
    base_url: str = field(
        default_factory=lambda: os.environ.get(
            "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"
        )
    )
    model: str = field(default_factory=lambda: os.environ.get("ARK_MODEL", "glm-5.3-flash"))

    # 流程参数
    out_dir: Path = Path("out")
    top: int = 12            # 最终输出写真张数
    interval: float = 0.5    # 粗采样间隔 (秒)
    refine_k: int = 15       # 进入峰值精修的时刻数
    refine_span: float = 0.6 # 精修窗口半径 (秒)
    refine_step: float = 0.1 # 精修采样步长 (秒)
    min_score: float = 6.0   # 精修段候选帧最低 overall 分
    upload_long_edge: int = 768  # 送 LLM 评分的图片长边像素
    workers: int = 6         # API 并发数
    enhance: bool = False    # 输出前做 autocontrast+锐化
    png: bool = False        # 成品用 PNG 而非 JPEG

    def to_dict(self) -> dict:
        d = asdict(self)
        d["out_dir"] = str(self.out_dir)
        return d
