# Phase 10 — Perception expansion

## Scope

`PerceptionIngress` is the only Phase 10 canonical observation ingress.  It maps
existing `InputEvent` values into immutable `PerceptionEvent` values and keeps a
bounded local history for diagnostics. It is feature-gated by
`perception_expansion` and records accepted, rejected and disabled outcomes.

The current implementation wires only the legacy chat compatibility adapter and
the pre-existing structured `ENVIRONMENT_OBSERVED` bridge. OBS, STT, vision and
game adapters are deliberately not implemented or connected.

## Safety boundary

- `ChatRouter` submits input through an activity listener; it never invokes the
  Director through this ingress.
- Raw chat, donation and free text cannot update the World Model.
- Only a structured grounded environment observation can pass to World shadow.
- World state remains read-only for the Director's live legacy path.

## Configuration and rollback

Bounds and permitted input sources live in `config/agent_state.yaml`. Turning
off `perception_expansion` immediately stops collection and World bridge
forwarding; it does not change chat delivery or the Director path.

## Verification

Run `tests/unit/test_perception_ingress.py`, then chat/world-model and replay
regressions. The test suite asserts input sanitization, source rejection,
bounded retention, feature rollback, and that raw chat cannot write world state.
