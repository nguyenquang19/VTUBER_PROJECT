# 10 — World Model shadow (Phase 2 design)

> **Status:** implemented Phase 2 shadow capability; it remains outside production decision and prompt paths.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`); no product version change is implied.

## Purpose and non-goals

Phase 2 introduces a bounded, in-memory **World Model** that reduces explicit environment observations into an immutable `WorldSnapshot`. It is a shadow capability: it is observable through metrics and the read-only operator dashboard, but it must not change routing, Director scoring, prompt construction, LLM generation, TTS, transaction commit, action execution, or persisted data.

This phase does not create an environment/game adapter, infer facts from chat or LLM text, create a Self Model, write history, or expose dashboard controls that mutate World Model state.

## Ownership and contracts

| Owner | Responsibility |
|---|---|
| `interfaces/world.py::WorldModelService` | Public lifecycle plus `apply_event(event)`, `snapshot()`, `query(path)`, and `evict_stale(now)` contract. |
| `services/world/world_model.py::WorldModelShadow` | Deterministic reducer, validation, deduplication, TTL eviction, conflict resolution, bounded state and metrics. |
| `interfaces/compatibility.py` | Existing immutable `PerceptionEvent`, `StateValue`, `WorldSnapshot`, and provenance contracts. |
| `orchestrator/stream_runtime.py` | Sole composition/lifecycle owner; attaches a fail-isolated shadow listener and never supplies snapshot data to a decision path. |
| `orchestrator/runtime_operations.py`, `dashboard/dashboard_server.py` | Read-only snapshot transport only. |

The service accepts only `PerceptionEvent`; no other subsystem reaches into its private state. `snapshot()` and `query()` return immutable contract values. Runtime code may use a narrowly-scoped bridge from an existing grounded environment observation to `PerceptionEvent`, but the bridge must reject every non-environment event and every payload outside the schema below.

## Accepted event schema

Only `event_type: world.observation` is eligible. Its payload has exactly these logical fields:

| Field | Rule |
|---|---|
| `path` | Non-empty dotted path with an allowlisted first segment configured by `world_model.allowed_domains`. |
| `value` | JSON-safe, bounded, no sensitive/identity key; it is copied into the immutable state value. |
| `evidence_refs` | Optional bounded sequence of non-empty opaque references; never raw chat text or user identity. |

`source`, `confidence`, timestamp and provenance come from the immutable event envelope. An incoming event cannot choose its own authority or TTL. The bridge may emit only when an existing `ENVIRONMENT_OBSERVED` event has a structured `state_path` and `value`; chat, donation, LLM output and free text are ignored. Invalid input returns a rejected outcome, increments a metric and leaves the existing snapshot untouched.

## Reducer rules

All time is timezone-aware UTC. The service receives an injectable clock so replay and tests are deterministic.

1. A dedup key is retained in a bounded TTL cache. A duplicate event is ignored without changing state.
2. Source authority is looked up from YAML. Unknown sources are rejected; authority is never taken from payload.
3. A new path is accepted. For an existing fresh path, higher authority wins; equal authority accepts only a strictly newer event timestamp. Equal timestamp retains the existing value. Lower authority and older equal-authority values are rejected deterministically.
4. Every accepted value gets the configured TTL. `evict_stale(now)` removes expired values and returns the count. `snapshot()` and `query()` exclude stale values even if a caller has not explicitly evicted them.
5. `max_state_entries`, `max_evidence_refs`, payload limits and dedup-cache limits are YAML-owned. If the state is full, the reducer rejects the new path rather than silently discarding a fresh fact.

`WorldSnapshot` is bounded, ordered by path for stable replay, carries a monotonically increasing revision-derived snapshot id, and explicitly reports no value for domains that have no adapter. “Unknown” is not fabricated as a world fact.

## Configuration and feature gate

`config/agent_state.yaml` owns the `world_model` block, including allowed domains, default TTL, source-authority map, entry/evidence/payload bounds and dedup bounds. Every positive bound and authority value is validated before runtime startup.

`config/features.yaml::features.world_model_shadow` is a zero-VRAM, zero-latency-impact optional feature. `FeatureManager` must attach symmetric `set_enabled`/health handlers. Disabled means no event reduction and an empty read-only shadow snapshot; re-enabling does not replay old runtime traffic.

## Runtime, metrics and dashboard

`StreamRuntime` starts the World Model before the input router can emit and stops it after input has stopped. The listener is wrapped so a World Model failure cannot escape `AgentState` or interrupt the production pipeline. `operations_snapshot()` and `DashboardServer.build_snapshot()` expose the same read-only `world` shape.

Metrics are required and must report real outcomes, at minimum:

- `mai_world_model_events_total{outcome,reason}` for accepted, duplicate, conflict, invalid, disabled and rejected events;
- a gauge for current fresh state entries; and
- a counter for stale evictions.

The existing System panel gains a read-only summary (feature state, fresh entry count, accepted/rejected/evicted counts). It must render unavailable/empty honestly and has no mutation endpoint.

## Verification and release boundary

Required tests are offline and use a fixed UTC clock:

1. event-to-state, immutable snapshot/query and source provenance;
2. duplicate, TTL/staleness and explicit eviction;
3. authority/recency conflict cases and uncertainty/confidence retention;
4. path, payload and sensitive-key rejection with state isolation;
5. entry/evidence/dedup bounds and metric outcomes;
6. feature disabled/enabled behavior and listener failure isolation;
7. composition/dashboard snapshot tests proving no World Model value enters router, Director or LLM context;
8. targeted V1 regression and replay where environment observations exist.

Phase 2 is done only when shadow snapshots run without production-decision impact, all above tests pass, and the dashboard shows only actual data. Promotion of World Model data into a decision or Self Model is a later phase and requires a separate contract, review and confirmation.
