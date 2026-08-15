# 15 — Director V2 controlled conversational takeover (Phase 7)

> **Status:** implemented and regression-verified offline; disabled by default with immediate legacy fallback.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`); no product version change is implied.

## Purpose and safety boundary

Phase 7 permits a strictly bounded conversational takeover path while retaining the existing `Director` and `DirectorLoop` as the production fallback. It does not adapt speech or avatar executors; those remain Phase 8 scope.

The takeover selector receives the legacy `DirectorDecision` and the current Director V2 proposal. It can return the V2-owned decision only when all of these are true:

1. `director_v2_takeover` is enabled;
2. the YAML rollout stage authorizes the decision action;
3. the V2 proposal action is exactly the legacy action after the explicit action mapping;
4. a chat action names a ref that exists in the same bounded `DirectorInput`;
5. the proposal is not a hard hold, validation rejection or unavailable capability result.

Otherwise it returns the original legacy decision unchanged with a structured fallback reason. It never creates a new transaction, prompt, speech request or side effect. Existing `DirectorLoop` continues to own reservation, generation, delivery, commit and release.

## Rollout stages

The configured stage is monotonic and only exposes these action classes:

| Stage | V2-owned action class | Legacy fallback |
|---|---|---|
| `wait` | `WAIT` | all other actions or disagreement |
| `read_chat` | `WAIT`, `READ_CHAT`, `ACK_DONATION` | missing/mismatched chat evidence |
| `self_talk` | prior stage + `SELF_TALK` | no matching V2 proactive proposal |
| `follow_up` | prior stage + `FOLLOW_UP`, `CONTINUE_THREAD`, `ASK_FOLLOW_UP`, `SHARE_GOAL_PROGRESS` | thread/goal mismatch |
| `speech_scheduling` | all conversational actions above | existing delivery boundary on every outcome |

`TRANSITION` is intentionally excluded from Phase 7; it stays with the legacy Director. The production default is `enabled: false`, stage `wait`.

## Runtime and observability

`StreamRuntime` composes `DirectorV2Takeover` after the shadow service and attaches it to the existing `DirectorLoop` through a public setter. No new composition root or parallel delivery loop exists. The loop records `accepted` or a bounded fallback reason; the dashboard exposes only the selector snapshot and metrics.

Metrics count accepted and fallback decisions by stage/reason. Snapshot retention is bounded by `config/director.yaml::director_v2_takeover.max_recent_decisions`; no raw viewer text or chain-of-thought is stored.

## Verification gates

- stage gates reject actions from later stages;
- selected V2 action must agree with legacy decision and evidence;
- duplicate/cancel/delivery-failure paths remain in the unchanged legacy `DirectorLoop` transaction path;
- disabling the feature returns every decision to legacy immediately;
- fixed input/proposal pairs replay deterministically;
- full Director, transaction and delivery regressions remain green.

## Rollback

Set `features.director_v2_takeover.enabled: false`. The selector returns legacy decisions without waiting for a process restart. The shadow may continue to log independently through `director_v2_shadow`.
