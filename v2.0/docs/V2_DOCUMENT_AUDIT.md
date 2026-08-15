# V2 documentation audit and Phase 0 closure

> **Audit date:** 2026-08-15
>
> **Scope:** V1 archive integrity, V2 working-tree routing, version/config inventories,
> feature disposition, rollback boundary, and Phase 0 verification evidence.

## Canonical layout

| Location | Ownership | Status |
|---|---|---|
| `ver/v1.0/` | Frozen V1 snapshot | Baseline, regression, rollback and reference only; never edit from V2 work. |
| `v2.0/` | Active implementation | The only working tree for the V2 blueprint. |

The canonical blueprint is `v2.0/MAI_V2_MASTER_IMPLEMENTATION_BLUEPRINT_v2.0.md`.
Its Phase 0 is repository stabilization and V1 closure. Directory generation (`v2.0`),
blueprint version and product version are independent. The inherited product remains
Mai `1.4.3` until an accepted release change updates the configuration, changelog and
release evidence together.

## Snapshot integrity

- Git-tracked V1 source checked against `HEAD`: 470 product-source files matched byte-for-byte.
- One missing `.claude` worktree pointer is excluded metadata, not product source.
- Models, virtual environments, logs, runtime data, backups, caches and secrets are not
  tracked as V1 source and are not copied into V2.
- `v2.0/config/system.yaml` remains compatible with the frozen V1 product configuration.
  Phase 0 preserves product routing and feature behavior; it only makes optional adapter dependencies
  fail safe for offline verification.

## Capability and toggle disposition

The production, optional and interface-only capability matrix is frozen in
[00 — V1 baseline](00_V1_0_BASELINE.md). `config/features.yaml` is the authoritative
runtime toggle inventory; the documentation guard verifies its enabled/disabled sets
against the baseline. Disabled toggles remain optional or deferred—they are not evidence
of a production V2 capability. Real game/environment actions remain interface/context
only; no mock is promoted to production by this closure.

## V1 rollback boundary

- `ver/v1.0/` is the rollback reference; no V1 fallback or V1 decision path is removed.
- `v2.0` retains the existing `StreamRuntime` composition root, Director delivery boundary
  and llama.cpp backend.
- Production entry remains `scripts/start_live.ps1`; `orchestrator.main` remains a
  fail-fast compatibility shim.
- Phase 0 makes no feature-flag, threshold, schema, metric, model or Director runtime-decision change.

## Verification evidence

- Phase 0 documentation/config/entrypoint/dashboard/evaluation/replay regression: 71 passed.
- Optional-adapter and audio dependency regression after stabilization: 48 passed.
- Full offline CI profile (`not llm and not slow`): 1,817 passed, 5 deselected, 1 upstream
  deprecation warning (65.52 s).
- Live llama.cpp lifecycle test: 1 passed (22.44 s) with the configured GGUF in the fresh V2 environment.
- `pip check` passed after recreating the V2 Python 3.11 CI environment. The 5 deselected tests are
  live/slow coverage and remain outside this Phase 0 offline claim.

## Phase 0 exit rule

Phase 1 may begin only after review of this closure. It must add compatibility contracts
without changing Director production behavior, preserve the V1 fallback and follow the
same docs-first, targeted-regression and replay gates.
