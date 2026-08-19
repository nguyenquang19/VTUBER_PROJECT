"""eval_transcript — B0 baseline eval; xem docs/MAI_V2_SYSTEM_SPEC.md.

Đọc `logs/turns.jsonl` (schema từ LLMTurnRunner._log_turn), in 4 metric:

  - opener_repeat_ratio: (# câu Mai có 3 từ đầu trùng với câu Mai khác) / tổng
  - dead_air_gaps: danh sách gap giữa 2 turn liên tiếp > threshold_s (mặc định 10s)
  - mood_exposition_count: # turn có mood block dạng `[vui:N buon:N ...]` trong mai_text
  - turn_counts: {chat_reply, ambient, total}

Naturalness/hostness KHÔNG đo — human-rate manual, ghi tay vào baseline README.

Usage:
    python scripts/eval_transcript.py                       # đọc logs/turns.jsonl
    python scripts/eval_transcript.py --file logs/turns.jsonl.1
    python scripts/eval_transcript.py --since 2026-08-06T18:00:00
    python scripts/eval_transcript.py --dead-air-threshold 15
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MOOD_KEYS = ("vui", "buon", "buồn", "buc", "bực", "bon_chon", "bồn_chồn", "nguong", "ngượng", "neutral")
_MOOD_BLOCK_RE = re.compile(
    r"\[\s*(?:" + "|".join(MOOD_KEYS) + r")\s*:\s*-?\d+", re.IGNORECASE
)


@dataclass
class Report:
    total: int = 0
    by_kind: Counter = field(default_factory=Counter)
    opener_top: list[tuple[str, int]] = field(default_factory=list)
    opener_repeat_count: int = 0
    opener_repeat_ratio: float = 0.0
    dead_air_gaps_s: list[float] = field(default_factory=list)
    mood_exposition_count: int = 0
    parse_fail_count: int = 0
    span_start: str | None = None
    span_end: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_turns": self.total,
            "by_kind": dict(self.by_kind),
            "opener_repeat_count": self.opener_repeat_count,
            "opener_repeat_ratio": round(self.opener_repeat_ratio, 3),
            "opener_top5": self.opener_top,
            "dead_air_count": len(self.dead_air_gaps_s),
            "dead_air_gaps_s": [round(g, 1) for g in self.dead_air_gaps_s],
            "mood_exposition_count": self.mood_exposition_count,
            "parse_fail_count": self.parse_fail_count,
            "span_start": self.span_start,
            "span_end": self.span_end,
        }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    # jsonl có thể ghi "Z" hoặc offset. datetime.fromisoformat handle offset từ 3.11.
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _opener3(text: str) -> str | None:
    """3 từ đầu tiên viết thường (bỏ dấu câu 2 đầu). None nếu <3 từ."""
    if not text:
        return None
    words = re.findall(r"[^\s]+", text.strip().lower())
    if len(words) < 3:
        return None
    return " ".join(words[:3]).strip(".,!?…\"'()[]")


def _has_mood_block(text: str) -> bool:
    if not text:
        return False
    return bool(_MOOD_BLOCK_RE.search(text))


def iter_records(path: Path, since: datetime | None = None) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                ts = _parse_iso(rec.get("timestamp"))
                if ts is None or ts < since:
                    continue
            yield rec


def evaluate(records: Iterable[dict[str, Any]], dead_air_threshold_s: float = 10.0) -> Report:
    rep = Report()
    openers: list[str] = []
    last_ts: datetime | None = None

    for rec in records:
        rep.total += 1
        kind = rec.get("kind") or "unknown"
        rep.by_kind[kind] += 1

        text = rec.get("mai_text") or ""
        # A1.1: ưu tiên field raw_had_mood_block (đo LLM có tự sinh block hay không —
        # parser đã strip block khỏi mai_text nên regex trên mai_text luôn = 0).
        # Fallback về regex mai_text cho log cũ (trước A1.1) không có field này.
        if "raw_had_mood_block" in rec:
            if rec["raw_had_mood_block"]:
                rep.mood_exposition_count += 1
        elif _has_mood_block(text):
            rep.mood_exposition_count += 1
        if rec.get("parse_ok") is False:
            rep.parse_fail_count += 1

        op = _opener3(text)
        if op is not None:
            openers.append(op)

        ts = _parse_iso(rec.get("timestamp"))
        if ts is not None:
            iso = ts.astimezone(timezone.utc).isoformat()
            if rep.span_start is None:
                rep.span_start = iso
            rep.span_end = iso
            if last_ts is not None:
                gap = (ts - last_ts).total_seconds()
                if gap > dead_air_threshold_s:
                    rep.dead_air_gaps_s.append(gap)
            last_ts = ts

    if openers:
        counts = Counter(openers)
        repeated = sum(c for _o, c in counts.items() if c >= 2)
        rep.opener_repeat_count = repeated
        rep.opener_repeat_ratio = repeated / len(openers)
        rep.opener_top = counts.most_common(5)
    return rep


def render(rep: Report, dead_air_threshold_s: float) -> str:
    d = rep.to_dict()
    lines = [
        "=== TRANSCRIPT EVAL — B0 baseline ===",
        f"span:  {d['span_start']}  →  {d['span_end']}",
        f"turns: {d['total_turns']} total  ({d['by_kind']})",
        "",
        f"opener_repeat:    {d['opener_repeat_count']} / {rep.total}  ratio={d['opener_repeat_ratio']}  (target <0.10)",
        f"top openers:      {d['opener_top5']}",
        "",
        f"dead_air > {dead_air_threshold_s:.0f}s:  {d['dead_air_count']} gap(s)  values={d['dead_air_gaps_s']}",
        "",
        f"mood_exposition:  {d['mood_exposition_count']}  (target 0 sau A1)",
        f"parse_fail:       {d['parse_fail_count']}",
        "",
        "naturalness/hostness: human-rate 20 câu, ghi tay vào docs/baselines/",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--file", default="logs/turns.jsonl", help="đường dẫn turns.jsonl")
    ap.add_argument("--since", default=None, help="ISO datetime, lọc từ mốc này trở đi")
    ap.add_argument("--dead-air-threshold", type=float, default=10.0, help="gap tính là dead-air (giây)")
    ap.add_argument("--json", action="store_true", help="in JSON thay vì text")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"[eval] file không tồn tại: {path}", file=sys.stderr)
        return 2

    since = _parse_iso(args.since) if args.since else None
    if args.since and since is None:
        print(f"[eval] --since không parse được: {args.since}", file=sys.stderr)
        return 2

    rep = evaluate(iter_records(path, since=since), dead_air_threshold_s=args.dead_air_threshold)
    if rep.total == 0:
        print(f"[eval] không có record nào (file rỗng hoặc lệch since?): {path}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(rep, args.dead_air_threshold))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
