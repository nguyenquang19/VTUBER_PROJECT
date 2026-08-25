"""Build or finalize source-bound MCB-4 offline cognitive A/B evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config_loader import ConfigLoader  # noqa: E402
from services.evaluation.cognitive_ab import (  # noqa: E402
    CognitiveABConfig,
    CognitiveABCorpus,
    CognitiveABEvaluation,
)
from services.evaluation.cognitive_ab_source import (  # noqa: E402
    CognitiveABSourceProducer,
    build_identity,
)
from services.evaluation.release_gate import inspect_source_state  # noqa: E402
from services.llm.llama_cpp_llm import LlamaCppLLMService  # noqa: E402
from services.llm.process_manager import (  # noqa: E402
    LlamaServerConfig,
    LlamaServerProcessManager,
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("cognitive A/B artifact must be a JSON object")
    return value


def _write(path: Path, value: MappingLike) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    os.replace(temporary, target)


MappingLike = dict[str, Any]


def _source_summary(source: MappingLike) -> dict[str, Any]:
    rows = list(source.get("rows") or ())
    compatibility = Counter(str(row["compatibility"]["outcome"]) for row in rows)
    brain = Counter(str(row["brain"]["outcome"]) for row in rows)
    matrix = Counter(
        f'{row["compatibility"]["mode"]}->{row["brain"]["mode"]}' for row in rows
    )
    informative = sum(
        1 for row in rows
        if row["compatibility"]["outcome"] == "COMPLETED"
        and row["brain"]["outcome"] == "COMPLETED"
        and not (
            row["compatibility"]["mode"] == "WAIT"
            and row["brain"]["mode"] == "WAIT"
        )
    )
    return {
        "cases": len(rows),
        "informative_pairs": informative,
        "compatibility_outcomes": dict(sorted(compatibility.items())),
        "brain_outcomes": dict(sorted(brain.items())),
        "mode_matrix": dict(sorted(matrix.items())),
    }


async def _run(args: argparse.Namespace) -> int:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    evaluation = CognitiveABEvaluation.from_loader(loader, repo_root=REPO_ROOT)
    if args.finalize is not None:
        if args.manifest is None or args.private is None or args.output is None:
            raise ValueError(
                "--manifest, --private and --output are required with --finalize",
            )
        final = await evaluation.finalize(
            args.finalize, _read(args.manifest), _read(args.private),
        )
        _write(args.output, final)
        print(json.dumps({
            "output": str(args.output.resolve()),
            "status": final["status"],
            "automatic_release_decision": False,
            "owner_go_no_go_required": True,
        }, ensure_ascii=False))
        return 0

    input_path = args.input
    if args.collect_source is not None:
        if args.input is not None and args.input.resolve() != args.collect_source.resolve():
            raise ValueError("--input and --collect-source must identify the same artifact")
        config = CognitiveABConfig.from_loader(loader)
        if loader.get(
            "features", "features.cognitive_brain_shadow.activation_allowed", None,
        ) is not True:
            raise ValueError("cognitive Brain activation is not allowed by feature policy")
        corpus_path = config.corpus_file
        if not corpus_path.is_absolute():
            corpus_path = REPO_ROOT / corpus_path
        corpus = CognitiveABCorpus.load(corpus_path, config)
        source_state = inspect_source_state(REPO_ROOT)
        identity = build_identity(loader, repo_root=REPO_ROOT, corpus=corpus)
        process = LlamaServerProcessManager(LlamaServerConfig.from_loader(loader))
        service = LlamaCppLLMService.from_loader(loader)
        await process.start()
        await service.start()
        health = await service.health_check()
        if not health.is_ok:
            await service.stop()
            await process.stop()
            raise RuntimeError("llama-server is not healthy on the configured endpoint")

        def progress(done: int, total: int, rows: tuple[dict[str, Any], ...]) -> None:
            checkpoint = {
                "schema_version": config.schema_version,
                "marker": "mai_cognitive_ab_source_checkpoint",
                "generated": done,
                "total": total,
                "rows": list(rows),
            }
            _write(args.collect_source.with_suffix(args.collect_source.suffix + ".checkpoint"), checkpoint)
            print(json.dumps({"generated": done, "total": total}, ensure_ascii=False), flush=True)

        try:
            source = await CognitiveABSourceProducer(
                loader=loader,
                service=service,
                config=config,
                corpus=corpus,
                identity=identity,
                source_revision=source_state.revision,
                source_clean=source_state.clean,
                product_version=str(loader.get("system", "app.version", "")),
            ).collect(progress=progress)
        finally:
            await service.stop()
            await process.stop()
        _write(args.collect_source, source)
        input_path = args.collect_source
        print(json.dumps({
            "source": str(args.collect_source.resolve()),
            "technical_summary": _source_summary(source),
            "source_clean": source["source_clean"],
            "delivery_calls": 0,
            "state_mutations": 0,
        }, ensure_ascii=False), flush=True)
        artifact_paths = (args.private, args.review, args.manifest)
        if all(value is None for value in artifact_paths):
            return 0
        if any(value is None for value in artifact_paths):
            raise ValueError(
                "--private, --review and --manifest must be supplied together",
            )

    if any(value is None for value in (
        input_path, args.private, args.review, args.manifest,
    )):
        raise ValueError(
            "--input (or --collect-source), --private, --review and --manifest are required to build",
        )
    private, review, manifest = evaluation.build(_read(input_path))
    _write(args.private, private)
    _write(args.review, review)
    _write(args.manifest, manifest)
    gate_status = (
        "eligible_pending_human_review"
        if private["source_gate_eligible"] else "diagnostic_dirty_source"
    )
    print(json.dumps({
        "private": str(args.private.resolve()),
        "review": str(args.review.resolve()),
        "manifest": str(args.manifest.resolve()),
        "status": review["status"],
        "gate_status": gate_status,
        "selected_pairs": private["summary"]["selected_pairs"],
        "build_identity_hidden": True,
    }, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument(
        "--collect-source", type=Path,
        help="run both real llama.cpp candidates and write the exact source artifact",
    )
    parser.add_argument("--private", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--finalize", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
