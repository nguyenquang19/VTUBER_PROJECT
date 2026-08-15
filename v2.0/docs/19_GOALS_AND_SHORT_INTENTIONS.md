# 19 — Goals and short intentions (Phase 11)

> **Status:** implemented; pending user review/commit.
>
> **Applies to:** Mai `1.4.3` (baseline `1.0.0`)

`GoalManager` remains the sole mutable owner. A goal has one to three ordered, bounded steps; it is not a planning tree. The Director receives the active goal through its existing `GoalSnapshot` input.

LLM output may only propose a goal. `GoalManager` validates evidence and activates it deterministically. Delivery/action failure calls the deterministic `fail()` transition; it never invents a successor or retries autonomously. TTL pruning and terminal history bounds remain config-owned.