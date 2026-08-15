"""LLM-judge lọc SFT theo chất lượng NGỮ NGHĨA (release 1.2.0).

Gate cấu trúc (data_quality) chỉ chặn rác kỹ thuật. Judge này chặn rác ngữ nghĩa:
câu đúng luật nhưng NHẠT, máy móc, lệch chất, hoặc bịa. Chấm mỗi candidate CÙNG
context (cả mạch hội thoại), đúng nguyên tắc "tốt/xấu tùy tình huống".

Pluggable: `llm_fn(prompt:str)->str`. Không tự dựng model — export truyền vào, test
truyền stub. Fail-safe: judge lỗi → GIỮ example (không loại nhầm) + đếm lỗi.
"""
from __future__ import annotations

import re
import urllib.request
import json
from typing import Any, Callable

LLMFn = Callable[[str], str]

RUBRIC = (
    "Bạn là giám khảo chất lượng dữ liệu huấn luyện cho một VTuber tên Mai. "
    "Cho đoạn hội thoại dưới đây, chấm câu CUỐI của Mai theo thang 0.0–1.0 dựa trên: "
    "(1) đúng chất persona Mai (tự nhiên, có cá tính, không giọng trợ lý); "
    "(2) mạch lạc với ngữ cảnh phía trên; "
    "(3) KHÔNG nhạt/máy móc/sáo rỗng; "
    "(4) KHÔNG bịa sự kiện không có trong context. "
    "Chỉ trả về DUY NHẤT một số thập phân 0.0–1.0, không giải thích."
)

_SCORE_RE = re.compile(r"([01](?:\.\d+)?)")


def render_conversation(messages: list[dict]) -> str:
    """Render messages (persona + history + user + assistant) thành text cho judge."""
    lines = []
    for m in messages:
        role = m.get("role", "")
        content = " ".join(str(m.get("content", "")).split())
        if role == "system":
            lines.append(f"[hệ thống] {content}")
        elif role == "user":
            lines.append(f"Người xem: {content}")
        elif role == "assistant":
            lines.append(f"Mai: {content}")
    return "\n".join(lines)


def build_judge_prompt(messages: list[dict]) -> str:
    return f"{RUBRIC}\n\n---\n{render_conversation(messages)}\n---\nĐiểm:"


def parse_score(text: str) -> float | None:
    m = _SCORE_RE.search(str(text or ""))
    if not m:
        return None
    try:
        return max(0.0, min(1.0, float(m.group(1))))
    except ValueError:
        return None


class QualityJudge:
    def __init__(self, llm_fn: LLMFn) -> None:
        self._llm = llm_fn
        self.errors = 0

    def score(self, example: dict) -> float | None:
        try:
            prompt = build_judge_prompt(example.get("messages", []))
            return parse_score(self._llm(prompt))
        except Exception:
            self.errors += 1
            return None


def default_llama_judge_fn(
    base_url: str = "http://127.0.0.1:8080", timeout: float = 30.0,
) -> LLMFn:
    """Judge dùng chính llama-server local (/completion). Cần server đang chạy."""
    def _fn(prompt: str) -> str:
        payload = json.dumps({
            "prompt": prompt, "n_predict": 8, "temperature": 0.0,
            "stop": ["\n"], "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/completion", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("content", "")
    return _fn


def filter_sft_by_judge(
    sft: list[dict], *, min_score: float, llm_fn: LLMFn | None = None,
) -> tuple[list[dict], int]:
    """Giữ example có điểm >= min_score. Trả (kept, dropped_count).

    Judge lỗi/không parse được → GIỮ example (fail-safe, không loại nhầm), gắn
    meta.judge_score để lần sau review.
    """
    judge = QualityJudge(llm_fn or default_llama_judge_fn())
    kept: list[dict] = []
    dropped = 0
    for ex in sft:
        s = judge.score(ex)
        ex.setdefault("meta", {})["judge_score"] = s
        if s is not None and s < min_score:
            dropped += 1
            continue
        kept.append(ex)
    return kept, dropped
