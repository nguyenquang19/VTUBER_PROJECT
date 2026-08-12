"""Sampling sweep — A/B các cấu hình sampling trên 1 replay corpus.

Với mỗi config trong config/sampling_sweep.yaml:
  1. copy config/ ra thư mục tạm, patch models.yaml::llm_main theo config;
  2. chạy scripts/stress_youtube_llm.py <corpus> --config-dir <tmp> qua llama.cpp THẬT;
  3. gom metric từ report → bảng so sánh.

Cần llama-server đang chạy (models.yaml::llm_main). Chạy:
    python scripts/sampling_sweep.py logs/evaluation/<corpus>.json

So gì: distinct_2 ↑ (đỡ nhạt), exact_repetition ↓, assistant_register ↓,
avg_words hợp lý, turn_latency_p95 đừng tệ đi. Rồi ĐỌC review_sample bằng mắt.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _patch_models_yaml(cfg_dir: Path, overrides: dict) -> None:
    path = cfg_dir / "models.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    llm = data.setdefault("llm_main", {})
    for key, value in overrides.items():
        if value is None:
            llm.pop(key, None)      # null = bỏ param (dùng default server)
        else:
            llm[key] = value
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _run_one(name: str, overrides: dict, corpus: Path, out_dir: Path) -> dict:
    tmp_cfg = Path(tempfile.mkdtemp(prefix=f"sweep_{name}_"))
    shutil.copytree(REPO_ROOT / "config", tmp_cfg / "config")
    _patch_models_yaml(tmp_cfg / "config", overrides)
    report_path = out_dir / f"report_{name}.json"
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "stress_youtube_llm.py"),
        str(corpus), "--config-dir", str(tmp_cfg / "config"),
        "--output", str(report_path),
    ]
    print(f"[{name}] chạy replay...", flush=True)
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0 or not report_path.exists():
        print(f"[{name}] FAILED (returncode {result.returncode})")
        return {"config": name, "ok": False}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    quality = report.get("quality", report)
    ratios = quality.get("ratios", {})
    flags = quality.get("counts", {}).get("flagged", {})
    lat = quality.get("latency", {}).get("turn_ms", {})
    return {
        "config": name, "ok": True,
        "distinct_2": ratios.get("distinct_2"),
        "distinct_1": ratios.get("distinct_1"),
        "exact_rep": ratios.get("exact_repetition"),
        "avg_words": ratios.get("avg_words"),
        "asst_register": flags.get("assistant_register"),
        "meta_leak": flags.get("meta_leak"),
        "fallback": ratios.get("fallback"),
        "lat_p95": lat.get("p95"),
        "report": str(report_path),
    }


def _score(row: dict, baseline_lat: float | None) -> float | None:
    """Điểm tổng để tự chọn winner. Cao = tốt. None nếu thiếu số.

    Ưu tiên: đa dạng (distinct_2) ↑, ít lặp (exact_rep) ↓, không giọng AI
    (asst_register/meta_leak) ↓. Phạt nếu latency p95 tệ đi >25% so baseline,
    hoặc avg_words ngoài [6,20] (quá cụt/quá dài).
    """
    d2 = row.get("distinct_2")
    if d2 is None:
        return None
    score = float(d2)
    score -= 0.5 * float(row.get("exact_rep") or 0.0)
    score -= 0.05 * float(row.get("asst_register") or 0)
    score -= 0.05 * float(row.get("meta_leak") or 0)
    score -= 0.2 * float(row.get("fallback") or 0.0)
    aw = row.get("avg_words")
    if aw is not None and not (6 <= float(aw) <= 20):
        score -= 0.15
    lat = row.get("lat_p95")
    if baseline_lat and lat and float(lat) > 1.25 * float(baseline_lat):
        score -= 0.15
    return round(score, 4)


def _apply_winner(winner_overrides: dict) -> None:
    _patch_models_yaml(REPO_ROOT / "config", winner_overrides)


def _print_table(rows: list[dict]) -> None:
    cols = ["config", "distinct_2", "distinct_1", "exact_rep", "avg_words",
            "asst_register", "meta_leak", "fallback", "lat_p95"]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print("\n=== SO SÁNH SAMPLING ===")
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    print("\ndistinct_2 cao = đỡ nhạt · exact_rep thấp = ít lặp · asst_register thấp = ít giọng AI")
    print("Số chỉ để LOẠI bớt; chốt bằng đọc operator_review_sample từng report.")


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B sampling trên replay corpus")
    ap.add_argument("corpus", type=Path, help="file replay corpus (.json)")
    ap.add_argument("--sweep", type=Path, default=REPO_ROOT / "config" / "sampling_sweep.yaml")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "logs" / "evaluation" / "sweep")
    ap.add_argument("--only", nargs="*", help="chỉ chạy các config tên này")
    ap.add_argument("--apply", action="store_true",
                    help="tự chọn config điểm cao nhất và patch config/models.yaml")
    args = ap.parse_args()

    all_configs = yaml.safe_load(args.sweep.read_text(encoding="utf-8")).get("configs", {})
    configs = all_configs
    if args.only:
        configs = {k: v for k, v in all_configs.items() if k in args.only}
    if not configs:
        print("không có config nào để chạy")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    rows = [_run_one(name, ov or {}, args.corpus, args.out) for name, ov in configs.items()]
    ok_rows = [r for r in rows if r.get("ok")]
    baseline_lat = next((r.get("lat_p95") for r in ok_rows if r["config"] == "baseline"), None)
    for r in ok_rows:
        r["score"] = _score(r, baseline_lat)
    (args.out / "sweep_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    _print_table(ok_rows)

    scored = [r for r in ok_rows if r.get("score") is not None]
    if scored:
        winner = max(scored, key=lambda r: r["score"])
        print(f"\n>>> WINNER theo điểm: {winner['config']} (score={winner['score']})")
        print("    (điểm chỉ để gợi ý — ĐỌC operator_review_sample của winner + top 2 trước khi tin)")
        if args.apply:
            _apply_winner(all_configs.get(winner["config"], {}))
            print(f">>> ĐÃ patch config/models.yaml theo '{winner['config']}'.")
            print("    Chạy scripts/sample_conversation.py để nghe thử, không ưng thì revert git.")
    print(f"\nSummary: {args.out / 'sweep_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
