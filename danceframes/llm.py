"""火山方舟 (OpenAI 兼容) 多模态 LLM 客户端: 帧评分 / 全局精选."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .config import Settings

_SCORE_KEYS = ("pose", "composition", "expression", "clarity", "overall")


class LLMError(Exception):
    pass


def parse_json_obj(text: str) -> dict:
    """从模型输出中稳健提取 JSON 对象 (容忍 ```json 包裹 / 尾逗号)."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError(f"输出中未找到 JSON: {text[:200]}")
    s = m.group(0)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        s = re.sub(r",\s*([}\]])", r"\1", s)
        return json.loads(s)


class ArkClient:
    def __init__(self, s: Settings):
        if not s.api_key:
            raise LLMError("缺少 ARK_API_KEY (请在 .env 或 --api-key 提供)")
        self.url = s.base_url.rstrip("/") + "/chat/completions"
        self.model = s.model
        self.key = s.api_key
        self.workers = s.workers
        self.calls = 0           # 成功的 API 调用数
        self.failures = 0        # 最终失败数
        self._lock = threading.Lock()

    def chat(self, messages: list, temperature: float = 0.1,
             max_tokens: int = 4000, retries: int = 3) -> str:
        headers = {"Authorization": f"Bearer {self.key}"}
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        delays = [2, 4, 8]
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                r = requests.post(self.url, headers=headers, json=payload, timeout=120)
                if r.status_code == 200:
                    msg = r.json()["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    if not content:  # 思考型模型偶发只填 reasoning_content
                        content = (msg.get("reasoning_content") or "").strip()
                    if not content:
                        raise LLMError("模型返回空内容")
                    with self._lock:
                        self.calls += 1
                    return content
                if r.status_code == 400 or r.status_code == 401 or r.status_code == 404:
                    raise LLMError(f"请求被拒绝 HTTP {r.status_code}: {r.text[:300]}")
                last_err = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            except (requests.RequestException, LLMError, KeyError) as e:
                last_err = e
            if attempt < retries - 1:
                time.sleep(delays[min(attempt, len(delays) - 1)])
        with self._lock:
            self.failures += 1
        raise LLMError(f"重试{retries}次仍失败: {last_err}")

    @staticmethod
    def _image_content(path: Path) -> dict:
        b64 = base64.b64encode(path.read_bytes()).decode()
        return {"type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


# ---------- 单帧评分 ----------

_COARSE_PROMPT = """你是专业舞蹈摄影评委。这是竖屏舞蹈视频中 t={ts:.2f}s 处的一帧, 正在为"舞蹈写真"挑选最佳瞬间。
请对这一帧严格按 1-10 整数打分:
- pose 姿态: 舞姿美感与动作张力, 肢体线条是否舒展, 全身是否完整入镜
- composition 构图: 画面布局、主体位置与占比、背景是否干扰主体
- expression 表情: 面部是否可见、表情是否有感染力 (背对镜头或面部被遮挡则低分)
- clarity 清晰度: 是否模糊、有无运动残影、主体是否被遮挡
- overall 整体: 作为一张成品舞蹈写真的综合价值

只输出 JSON, 不要任何其他文字:
{{"pose": n, "composition": n, "expression": n, "clarity": n, "overall": n, "reason": "20字内中文理由"}}"""

_REFINE_EXTRA = "\n注意: 该帧来自动作峰值精修段, 请重点评估动作是否处于最佳定格瞬间 (如跳跃顶点、肢体展开最大化、定格瞬间), 动作张力权重最高。"


def score_frame(client: ArkClient, image: Path, ts: float,
                refine: bool = False) -> dict | None:
    """评分单帧, 返回 {pose,..,overall,reason} 或 None (失败)."""
    prompt = _COARSE_PROMPT.format(ts=ts) + (_REFINE_EXTRA if refine else "")
    try:
        text = client.chat([
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                ArkClient._image_content(image),
            ]}
        ])
        d = parse_json_obj(text)
        out = {}
        for k in _SCORE_KEYS:
            v = float(d[k])
            out[k] = max(1.0, min(10.0, v))
        out["reason"] = str(d.get("reason", ""))[:100]
        return out
    except (LLMError, KeyError, ValueError, TypeError):
        return None


def score_frames_concurrent(client: ArkClient,
                            frames: list[tuple[float, Path]],
                            refine: bool = False) -> list[dict | None]:
    """并发评分, 保持输入顺序返回."""
    results: list[dict | None] = [None] * len(frames)
    with ThreadPoolExecutor(max_workers=client.workers) as ex:
        futs = {ex.submit(score_frame, client, img, ts, refine): i
                for i, (ts, img) in enumerate(frames)}
        for fut, i in futs.items():
            results[i] = fut.result()
    return results


# ---------- 全局精选 ----------

_SELECT_PROMPT = """图中是从一段竖屏舞蹈视频中精选出的 {n} 个候选最佳瞬间缩略图, 左上角标注了编号。
各候选信息:
{listing}

请跨时刻去重 (姿势雷同、同一段落的近似画面只保留最优的一张), 选出最多 {top} 张作为最终"舞蹈写真"成片, 要求:
1. 优先 overall 高的候选
2. 覆盖视频中不同的段落与不同的舞蹈姿势, 保证多样性
3. 明显缺陷 (模糊/构图差/表情差) 的候选宁可舍弃

只输出 JSON, 不要任何其他文字:
{{"selected": [编号数组, 按推荐优先级排序], "rationale": "50字内中文说明取舍"}}"""


def global_select(client: ArkClient, grid: Path,
                  candidates: list[dict], top: int) -> dict:
    """candidates: [{id, ts, overall, pose, composition, expression, clarity, reason}]"""
    listing = "\n".join(
        f"#{c['id']} t={c['ts']:.2f}s overall={c['overall']:.0f} "
        f"pose={c['pose']:.0f} comp={c['composition']:.0f} "
        f"expr={c['expression']:.0f} clarity={c['clarity']:.0f} | {c['reason']}"
        for c in candidates
    )
    prompt = _SELECT_PROMPT.format(n=len(candidates), listing=listing, top=top)
    try:
        text = client.chat([
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                ArkClient._image_content(grid),
            ]}
        ], temperature=0.2)
        d = parse_json_obj(text)
        valid = {c["id"] for c in candidates}
        sel = []
        for i in d.get("selected", []):
            i = int(i)
            if i in valid and i not in sel:
                sel.append(i)
        return {"selected": sel[:top],
                "rationale": str(d.get("rationale", ""))[:300]}
    except (LLMError, KeyError, ValueError, TypeError) as e:
        return {"selected": [], "rationale": f"全局精选调用失败, 回退按分数排序: {e}"}
