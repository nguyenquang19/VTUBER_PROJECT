"""Create a bounded, time-stratified local sample from a yt-dlp live-chat JSONL corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

_SUPPORTED_RENDERERS = {
    "liveChatTextMessageRenderer",
    "liveChatPaidMessageRenderer",
    "liveChatPaidStickerRenderer",
    "liveChatMembershipItemRenderer",
}


def select_stratified_lines(input_path: Path, *, count: int) -> tuple[list[str], dict[str, Any]]:
    """Select eligible chat lines evenly across one source timeline without rewriting their contents."""
    if count <= 0:
        raise ValueError("count must be positive")
    eligible: list[str] = []
    total_lines = 0
    malformed = 0
    for raw_line in input_path.read_text(encoding="utf-8-sig").splitlines():
        if not raw_line.strip():
            continue
        total_lines += 1
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if _has_supported_chat(payload):
            eligible.append(raw_line)
    if count > len(eligible):
        raise ValueError("count exceeds eligible chat lines")
    indexes = _stratified_indexes(len(eligible), count)
    selected = [eligible[index] for index in indexes]
    return selected, {
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "input_lines": total_lines,
        "malformed_lines": malformed,
        "eligible_chat_lines": len(eligible),
        "selected_chat_lines": len(selected),
    }


def write_sample(output_path: Path, lines: Sequence[str]) -> None:
    rendered = "\n".join(lines) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(output_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _stratified_indexes(size: int, count: int) -> tuple[int, ...]:
    if size <= 0 or count <= 0 or count > size:
        raise ValueError("invalid stratified sampling bounds")
    if count == 1:
        return (0,)
    return tuple(round(index * (size - 1) / (count - 1)) for index in range(count))


def _has_supported_chat(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    actions = ((payload.get("replayChatItemAction") or {}).get("actions") or ())
    for action in actions:
        item = ((action or {}).get("addChatItemAction") or {}).get("item") or {}
        if any(name in item for name in _SUPPORTED_RENDERERS):
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        lines, summary = select_stratified_lines(args.input, count=args.count)
        write_sample(args.output, lines)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__}, ensure_ascii=False))
        return 2
    print(json.dumps({
        **summary,
        "output": str(args.output.resolve()),
        "sanitized_summary": True,
        "raw_chat_printed": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())