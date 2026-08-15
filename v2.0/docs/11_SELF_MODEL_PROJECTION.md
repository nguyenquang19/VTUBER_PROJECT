# 11 — Self Model projection (Phase 3 design)

> **Status:** implemented in Phase 3; offline regression evidence is required before release.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`); no product version change is implied.

## Purpose and non-goals

Phase 3 adds an immutable `SelfSnapshot` projection of what the running system is actually doing. It is an observation surface for dashboard, metrics and later capability evaluation. It is not a new agent state, scheduler, intention store, action owner or decision input.

The projection must not mutate or duplicate mood, goals, threads, transactions, delivery state, avatar state or health. It does not create an intention lifecycle, action executor or any Phase 4+ capability logic.

## Ownership and contract

| Owner | Responsibility |
|---|---|
| `interfaces/self_model.py::SelfModelService` | Lifecycle plus `snapshot()` projection contract. |
| `services/self_model/projection.py::SelfModelProjection` | Pure bounded assembly from public source snapshots only. |
| `interfaces/compatibility.py::SelfSnapshot` | Existing immutable V2 wire contract. |
| `orchestrator/stream_runtime.py` | Sole composition/lifecycle owner; it must not pass SelfSnapshot to Director, router or prompt construction. |
| `dashboard/dashboard_server.py` | Read-only transport to the operator panel. |

`SelfModelProjection` may retain lifecycle/feature-enable state and counters for metrics, but it may not retain a second mutable copy of any projected domain value. Each `snapshot()` reads public source APIs at that instant.

## Source mapping

| `SelfSnapshot` field | Authoritative existing source | Rule |
|---|---|---|
| `speaking` | `AudioPlayer.is_playing` | `true` only during actual playback. Missing audio player is `false`, never guessed from generated text. |
| `busy` | current non-terminal action transaction or `speaking` | `reserved`, `generated` and `delivering` transactions are active; terminal transactions are not busy. |
| `degraded` | health-supervisor snapshot and enabled animation connection | `true` only for actual degraded/unhealthy source health or enabled-but-disconnected animation. Missing optional sources are not fake failures. |
| `current_action_id` | latest active action transaction | Transaction ID, otherwise `null`. |
| `current_intention_id` | none in Phase 3 | Always `null`; intention lifecycle is later scope. |
| `active_goal_id` | `GoalManager.snapshot().active` | Goal ID, otherwise `null`. |
| `focused_thread_id` | `AgentStateSnapshot.open_threads` | Most recently updated open thread; ties break by thread ID. |
| `current_topic` | `AgentStateSnapshot.current_topic` | Current bounded topic summary, otherwise `null`. |
| `attention_target` | none in Phase 3 | Always `null`; no inferred attention policy. |
| `avatar_state` | animation public state/metrics | Bounded `enabled` and `connected` values only. |
| `recent_action_ids` | action-transaction public snapshot | Most-recent IDs, bounded by YAML; no private transaction access. |

`snapshot_id` is a deterministic hash of the projected values, not a mutable revision counter. All source absence is represented as an empty/null contract value.

## Configuration, feature and metrics

`config/agent_state.yaml::self_model` owns `max_recent_action_ids`, the sole Phase 3 projection bound. It is validated positive on construction.

`config/features.yaml::features.self_model_projection` is a zero-VRAM, zero-latency optional feature. It has symmetric FeatureManager handlers. When disabled, the service returns an empty, explicit projection and does not fabricate a healthy/busy state.

Metrics are real projection observations only:

- `mai_self_model_snapshots_total{outcome}` for successful or isolated-failure projections;
- `mai_self_model_degraded` gauge for the latest projection; and
- a gauge for the bounded recent-action count.

## Runtime and dashboard boundary

Runtime starts the projector before the input router and stops it after input has stopped. The projector is composed with public source providers for AgentState, GoalManager, ActionTransactionManager, AudioPlayer, animation and health supervisor. Provider failures are isolated: the affected field becomes unavailable/empty and `degraded=true`; they must not interrupt the live pipeline.

`operations_snapshot()` and `DashboardServer.build_snapshot()` expose `self` read-only. The existing operator UI gains a Self Model card showing speaking, busy, degraded, current action/goal/thread and recent actions. It has no mutation endpoint. `ChatRouter`, `DirectorLoop`, prompt/context building, TTS and transaction commit must not read `SelfSnapshot` in this phase.

## Verification and completion gate

Required offline tests use fixed source snapshots and clocks:

1. immutable projection and deterministic snapshot ID;
2. source changes reflected without duplicated ownership;
3. active/terminal transaction and actual audio playback semantics;
4. bounded recent actions and stable thread tie-break;
5. degraded/unavailable source handling and feature toggle behavior;
6. metrics and dashboard read-only snapshot;
7. runtime static/integration boundary proving no SelfSnapshot reaches Director or prompt paths;
8. impacted V1 regression suite.

Phase 3 is complete only when `SelfSnapshot` is accurate, bounded, immutable and has no parallel mutable domain state.
