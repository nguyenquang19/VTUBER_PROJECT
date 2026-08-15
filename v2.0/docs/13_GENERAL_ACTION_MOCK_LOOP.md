# 13 — General action mock closed loop (Phase 5)

> **Status:** implemented and regression-verified offline; mock-only, not externally connected.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`); no product version change is implied.

## Purpose and boundary

Phase 5 proves one typed non-conversation action can complete a closed loop without changing the current Director, speech delivery path, prompt construction or any real external system. The only implemented mock capability pair is `CALL_GUEST` / `REMOVE_GUEST`. The existing declarations for media and scene remain unavailable until an executor is explicitly registered.

The loop is:

```text
ActionRequest → validate availability/schema → reserve transaction
→ MockExecutor → ActionResult → authoritative mock verifier
→ verified World event → commit, or release on every failure/unknown path
```

`MockExecutor` is not a production call/media/OBS executor. Its isolated backend is the only simulated external authority and is used solely by deterministic offline tests. Phase 9 is the earliest scope for an external integration.

## Ownership and contracts

| Owner | Responsibility |
|---|---|
| `interfaces/action_execution.py` | Typed `ActionExecutor`, `ActionVerifier` and closed-loop coordinator contracts. |
| `interfaces/compatibility.py::{ActionRequest,ActionResult}` | Existing immutable request/result wire contracts; no duplicate DTO. |
| `services/action/mock_backend.py` | Private bounded simulated guest connection authority; executor writes it only after configured mock success, verifier reads it independently. |
| `services/action/mock_loop.py` | Generic validation/reserve/execute/verify/world-update/commit-or-release coordinator; bounded result/idempotency cache. |
| `services/director/action_transaction.py` | Existing transaction owner; no new transaction state machine. Compatibility mapping is `reserved → generated → delivering → delivered → committed`; every reject/failure/unknown path calls `release`. |
| `services/world/world_model.py` | Receives one structured runtime observation only after successful verification; it never selects or executes the action. |
| `config/capabilities.yaml::mock_action` | Mock timeout, result-cache bound and deterministic default outcome. |
| `config/features.yaml::action_mock_closed_loop` | Feature gate, zero VRAM/latency declaration and symmetric `FeatureManager` handlers. |

The coordinator is composed by `StreamRuntime` as an idle shadow service and may expose a read-only result snapshot. It is not passed to the current Director, router, prompt, TTS, animation or operator mutation endpoint.

## Validation and idempotency

Validation is deterministic and occurs before reserve:

1. feature enabled and request schema version supported;
2. request `capability_id`, `action_type` and `transaction_policy` exactly match the registry declaration;
3. registry returns `available` at execution time;
4. argument keys/types satisfy the declared bounded schema;
5. an executor and verifier are registered for the declared IDs.

The existing transaction manager owns reservation/idempotency. A duplicate committed idempotency key returns the prior bounded `ActionResult` from the coordinator cache and never invokes the executor again. The cache size is YAML-bounded; a cache miss for an old duplicate is rejected rather than re-executed.

## Mock call state and verification

`CALL_GUEST` requires a non-empty `guest_id`; the mock executor marks that guest connected in its private mock backend only for configured success. The mock verifier independently queries the backend. A verified call then emits a structured World Model observation `call.guest_connected=true` with the action ID as provenance/evidence. `REMOVE_GUEST` is symmetric and emits `false`.

The World event must be accepted before the transaction is marked delivered/committed. If executor failure, timeout, verifier false/unknown, malformed result or World update failure occurs, the coordinator releases the reservation, returns a non-verified `ActionResult`, emits no successful World event and writes no success memory/goal/thread state.

The runtime YAML intentionally does **not** grant `call.control`; tests use an explicit local fixture permission grant to prove the mock loop. This preserves deny-by-default behavior in a live process.

## Observability and dashboard

Metrics record action-loop outcomes using stable reason codes, including `validated`, `rejected`, `executed`, `verified`, `released`, `duplicate` and `world_update_failed`. No raw prompt/chat content is recorded. A bounded read-only snapshot exposes action ID, capability ID, status, verified flag, error code and mock-only label. No dashboard execute/retry button is added.

## Verification and completion gate

Required offline tests use a fixed clock, explicit permission fixture and mock backend:

1. `CALL_GUEST(Evil)` validates, reserves, executes, verifies, commits and emits `call.guest_connected=true`;
2. after that event, `CALL_GUEST` is blocked and `REMOVE_GUEST` is available;
3. executor failure, timeout and verifier unknown release the transaction, leave World `false` and do not report success;
4. malformed/unavailable requests reject before execution;
5. duplicate idempotency never executes twice;
6. World update failure releases rather than commits;
7. existing speech transactions retain their delivery semantics;
8. metrics/read-only dashboard snapshot and static current-Director boundary pass;
9. targeted and full V1 regression are green.

Phase 5 is complete only when the mock closed loop is end-to-end verified and no code path can claim an action success before verifier and World update succeed.
