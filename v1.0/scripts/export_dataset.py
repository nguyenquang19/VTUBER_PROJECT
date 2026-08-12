"""Export dataset fine-tune (T5, Phase 8 data pipeline).

Đọc logs/{turns,delivery_outcomes,ratings,pref_pairs,corrections}.jsonl → emit bundle bất biến:
  - canonical/turns.jsonl + manifest/checksum/provenance
  - SFT (messages format): dạy model NÓI như Mai
  - DPO (prompt/chosen/rejected): dạy model tránh câu dở

Lọc rác:
  - chỉ nhận generation attempt có delivery outcome `delivered=true`
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
import hashlib
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.data.sanitize import mask_known_identifier, mask_pii_with_count  # noqa: E402
from services.evaluation.data_quality import (  # noqa: E402
    DatasetQualityGate,
    index_delivery_outcomes,
    load_data_contract,
    quality_report,
)

Record = dict[str, Any]
RecordIdentity = tuple[str, int]


def _read_jsonl(path: Path) -> list[Record]:
    return _read_jsonl_with_stats(path)[0]


def _read_jsonl_with_stats(path: Path) -> tuple[list[Record], int]:
    if not path.exists():
        return [], 0
    out: list[Record] = []
    invalid = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    out.append(record)
                else:
                    invalid += 1
            except (json.JSONDecodeError, UnicodeDecodeError):
                invalid += 1
    return out, invalid


class DatasetScrubber:
    """Scrub human-authored text and expose substitution count for dry-run metrics."""

    def __init__(self) -> None:
        self.masked_fields = 0

    def text(self, value: Any, known_identifier: str | None = None) -> str:
        raw = mask_known_identifier(str(value or ""), known_identifier)
        if raw != str(value or ""):
            self.masked_fields += 1
        masked, count = mask_pii_with_count(raw)
        self.masked_fields += count
        return masked or ""


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
    scrubber: DatasetScrubber | None = None,
) -> list[Record]:
    """1 SFT example / turn hợp lệ (messages format)."""
    scrub = scrubber or DatasetScrubber()
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

        cause = t.get("mood_cause") or {}
        alias = cause.get("alias") if isinstance(cause, dict) else None
        messages = [{"role": "system",
                     "content": _persona_content(t.get("persona_version"), persona_mode, persona_text)}]
        ctx = t.get("context_block")
        if ctx:
            messages.append({"role": "system", "content": scrub.text(ctx, alias)})
        user = t.get("user_text")
        if user:
            messages.append({"role": "user", "content": scrub.text(user, alias)})
        elif t.get("kind") == "ambient":
            messages.append({"role": "user", "content": "(Mai tự lên tiếng)"})
        messages.append({"role": "assistant", "content": scrub.text(target, alias)})
        out.append({"schema_version": 1,
                    "timestamp": _record_timestamp(t),
                    "source": f"sft:{t.get('kind') or 'turn'}",
                    "session_id": identity[0],
                    "messages": messages,
                    "meta": {"session_id": identity[0], "turn_id": identity[1],
                             "kind": t.get("kind"), "rating": rating,
                             "corrected": identity in corrections_by_identity,
                             **_contract_meta(t)}})
    return out


def build_dpo(
    pref_pairs: list[Record],
    corrections: list[Record],
    turns_by_identity: dict[RecordIdentity, Record],
    scrubber: DatasetScrubber | None = None,
) -> list[Record]:
    """DPO từ pref_pairs (regen) + corrections (gốc→sửa)."""
    scrub = scrubber or DatasetScrubber()
    out = []
    for p in pref_pairs:
        identity = _identity(p, "pref_pairs")
        pr = p.get("prompt_ref") or {}
        out.append({"schema_version": 1, "timestamp": _record_timestamp(p),
                    "session_id": identity[0] if identity else None,
                    "prompt": _dpo_prompt(
                        pr.get("context_block"), pr.get("user_text"), scrub, None,
                    ),
                    "chosen": scrub.text(p.get("chosen", "")),
                    "rejected": scrub.text(p.get("rejected", "")),
                    "source": p.get("reason", "pref"),
                    "meta": {**_identity_meta(identity), **_contract_meta(pr)}})
    for c in corrections:
        orig = (c.get("original") or "").strip()
        corr = (c.get("corrected") or "").strip()
        if not orig or not corr or orig == corr:
            continue
        identity = _identity(c, "corrections")
        t = turns_by_identity.get(identity, {}) if identity is not None else {}
        cause = t.get("mood_cause") or {}
        alias = cause.get("alias") if isinstance(cause, dict) else None
        out.append({"schema_version": 1, "timestamp": _record_timestamp(c),
                    "session_id": identity[0] if identity else None,
                    "prompt": _dpo_prompt(
                        t.get("context_block"), t.get("user_text"), scrub, alias,
                    ),
                    "chosen": scrub.text(corr, alias),
                    "rejected": scrub.text(orig, alias),
                    "source": "correction",
                    "meta": {**_identity_meta(identity), **_contract_meta(t)}})
    return out


def _identity_meta(identity: RecordIdentity | None) -> Record:
    if identity is None:
        return {"session_id": None, "turn_id": None}
    return {"session_id": identity[0], "turn_id": identity[1]}


def _contract_meta(record: Record) -> Record:
    return {
        key: record.get(key)
        for key in (
            "persona_version", "architecture_version",
            "context_schema_version", "agenda_policy_version",
        )
    }


def _dpo_prompt(
    context_block: Any,
    user_text: Any,
    scrubber: DatasetScrubber,
    known_identifier: str | None,
) -> str:
    parts = []
    if context_block:
        parts.append(scrubber.text(context_block, known_identifier))
    if user_text:
        parts.append(scrubber.text(user_text, known_identifier))
    return "\n".join(parts)


def _record_timestamp(record: Record) -> str:
    value = record.get("timestamp") or record.get("ts")
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser(description="Export SFT + DPO dataset")
    ap.add_argument("--in-dir", default="logs")
    ap.add_argument("--out-dir", default="data/datasets")
    ap.add_argument("--persona", choices=["ref", "full"], default="ref")
    ap.add_argument("--persona-file", default="config/prompts/persona_system.txt")
    ap.add_argument("--contract", default="eval/contracts/mai_agent_v1.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate/scrub/count only; do not create dataset files")
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    turns, invalid_turns = _read_jsonl_with_stats(in_dir / "turns.jsonl")
    ratings, invalid_ratings = _read_jsonl_with_stats(in_dir / "ratings.jsonl")
    pref_pairs, invalid_prefs = _read_jsonl_with_stats(in_dir / "pref_pairs.jsonl")
    corrections, invalid_corrections = _read_jsonl_with_stats(in_dir / "corrections.jsonl")
    delivery_outcomes, invalid_outcomes = _read_jsonl_with_stats(
        in_dir / "delivery_outcomes.jsonl",
    )
    invalid_total = (
        invalid_turns + invalid_ratings + invalid_prefs
        + invalid_corrections + invalid_outcomes
    )

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

    gate = DatasetQualityGate(load_data_contract(args.contract))
    eligible_turns, report = quality_report(
        turns, gate,
        ratings=ratings_by_identity,
        corrections=set(corrections_by_identity),
        delivery_outcomes=index_delivery_outcomes(delivery_outcomes),
    )
    canonical_turns = [gate.canonicalize_turn(turn) for turn in eligible_turns]
    eligible_identities = {
        identity for turn in eligible_turns
        if (identity := _identity(turn, "turns")) is not None
    }
    eligible_corrections = [
        record for record in corrections
        if _identity(record, "corrections") in eligible_identities
    ]
    eligible_pref_pairs = [
        record for record in pref_pairs if gate.assess_preference(record).eligible
    ]

    scrubber = DatasetScrubber()
    sft = build_sft(
        canonical_turns, ratings_by_identity, corrections_by_identity, args.persona, persona_text,
        scrubber,
    )
    dpo = build_dpo(eligible_pref_pairs, eligible_corrections, turns_by_identity, scrubber)
    sft_splits = gate.partition(sft)
    dpo_splits = gate.partition(dpo)

    out_dir = Path(args.out_dir)
    dataset_path: Path | None = None
    if not args.dry_run:
        dataset_path = write_dataset_bundle(
            out_dir=out_dir,
            contract=gate.contract,
            source_paths=[
                in_dir / "turns.jsonl",
                in_dir / "delivery_outcomes.jsonl",
                in_dir / "ratings.jsonl",
                in_dir / "corrections.jsonl",
                in_dir / "pref_pairs.jsonl",
            ],
            canonical_turns=canonical_turns,
            sft_splits=sft_splits,
            dpo_splits=dpo_splits,
            quality={
            **report,
            "sft_split_counts": {key: len(value) for key, value in sft_splits.items()},
            "dpo_split_counts": {key: len(value) for key, value in dpo_splits.items()},
            "invalid_records": invalid_total,
            "pii_masks": scrubber.masked_fields,
            },
        )

    # thống kê
    kinds = Counter(t.get("kind") for t in turns)
    rate_dist = Counter(ratings_by_identity.values())
    print("=== EXPORT DATASET ===")
    print(f"turns đọc:        {len(turns)}  ({dict(kinds)})")
    print(f"ratings:          {dict(rate_dist)}")
    print(f"corrections:      {len(corrections)}")
    print(f"invalid records:  {invalid_total}")
    print(f"PII masks:        {scrubber.masked_fields}")
    print(f"contract:         {gate.contract.contract_id}")
    print(f"eligible turns:   {report['eligible_turns']}")
    print(f"quarantined:      {report['quarantined_turns']}")
    print(f"quarantine why:   {report['quarantine_reason_counts']}")
    suffix = " (dry-run, không ghi file)" if args.dry_run else ""
    destination = dataset_path or out_dir
    print(f"SFT xuất:         {len(sft)}  → {destination}{suffix}")
    print(f"DPO xuất:         {len(dpo)}  → {destination}{suffix}")
    print(f"  DPO nguồn:      {dict(Counter(d['source'] for d in dpo))}")
    print(f"SFT splits:       {dict((key, len(value)) for key, value in sft_splits.items())}")
    print(f"DPO splits:       {dict((key, len(value)) for key, value in dpo_splits.items())}")


def write_dataset_bundle(
    *,
    out_dir: Path,
    contract: Any,
    source_paths: list[Path],
    canonical_turns: list[Record],
    sft_splits: dict[str, list[Record]],
    dpo_splits: dict[str, list[Record]],
    quality: Record,
    now: datetime | None = None,
) -> Path:
    """Atomically create one immutable, self-describing dataset directory."""
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sources = [_source_manifest(path) for path in source_paths if path.exists()]
    fingerprint = hashlib.sha256(
        json.dumps(sources, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:10]
    dataset_id = (
        f"{contract.contract_id}-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}-{fingerprint}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    final_dir = out_dir / dataset_id
    temporary = out_dir / f".{dataset_id}.tmp"
    if final_dir.exists() or temporary.exists():
        raise FileExistsError(f"dataset bundle already exists: {dataset_id}")
    try:
        temporary.mkdir()
        _write(temporary / "canonical" / "turns.jsonl", canonical_turns)
        for split, records in sft_splits.items():
            _write(temporary / "sft" / f"{split}.jsonl", records)
        for split, records in dpo_splits.items():
            _write(temporary / "dpo" / f"{split}.jsonl", records)
        _write_json(temporary / "quality_report.json", quality)
        manifest: Record = {
            "schema_version": 1,
            "dataset_id": dataset_id,
            "created_at": created_at.isoformat(),
            "contract_id": contract.contract_id,
            "contract_schema_version": contract.schema_version,
            "canonical_schema_version": contract.canonical_schema_version,
            "source_turn_schema_version": contract.turn_schema_version,
            "compatible_turn_schema_versions": list(
                contract.compatible_turn_schema_versions,
            ),
            "delivery_outcome_schema_version": contract.delivery_outcome_schema_version,
            "canonical_adapter_ids": sorted({
                str(row.get("canonical_adapter_id")) for row in canonical_turns
            }),
            "compatible_persona_versions": list(contract.compatible_persona_versions),
            "sources": sources,
            "counts": {
                "canonical_turns": len(canonical_turns),
                "sft": {key: len(value) for key, value in sft_splits.items()},
                "dpo": {key: len(value) for key, value in dpo_splits.items()},
                "quarantined_turns": int(quality.get("quarantined_turns", 0)),
            },
        }
        _write_json(temporary / "manifest.json", manifest)
        temporary.replace(final_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return final_dir


def _source_manifest(path: Path) -> Record:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def _write(path: Path, records: list[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def _write_json(path: Path, value: Record) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
