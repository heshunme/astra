# Self-Iteration Implementation Plan (Phase 1)

This document defines the first production-safe self-iteration loop for the Python replica.

## Objective

Upgrade `astra` from a tool-calling coding assistant to a single-run self-iteration agent with explicit safety gates:

1. Create a checkpoint
2. Attempt one repository improvement
3. Run acceptance gates
4. Accept changes or revert automatically
5. Persist a structured iteration record

## Scope (Phase 1)

Included:

- `/iterate once [objective]`
- `/iterate status`
- Dirty-worktree preflight checks
- Workspace checkpoint + automatic rollback on failure
- Gate pipeline:
  - `python -m compileall src`
  - `python -m pytest -q tests/unit`
  - `python -m astra --help`
- JSONL run ledger at `.astra/logs/iteration_runs.jsonl`
- Runtime inspection summary fields for latest iteration run

Excluded:

- Multi-run autonomous loops
- Policy learning / strategy tuning
- Provider or tool-safety model changes
- External telemetry services

## Execution Model

Single run state machine:

1. `preflight`
2. `checkpoint`
3. `attempt`
4. `validate`
5. `accept` or `revert`
6. `record`

Failure behavior:

- Any gate failure, timeout, or runtime exception triggers revert.
- Revert restores tracked file contents from checkpoint and removes untracked files created during the run.

## Run Record Schema

Each JSONL line stores one run:

- `run_id`
- `session_id`
- `checkpoint_id`
- `final_decision`: `accepted | reverted | failed`
- `score` (passed_gates / total_gates)
- `changed_files`
- `gate_results[]`
- `failure_class`: `syntax | test | cli | env | timeout | unknown`
- `error`
- `duration_seconds`

## Acceptance Criteria

Phase 1 is complete when:

1. `/iterate once` can execute end-to-end and produce a run record.
2. Failed gate runs revert workspace changes automatically.
3. `/iterate status` reports the latest run from memory or persisted log.
4. `/runtime json` includes latest iteration summary fields.

## Phase 2 (Implemented)

Phase 2 extends the single-run loop with a bounded multi-step orchestrator while keeping Phase 1 safety guarantees.

Included:

- `/iterate auto [objective]`
- Budgeted loop controller:
  - `max_steps=3`
  - `max_reverts=2`
  - `max_total_seconds=900`
- Loop termination rules:
  - stop on first accepted step
  - stop on `env` failure (fail-fast)
  - stop when any budget limit is reached
- Extended run ledger fields:
  - `objective`
  - `loop_id`
  - `loop_step`
  - `loop_final_decision`
  - `loop_stop_reason`
- Runtime inspection summary for latest loop fields via `/runtime json`

Notes:

- Checkpointing remains workspace file snapshot based; no git-commit/branch checkpointing is introduced in Phase 2.
