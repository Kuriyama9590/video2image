# danceframes — 竖屏舞蹈视频 → 精选写真

一站式 CLI 工具：输入竖屏舞蹈视频，自动完成精彩镜头定位、动作峰值捕捉、
跨段落去重，输出高清写真帧。质量判断全部由多模态 LLM（火山方舟 glm-5.3-flash）完成，
本地只做 ffmpeg 编解码，无需显卡。

## 流程

```
① ffprobe 探测 (时长/分辨率/竖屏校验, 自动处理旋转元数据)
② 均匀采样      每 0.5s 一帧, 缩放至长边 768px
③ LLM 密集评分  姿态/构图/表情/清晰度/整体 各 1-10 分 (并发)
④ 峰值精修      分数最高的 Top-K 时刻, ±0.6s 内以 0.1s 步长重抽并二次评分,
                抓跳跃顶点/肢体展开最大化的定格瞬间
⑤ LLM 全局精选  候选拼图 + 评分理由 → 跨时刻去重, 保证姿势与段落多样性
⑥ 导出成品      按时间戳 ffmpeg 精确 seek 导出原始分辨率 JPEG/PNG
```

## 安装

```bash
# 需要 Python 3.10+ 和 ffmpeg (在 PATH 中)
pip install -r requirements.txt
```

在项目根目录配置 `.env`：

```
ARK_API_KEY=你的key
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
ARK_MODEL=glm-5.3-flash
```

## 用法

```bash
python -m danceframes dance.mp4                          # 默认输出 12 张到 out/
python -m danceframes a.mp4 b.mp4 -o 照片 --top 20       # 批量 + 加大产出
python -m danceframes dance.mp4 --enhance --png          # 增强 + PNG 成品
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o/--out` | `out` | 输出根目录 |
| `--top` | 12 | 最终写真张数 |
| `--interval` | 0.5 | 粗采样间隔(秒) |
| `--refine-k` | 15 | 进入峰值精修的时刻数 |
| `--refine-span` | 0.6 | 精修窗口半径(秒) |
| `--refine-step` | 0.1 | 精修采样步长(秒) |
| `--min-score` | 6.0 | 精修段候选最低 overall 分 |
| `--workers` | 6 | API 并发数 |
| `--upload-long-edge` | 768 | 送 LLM 评分图片的长边像素 |
| `--enhance` | 关 | 额外输出增强版 (自动对比度+锐化) |
| `--png` | 关 | 成品用 PNG |

## 输出结构

```
out/<视频名>/
├── finals/            成品写真 (原始分辨率, 文件名含时间戳)
├── finals_enhanced/   (--enhance 时) 增强版
├── contact_sheet.jpg  成品总览拼图
├── report.json        全帧评分、精修窗口、选择理由 (调参回溯用)
└── work/              中间帧 (coarse/ refine/ select/)
```

## 调参建议

- **速度**：glm-5.3-flash 是思考型模型，单次评分约 5~8s。60s 视频约 300 次调用，
  默认 6 并发约 6 分钟；额度充足时可 `--workers 10` 提速。
- **动作周期长的舞种**（慢舞/大招定格）：加大 `--refine-span 1.0`。
- **想多出片**：`--top 20 --refine-k 20`。
- **成片质量不佳**：先看 `report.json` 里低分帧的 `reason`，通常是源视频码率低或
  运动模糊严重；也可提高 `--min-score` 过滤。

## 常见问题

- **全局精选回退**：若 LLM 全局精选调用失败，自动按 overall 分数排序出片，
  日志会标注 `[回退]`。
- **候选不足**：达标时刻不足 `--top` 时，自动从粗评分高分帧回填，保证有产出。
- **横屏视频**：仅警告不拦截，照常处理。
