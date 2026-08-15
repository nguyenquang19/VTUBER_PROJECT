# 16 — Speech and avatar action adaptation (Phase 8)

> **Status:** implemented; both adapters are disabled by default and retain the legacy path as immediate rollback.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`)

## Purpose and ownership

Phase 8 adapts the already-running speech and VTube Studio paths to the typed action boundary. It does not add a second TTS pipeline, transaction manager, Director loop, avatar scheduler or embodiment policy.

`DirectorLoop` remains the owner of reservation, generated/delivering/delivered transaction states, business commit/release and runner delivery finalization. The speech action executor calls the existing `TTSPipeline` callback exactly once. Its verifier evaluates the returned typed `TTSDeliveryResult`; only verified delivery permits the existing loop to mark delivered and commit.

The avatar gesture executor sends one configured intentional gesture through VTube Studio. Verification means the VTS API acknowledged the hotkey trigger, not that a camera/image analysis proved the avatar animation completed. Automatic mood expression remains the existing post-delivery cosmetic path and is not represented as a Director action. Embodiment arbitration, overlapping gestures and policy selection remain Phase 13 scope.

## Boundaries

| Adapter | Executor input | Authoritative verification | May commit business state? |
|---|---|---|---|
| Speech | typed `ActionRequest` with bounded generated text | `TTSDeliveryResult.delivered` with every sentence delivered by audio/subtitle | No |
| Avatar gesture | typed `ActionRequest` with configured gesture ID | VTube Studio hotkey API acknowledgement | No |

Missing callback, exception, malformed delivery value, partial delivery and cancellation are failures. Subtitle-only and mixed delivery remain verified degraded successes when all sentences reached a sink. VTS unavailable, disconnected, unconfigured or rejected gestures fail safe and are never recorded as verified success.

## Feature and configuration

`speech_action_adapter` and `avatar_action_adapter` are independent `FeatureManager` features. Both default to disabled and are the immediate rollback switches. Their dependencies and operational limits live in the existing `features.yaml`, `capabilities.yaml`, `models.yaml` and `animation.yaml`; this phase adds no new YAML file.

Intentional gesture IDs are allowlisted in `animation.intentional_gesture_hotkeys`. Mood-to-hotkey configuration remains separate under `animation.mood_hotkeys`.

## Observability

The adapters record bounded outcome counters for execution, verification and rejection/degraded failure. Existing TTS delivery and VTS health metrics remain the source for device-level diagnostics; the new counters only describe action-boundary adaptation.

## Verification gates

- subtitle degraded success is verified without changing legacy transaction semantics;
- missing or malformed delivery callback, partial sentence failure and cancellation release rather than commit;
- duplicate idempotency keys do not invoke speech delivery twice;
- VTS degraded/unavailable state fails safe without a verified gesture;
- automatic mood expression never appears as an intentional action;
- feature disable immediately returns the legacy speech path and disables intentional gesture execution.
