"""Export dataset fine-tune (T5, Phase 8 data pipeline).

Đọc logs/{turns,ratings,pref_pairs,corrections}.jsonl → emit:
  - SFT (messages format): dạy model NÓI như Mai
  - DPO (prompt/chosen/rejected): dạy model tránh câu dở

Lọc rác:
  - bỏ level_used=1 (canned — không phải giọng Mai)
  - bỏ parse_ok=false / mai_text rỗng
  - bỏ filter_verdict.passed=false chưa regen (câu bị chặn)
  - bỏ operator_rating=bad; correction → dùng câu SỬA làm target (ưu tiên cao)
  - scrub PII lần cuối

Chạy:
  python scripts/export_dataset.py [--in-dir logs] [--out-dir data/datasets] [--persona ref|full]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.data.sanitize import mask_pii  # noqa: E402

Record = dict[str, Any]
RecordIdentity = tuple[str, int]


def _read_jsonl(path: Path) -> list[Record]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _persona_content(persona_version: str | None, mode: str, persona_text: str) -> str:
    return persona_text if mode == "full" else f"[persona:{persona_version or 'unknown'}]"


def _identity(record: Record, legacy_source: str) -> RecordIdentity | None:
    """Return composite identity; isolate records that predate session IDs by source."""
    try:
        turn_id = int(record["turn_id"])
    except (KeyError, TypeError, ValueError):
        return None
    session_id = record.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = f"legacy:{legacy_source}"
    return session_id, turn_id


def build_sft(
    turns: list[Record],
    ratings_by_identity: dict[RecordIdentity, str],
    corrections_by_identity: dict[RecordIdentity, str | None],
    persona_mode: str,
    persona_text: str,
) -> list[Record]:
    """1 SFT example / turn hợp lệ (messages format)."""
    out = []
    for t in turns:
        identity = _identity(t, "turns")
        if identity is None:
            continue
        if t.get("level_used", 0) != 0:          # canned → bỏ
            continue
        if t.get("parse_ok") is False:
            continue
        rating = ratings_by_identity.get(identity)
        if rating == "bad" and identity not in corrections_by_identity:
            continue                              # bad chưa sửa → bỏ
        fv = t.get("filter_verdict") or {}
        if fv.get("passed") is False and not fv.get("regen"):
            continue                              # câu bị chặn chưa regen → bỏ
        # correction → target = câu sửa (ưu tiên cao nhất)
        target = corrections_by_identity.get(identity) or t.get("mai_text")
        target = (target or "").strip()
        if not target:
            continue

        messages = [{"role": "system",
                     "content": _persona_content(t.get("persona_version"), persona_mode, persona_text)}]
        ctx = t.get("context_block")
        if ctx:
            messages.append({"role": "system", "content": ctx})
        user = t.get("user_text")
        if user:
            messages.append({"role": "user", "content": mask_pii(user)})
        elif t.get("kind") == "ambient":
            messages.append({"role": "user", "content": "(Mai tự lên tiếng)"})
        messages.append({"role": "assistant", "content": mask_pii(target)})
        out.append({"messages": messages,
                    "meta": {"session_id": identity[0], "turn_id": identity[1],
                             "kind": t.get("kind"), "rating": rating,
                             "corrected": identity in corrections_by_identity}})
    return out


def build_dpo(
    pref_pairs: list[Record],
    corrections: list[Record],
    turns_by_identity: dict[RecordIdentity, Record],
) -> list[Record]:
    """DPO từ pref_pairs (regen) + corrections (gốc→sửa)."""
    out = []
    for p in pref_pairs:
        identity = _identity(p, "pref_pairs")
        pr = p.get("prompt_ref") or {}
        out.append({"prompt": _dpo_prompt(pr.get("context_block"), pr.get("user_text")),
                    "chosen": mask_pii(p.get("chosen", "")),
                    "rejected": mask_pii(p.get("rejected", "")),
                    "source": p.get("reason", "pref"),
                    "meta": _identity_meta(identity)})
    for c in corrections:
        orig = (c.get("original") or "").strip()
        corr = (c.get("corrected") or "").strip()
        if not orig or not corr or orig == corr:
            continue
        identity = _identity(c, "corrections")
        t = turns_by_identity.get(identity, {}) if identity is not None else {}
        out.append({"prompt": _dpo_prompt(t.get("context_block"), t.get("user_text")),
                    "chosen": mask_pii(corr), "rejected": mask_pii(orig),
                    "source": "correction", "meta": _identity_meta(identity)})
    return out


def _identity_meta(identity: RecordIdentity | None) -> Record:
    if identity is None:
        return {"session_id": None, "turn_id": None}
    return {"session_id": identity[0], "turn_id": identity[1]}


def _dpo_prompt(context_block, user_text) -> str:
    parts = []
    if context_block:
        parts.append(context_block)
    if user_text:
        parts.append(mask_pii(user_text))
    return "\n".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="Export SFT + DPO dataset")
    ap.add_argument("--in-dir", default="logs")
    ap.add_argument("--out-dir", default="data/datasets")
    ap.add_argument("--persona", choices=["ref", "full"], default="ref")
    ap.add_argument("--persona-file", default="config/prompts/persona_system.txt")
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    turns = _read_jsonl(in_dir / "turns.jsonl")
    ratings = _read_jsonl(in_dir / "ratings.jsonl")
    pref_pairs = _read_jsonl(in_dir / "pref_pairs.jsonl")
    corrections = _read_jsonl(in_dir / "corrections.jsonl")

    persona_text = ""
    pf = Path(args.persona_file)
    if args.persona == "full" and pf.exists():
        persona_text = pf.read_text(encoding="utf-8")

    ratings_by_identity = {
        identity: r["rating"]
        for r in ratings
        if (identity := _identity(r, "ratings")) is not None and "rating" in r
    }
    corrections_by_identity = {
        identity: c.get("corrected")
        for c in corrections
        if (identity := _identity(c, "corrections")) is not None
    }
    turns_by_identity = {
        identity: t
        for t in turns
        if (identity := _identity(t, "turns")) is not None
    }

    sft = build_sft(
        turns, ratings_by_identity, corrections_by_identity, args.persona, persona_text,
    )
    dpo = build_dpo(pref_pairs, corrections, turns_by_identity)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    sft_path = out_dir / f"sft_{stamp}.jsonl"
    dpo_path = out_dir / f"dpo_{stamp}.jsonl"
    _write(sft_path, sft)
    _write(dpo_path, dpo)

    # thống kê
    kinds = Counter(t.get("kind") for t in turns)
    rate_dist = Counter(ratings_by_identity.values())
    print("=== EXPORT DATASET ===")
    print(f"turns đọc:        {len(turns)}  ({dict(kinds)})")
    print(f"ratings:          {dict(rate_dist)}")
    print(f"corrections:      {len(corrections)}")
    print(f"SFT xuất:         {len(sft)}  → {sft_path}")
    print(f"DPO xuất:         {len(dpo)}  → {dpo_path}")
    print(f"  DPO nguồn:      {dict(Counter(d['source'] for d in dpo))}")


def _write(path: Path, records: list[Record]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
