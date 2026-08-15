# 14 — Director V2 shadow (Phase 6 design)

> **Status:** implemented and regression-verified offline; shadow-only, no controlled takeover.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`); no product version change is implied.

## Purpose and boundary

Phase 6 adds a deterministic Director V2 **shadow**. It observes bounded snapshots and produces a typed proposal plus a structured, bounded record. The current `Director` and `DirectorLoop` remain the only production decision and delivery path.

```text
bounded runtime snapshots
→ HardArbiter → CandidateGenerator → CandidateScorer → SoftPolicy
→ ActionValidator → shadow proposal + structured record
```

The shadow service never reserves an `ActionTransaction`, calls an executor, emits a World event, mutates a goal/thread/pool, invokes an LLM, or calls TTS. Its proposal is not passed to the legacy Director, prompt construction, delivery boundary, or operator mutation endpoints.

## Inputs and deterministic decision order

The service consumes only read-only views of:

- current `WorldSnapshot`, `SelfSnapshot`, capability snapshot and transaction snapshot;
- bounded chat candidates, active goals, threads and verified-world evidence supplied by a context provider;
- proactive material supplied by a context provider.

Candidate sources are `chat`, `thread`, `goal`, `world`, `capability`, `proactive`, and the always-valid `WAIT`. Candidate labels and evidence references are bounded before record retention.

Hard arbitration wins before any soft score, in this exact order:

1. emergency;
2. operator hold;
3. safety hold;
4. permission/capability rejection;
5. transaction conflict;
6. critical-state hold;
7. donation;
8. normal deterministic candidate scoring.

`ActionValidator` only accepts a capability action when the registry marks it `available`; malformed or unavailable candidates become a `WAIT` proposal with a reason code. Scoring is stable: score descending, then configured source priority, then candidate ID. `SoftPolicy` may apply only YAML-configured additive source weights; it cannot override a hard decision.

## Runtime and feature safety

`director_v2_shadow` depends on `world_model_shadow`, `self_model_projection` and `capability_registry`. It is composed as an idle/read-only service in `StreamRuntime`; normal runtime lifecycle starts/stops it and exposes a snapshot and metrics. Disabling it clears retained shadow records and leaves the V1 Director untouched.

All retention, source limits, source weights and labels are configured in `config/director.yaml::director_v2_shadow`; no decision threshold or bound is hardcoded as production policy.

## Records, observability and replay

Each recorded proposal contains only structured fields: context/snapshot identifiers, candidate summaries, selected action, reason codes and evidence references. It never records chain-of-thought or raw viewer content. The record store is bounded by `max_recent_records`.

Metrics count proposals by outcome (`selected`, `hard_hold`, `validation_rejected`) and expose retained-record count. A fixed context plus fixed configuration must yield byte-equivalent proposal content across replay runs.

## Verification gates

- hard priorities dominate all soft candidates;
- each required source can be represented without mutating its owner;
- unavailable/malformed action candidates are rejected to `WAIT`;
- ties and replay are deterministic;
- records/evidence remain bounded;
- the runtime boundary proves the shadow is not passed to legacy `Director`/`DirectorLoop`, executor or transaction code;
- existing Director and delivery regression tests remain green.

## Rollback

Set `features.director_v2_shadow.enabled: false`. The service stops recording and is omitted from the runtime snapshot; the legacy Director behavior is unchanged because it never consumes shadow output.
