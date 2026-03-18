# Evolution Strategy

## Goal

This project is moving toward a Python coding-agent stack with a reusable core engine plus a thin application layer.

The long-term core-engine target is a more general agent runtime:

- state machine
- event stream
- provider/tool loop
- snapshot/restore
- explicit context transformation seams
- explicit internal-message-to-provider conversion seams

## Near-term priority

The current product priority is to land the infrastructure for a self-evolving agent.

In this project, self-evolution does not mean rewriting the core decision loop. It means evolving higher-level artifacts that the core can already load and execute, such as:

- prompts
- skills
- templates
- tools
- extensions, including extension code when that is part of the artifact surface

## Layering

Target architecture:

- `core engine`
  - owns state machine, event stream, provider/tool loop, snapshot/restore, and generic transformation seams
- `application/orchestration layer`
  - owns self-evolution policy, experience persistence, artifact generation/selection policy, retrieval/injection policy, and other product semantics
- CLI
  - owns config loading, session persistence, terminal I/O, and built-in interactive commands

Current implementation:

- `agent-core` still contains some product semantics such as skill/template behavior
- that should be treated as a transitional state, not the desired long-term boundary

## Design guidance

- Do not treat self-evolution as permission to mutate core logic by default.
- Prefer evolving artifacts over changing the core loop.
- Prefer adding narrow hooks to the core over embedding more fixed product policy into `src/astra/agent.py`.
- Add generic hooks only when they directly help separate engine behavior from self-evolution policy.
