"""Build and validate a sanitized 20-30 turn M5 operator hosting review."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.eval_transcript import evaluate  # noqa: E402
from services.data.sanitize import mask_known_identifier, mask_pii  # noqa: E402

RUBRIC: dict[str, str] = {
    "relevance": "Responses address the grounded message, goal, thread, or environment fact.",
    "continuity": "Turns preserve topic, promises, and corrections across the reviewed sequence.",
    "persona": "Mai's register is recognizable while respecting hard tone and safety overrides.",
    "non_confabulation": "Claims about viewers, events, goals, and environment have visible evidence.",
    "repetition": "Openers, jokes, questions, and proactive topics avoid mechanical repetition.",
    "hostness": "Mai actively carries grounded goals/threads and invites interaction without filler spam.",
}
MIN_REVIEW_TURNS = 20
MAX_REVIEW_TURNS = 30


def build_review_sheet(
    records: Iterable[dict[str, Any]],
    *,
    turn_count: int = 25,
    source_label: str = "sanitized transcript",
) -> dict[str, Any]:
    if not MIN_REVIEW_TURNS <= int(turn_count) <= MAX_REVIEW_TURNS:
        raise ValueError("operator review must contain 20-30 turns")
    selected = list(records)[: int(turn_count)]
    if len(selected) < int(turn_count):
        raise ValueError("not enough turns for requested operator review")
    turns = [_sanitize_turn(index, record) for index, record in enumerate(selected, 1)]
    automated = _automated_metrics(turns)
    return {
        "schema_version": 1,
        "milestone": "M5",
        "source": source_label,
        "raw_transcript_committed": False,
        "turn_count": len(turns),
        "turns": turns,
        "automated_metrics": automated,
        "rubric": dict(RUBRIC),
        "operator_review": {
            "reviewer": "",
            "scores": {key: None for key in RUBRIC},
            "notes": {key: "" for key in RUBRIC},
            "flagged_turns": [],
        },
    }


def validate_operator_review(sheet: dict[str, Any]) -> dict[str, Any]:
    turns = list(sheet.get("turns") or [])
    if not MIN_REVIEW_TURNS <= len(turns) <= MAX_REVIEW_TURNS:
        raise ValueError("operator review must contain 20-30 turns")
    review = dict(sheet.get("operator_review") or {})
    reviewer = " ".join(str(review.get("reviewer") or "").split())
    if not reviewer:
        raise ValueError("operator reviewer is required")
    scores = dict(review.get("scores") or {})
    notes = dict(review.get("notes") or {})
    normalized: dict[str, int] = {}
    for key in RUBRIC:
        value = scores.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise ValueError(f"rubric score {key} must be an integer within [1, 5]")
        if not " ".join(str(notes.get(key) or "").split()):
            raise ValueError(f"rubric note {key} is required")
        normalized[key] = value
    return {
        "reviewer": reviewer,
        "turn_count": len(turns),
        "scores": normalized,
        "average_score": round(sum(normalized.values()) / len(normalized), 2),
        "complete": True,
    }


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                yield value


def _sanitize_turn(index: int, record: dict[str, Any]) -> dict[str, Any]:
    known_name = record.get("viewer_name") or record.get("author")
    user = mask_known_identifier(mask_pii(record.get("user_text")), known_name) or ""
    mai = mask_known_identifier(mask_pii(record.get("mai_text")), known_name) or ""
    evidence_ids = tuple(
        str(item)[:120] for item in record.get("evidence_ids", [])
        if str(item).strip()
    )
    return {
        "review_turn": index,
        "kind": str(record.get("kind") or "unknown")[:40],
        "user_text": _compact(user, 240),
        "mai_text": _compact(mai, 400),
        "action": str(record.get("action") or "")[:60],
        "behavior": str(record.get("behavior") or "")[:60],
        "evidence_ids": list(evidence_ids[:6]),
    }


def _automated_metrics(turns: list[dict[str, Any]]) -> dict[str, Any]:
    report = evaluate(turns)
    proactive = sum(1 for turn in turns if turn["kind"] == "ambient")
    behaviors = Counter(turn["behavior"] for turn in turns if turn["behavior"])
    grounded = sum(1 for turn in turns if turn["evidence_ids"])
    return {
        "opener_repeat_ratio": report.opener_repeat_ratio,
        "mood_exposition_count": report.mood_exposition_count,
        "proactive_turn_ratio": round(proactive / len(turns), 3) if turns else 0.0,
        "turns_with_evidence_ratio": round(grounded / len(turns), 3) if turns else 0.0,
        "behavior_counts": dict(sorted(behaviors.items())),
    }


def _compact(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path("logs/turns.jsonl"))
    parser.add_argument("--turns", type=int, default=25)
    parser.add_argument("--output", type=Path, default=Path("logs/m5_hosting_review.json"))
    parser.add_argument("--validate-review", type=Path)
    args = parser.parse_args()
    try:
        if args.validate_review:
            sheet = json.loads(args.validate_review.read_text(encoding="utf-8"))
            result = validate_operator_review(sheet)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        sheet = build_review_sheet(
            iter_jsonl(args.file), turn_count=args.turns,
            source_label=f"sanitized:{args.file.name}",
        )
        _write_json(args.output, sheet)
        print(json.dumps({
            "output": str(args.output),
            "turn_count": sheet["turn_count"],
            "operator_review_complete": False,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
