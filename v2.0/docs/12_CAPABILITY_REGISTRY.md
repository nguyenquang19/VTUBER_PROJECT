# 12 — Capability, permission and health registry (Phase 4 design)

> **Status:** implemented in Phase 4; offline regression evidence is required before release.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`); no product version change is implied.

## Purpose and boundary

Phase 4 adds a deterministic, read-only answer to: *which declared action is permitted and healthy enough to be considered now?* It is a shadow registry for the dashboard and later Director V2 candidate generation. It does not execute, reserve, commit, roll back, mutate the World Model, mutate the Self Model, or change the current Director/LLM prompt path.

`WAIT` is always declared and can be available without an LLM. All other results are deny-by-default when a declaration, permission, executor-health source, verifier registration, transaction state, World precondition or Self precondition is absent or invalid.

## Ownership and contracts

| Owner | Responsibility |
|---|---|
| `interfaces/capability.py::CapabilityRegistryService` | Lifecycle, registration and immutable availability/snapshot API. |
| `interfaces/compatibility.py::{Capability,CapabilityAvailability}` | Existing immutable cross-subsystem data contracts; do not redefine them. |
| `services/capability/registry.py::CapabilityRegistry` | Pure deterministic evaluation from declarations plus public World, Self, transaction and health snapshots. It owns no action state. |
| `config/capabilities.yaml` | Declarative capability IDs, permissions, executor/verifier IDs, preconditions, mock-only status and registry bounds. |
| `config/features.yaml::capability_registry` | Optional zero-VRAM/zero-latency shadow feature with `FeatureManager` handlers. |
| `orchestrator/stream_runtime.py` | Sole composition/lifecycle owner; supplies snapshot providers and read-only dashboard transport only. |
| `dashboard/dashboard_server.py` | Read-only AVAILABLE/BLOCKED view; no execution or permission mutation endpoint. |

The registry must not call Director, ChatRouter, `LLMTurnRunner`, TTS, animation or an external API. A caller receives only a `CapabilityAvailability`, never an executable callable. Action validation/reservation and executor dispatch stay in Phase 5.

## Initial declarations

The YAML declarations are the only capability source; an LLM cannot create IDs, permissions, executor IDs, verifier IDs or preconditions.

| Class | Declared capability IDs |
|---|---|
| Existing-runtime intent | `SPEAK`, `WAIT`, `READ_CHAT`, `SELF_TALK`, `FOLLOW_UP`, `AVATAR_GESTURE` |
| Mock-only, never production external success | `PLAY_MUSIC`, `STOP_MUSIC`, `SWITCH_SCENE`, `CALL_GUEST`, `REMOVE_GUEST` |

Mock-only means the registry may report the capability declaratively eligible if all registry inputs pass, but it cannot imply that an external side effect happened. Phase 5 supplies the mock executor and verifier loop; Phase 9 is the earliest scope for a real external executor.

## Deterministic availability order

For one capability, evaluation stops at the first blocking condition and returns one stable reason code:

1. registry feature enabled and capability declaration exists: `feature_disabled` / `unknown_capability`;
2. every declared permission is present in the runtime permission set: `permission_denied`;
3. executor health is explicitly `healthy`: missing, unknown, degraded, unhealthy or stopped is `executor_unhealthy`;
4. the named authoritative verifier is registered: `missing_verifier`;
5. no configured non-terminal transaction conflicts: `transaction_conflict`;
6. configured public World predicates match: `world_precondition_failed`;
7. configured public Self predicates match: `self_precondition_failed`;
8. otherwise `available`.

Only stable identifiers and source snapshot IDs/paths are emitted as bounded `evidence_refs`; raw chat, prompt content and chain-of-thought are forbidden. `checked_at` comes from an injected UTC clock. The registry stores no mutable World/Self/action copy and recomputes from the providers for each request.

## Health, permissions and configuration

`capabilities.yaml` owns all declaration data and bounds, including the maximum dashboard/history entries and each executor-to-health target mapping. The runtime permission set is configuration-owned and defaults to an empty set. Permission grants for mock/external operations therefore need an explicit YAML change and are not editable through the dashboard.

Health is obtained through public `HealthSupervisor.snapshot()` target status or a registered local health provider. A target not yet observed is `unknown` and blocks an action. A verifier must be explicitly registered; executor health alone is insufficient. Existing V1 services remain their own lifecycle owners; the capability registry only reads their public health.

Metrics are observations, not action outcomes:

- `mai_capability_availability_checks_total{reason_code}`;
- a gauge for declared capabilities and currently available capabilities; and
- a bounded registry snapshot metric/counter for diagnostics.

## Runtime, dashboard and Phase 5 boundary

`StreamRuntime` starts/stops the registry with the Phase 2/3 read-only services, registers the feature toggle, and exposes its snapshot to `operations_snapshot()` and `DashboardServer.build_snapshot()`. It must not pass the registry or `CapabilityAvailability` into the current Director, router, prompt builder or existing speech transaction path.

The operator dashboard displays capability ID, availability, reason code, mock-only label and bounded evidence references. It is read-only. There is no execute button, permission editor, action request endpoint or automatic retry in Phase 4.

Phase 5 may consume the same declarations to build `ActionRequest → validate → reserve → mock executor → verify → commit/rollback`; it must not change a Phase 4 availability result into a claimed action success without authoritative verification.

## Verification and completion gate

Required offline tests use fixed snapshots, a fixed UTC clock and fake health/verifier registrations:

1. correct availability changes for World and Self preconditions;
2. deny-by-default permission rejection;
3. unhealthy/unknown executor rejection;
4. missing verifier rejection;
5. non-terminal transaction conflict rejection;
6. unavailable and unknown actions rejected with stable reason codes;
7. immutable/bounded snapshot and metric counters;
8. read-only dashboard projection and static runtime boundary proving the registry does not reach Director/prompt code;
9. YAML/FeatureManager loading plus impacted V1 regression.

Phase 4 is complete only when the registry can answer which declared action is available now, deterministically and without consulting an LLM or executing anything.
