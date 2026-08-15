# 17 — External executor framework (Phase 9)

> **Status:** implemented inert framework; the runtime starts an empty registry and no external executor is registered or callable.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`)

## Scope

This phase creates the typed registry that later OBS, media, call/guest and game/environment adapters will use. It does not compose a client, read a credential, open a network connection, declare a new capability, or expose an action to the Director.

Each future binding must supply both an `ActionExecutor` and an `ActionVerifier`, a feature ID and a health source. The registry is a routing and diagnostics boundary only: it never executes an action, retries, commits a transaction, changes World state or turns an executor claim into verified success.

## Required future integration

Before a real adapter is registered, its phase must add a feature flag, YAML timeout/retry bounds, permissioned capability declaration, independent verification source, metrics and failure/rollback tests. An unavailable or unverified action remains unavailable or unverified; it must not be represented as a completed external action.

## Current safety properties

- empty registry is healthy and inert;
- duplicate executor/verifier IDs are rejected deterministically;
- a lookup for an unregistered ID returns no callable service;
- snapshots contain only bounded identifiers and counts, never credentials or raw request data.
