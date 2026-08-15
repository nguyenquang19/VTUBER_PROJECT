# 09 — Compatibility contracts

> **Applies to:** V2 Phase 1 work in progress; inherited runtime Mai `1.4.3`
>
> **Status:** Implemented and verified in the V2 working tree; awaiting user review. Nothing in this document is wired into the production decision path yet.

## 1. Purpose and boundary

Phase 1 introduces immutable V2 boundary values without changing `StreamRuntime`, `DirectorLoop`,
`ActionTransactionManager`, or any live adapter. The contracts live in `interfaces/compatibility.py` and are
plain data plus explicit compatibility mappers. They do not create a second composition root, feature flag,
metric, YAML file, executor, reducer, or persistence path.

All timestamps are normalized to timezone-aware UTC. Every mapping/collection exposed by a contract is
immutable to callers. Contract serialization produces JSON-safe dictionaries and never serializes credentials.

## 2. Contract set

| Contract | Role in Phase 1 | Runtime owner later |
|---|---|---|
| `PerceptionEvent` | sanitized observation boundary | Perception adapter / World Model |
| `StateValue` | one current belief with provenance, confidence and freshness | World Model |
| `WorldSnapshot` | read-only grouped current belief | World Model |
| `SelfSnapshot` | read-only projection of existing runtime state | Self Model projection |
| `Capability` | declared action/executor/verifier contract | Capability Registry |
| `CapabilityAvailability` | deterministic availability result, never LLM-authored | Capability Registry |
| `ActionRequest` | typed request before execution | Director V2 |
| `ActionResult` | executor result; `SUCCESS` alone is not verified truth | Executor / verifier |

`EventProvenance` is also defined in the compatibility module as the V2-owned nested value for
`PerceptionEvent`. Existing `services.agent.types.EventProvenance` remains unchanged; the compatibility
adapter copies its public values at the boundary. Phase 1 deliberately does not move that existing type.

## 3. Validation and privacy rules

- Required identifiers and textual codes are non-empty after trimming.
- Confidence is finite and constrained to `[0, 1]`.
- UTC normalization rejects naive timestamps rather than silently assuming a timezone.
- `ActionStatus` is a closed enum: `success`, `failed`, `rejected`, `timeout`, `cancelled`, `unknown`.
- `ActionResult.completed_at` cannot precede `started_at`.
- Contract payloads are recursively frozen. The Phase 1 mapper receives explicit payload size limits rather
  than hiding a production threshold in code; it rejects an over-limit input.
- The `InputEvent` mapper retains content and sanitized metadata only. It does not copy raw viewer identity,
  credential-like keys, or secret-like values into the V2 payload.
- Serialization is an in-memory boundary operation only. Phase 1 does not persist event payloads, snapshots,
  or action values.

## 4. Compatibility mapping

| Existing value | Mapper | V2 value | Meaning |
|---|---|---|---|
| `interfaces.input.InputEvent` | `perception_event_from_input` | `PerceptionEvent` | creates a sanitized observation without changing chat routing |
| `services.agent.types.EventProvenance` | `EventProvenance.from_legacy` | V2 `EventProvenance` | copies public provenance fields only |
| `interfaces.action_transaction.ActionTransaction` | `action_request_from_transaction` | `ActionRequest` | supplies the original action/idempotency key; caller must state capability and policy |
| `interfaces.tts.TTSDeliveryResult` | `action_result_from_tts_delivery` | `ActionResult` | records delivery result for compatibility only; no transaction is committed by the mapper |

`action_result_from_tts_delivery` reports `verified=true` only when the existing typed TTS result says
`delivered=true`; this verification applies to the speech delivery boundary, not an unverified external world
action. No mapper invokes an executor, changes a transaction, writes a journal, or updates a world belief.

## 5. Tests and acceptance

`tests/unit/test_compatibility_contracts.py` covers validation, deep immutability, UTC normalization,
JSON-safe serialization, invalid confidence/status/timestamp isolation, explicit bounded payload rejection,
and compatibility mapping from existing `InputEvent`, transaction and TTS delivery values. Existing
`test_interfaces.py`, `test_runtime_boundaries.py`, Director transaction tests, and replay tests remain the
regression proof that production behavior is unchanged.

Verification evidence: the dedicated compatibility suite passed 13 tests; targeted contract, transaction,
TTS delivery and deterministic replay regression passed 165 tests; the offline CI profile passed 1,830 tests
with 5 live/slow tests deselected and one upstream deprecation warning. The source-boundary test confirms
that none of the new mappers are imported by the production runtime composition path.