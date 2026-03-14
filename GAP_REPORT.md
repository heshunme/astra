# Python Replica Gap Report

This document records the current gap between the Python replica in this repository and the original `pi` coding agent in `packages/coding-agent/`.

It is intended to be a living baseline for future convergence work.

## Current Python baseline

The Python implementation currently includes:

- OpenAI-compatible provider integration via configurable `base_url`
- Streaming assistant output
- Tool-calling loop with tool-result replay
- Workspace-scoped built-in tools
- Local JSON session persistence
- Session switch, fork, rename, and save
- Manual runtime reload via `/reload`
- Best-effort module reload via `/reload code`
- Project/global YAML config for a narrow set of runtime options
- Prompt assembly with ordered fragments
- Project/global prompt and skill discovery plus session-scoped activation
- Runtime inspection for assembled prompt, warnings, and active capabilities

This means the Python project already covers the narrow “core coding harness” path, but not the broader product surface of `pi`.

## High-level gap summary

### Already approximated

These areas are present in both projects, though often with much lower scope in Python:

- CLI entrypoint
- Stateful agent loop
- Streaming text responses
- Tool calling and tool result continuation
- Basic session persistence
- Manual runtime reload
- Model selection
- Configurable provider endpoint
- Minimal prompt/skill/template capability runtime
- Runtime inspection for prompt assembly and loaded capability resources

### Major gaps still open

These are the largest remaining differences from the original `pi`:

- Interactive TUI and all terminal UI components
- RPC mode and SDK-oriented process integration
- Provider ecosystem and auth flows
- Extensions and theme systems
- Rich settings/resource loading and reload behavior
- Advanced session management and compaction
- Broader command surface and workflow commands
- HTML export and related utilities
- Package management for pi packages

## Detailed gaps

## 1. UI and operating modes

The original `pi` supports multiple operating modes and a full interactive terminal UI.

Original `pi` includes:

- Interactive mode with custom TUI rendering
- Print mode
- RPC mode
- SDK-oriented integration surface
- UI components for selectors, dialogs, trees, settings, themes, and tool rendering

Python currently includes only:

- A plain REPL-style CLI
- Single-shot prompt mode
- Minimal streamed console output

Missing from Python:

- Differential terminal rendering
- Session picker UI
- Settings UI
- Tree selector / branch visualization
- Interactive editor widgets
- Footer/status widgets
- RPC transport and command protocol
- Print/RPC mode parity

## 2. Provider support and authentication

The original `pi` sits on top of the monorepo AI/provider layer and supports a broad provider set plus multiple auth strategies.

Original `pi` includes support for:

- Anthropic
- OpenAI
- Azure OpenAI
- Google Gemini
- Google Vertex
- Amazon Bedrock
- Mistral
- Groq
- Subscription-backed/OAuth-backed provider flows in the broader project

Python currently supports:

- OpenAI-compatible API shape only
- API key auth only
- Runtime `base_url` override for internal gateways

Missing from Python:

- Provider registry and provider-specific option mapping
- OAuth/subscription login flows such as `/login`
- Automatic model catalog loading per provider
- Provider capability detection
- Multiple transport selection per provider
- Model-family defaults and richer resolver logic

## 3. Config, settings, and reload architecture

The original `pi` has a broader settings and resource-loading architecture.

Original `pi` includes:

- Settings managers
- Resource loaders
- Reloadable prompts, context files, skills, and extensions
- Theme hot reload
- More settings dimensions than simple runtime values

Python currently supports:

- Global YAML config: `~/.astra-python/config.yaml`
- Project YAML config: `.astra/config.yaml`
- Prompt order configuration
- Capability discovery paths for prompts and skills
- Project/global prompt and skill resource discovery
- Session-scoped `/skill:` and `/template:` activation
- Runtime inspection via `/runtime`, `/runtime warnings`, `/runtime prompt`, and `/runtime json prompt`
- Runtime reload for:
  - `model`
  - `base_url`
  - `system_prompt`
  - tool enable list
  - selected tool defaults

Missing from Python:

- Automatic file watching
- Theme config and theme hot reload
- Rich context file loading beyond skill-bundled resources
- Resource loader abstraction
- Rich settings categories
- Persistent auth/config backends beyond simple files

Note: `/reload code` in Python is a developer convenience only. It is not equivalent to the original project’s broader reload behavior.

## 4. Tools and execution model

The Python version has the core built-in tools but not the broader surrounding ecosystem.

Python currently includes:

- `read`
- `write`
- `edit`
- `ls`
- `find`
- `grep`
- `bash`

Missing or reduced versus original `pi`:

- Extension-provided tools
- Tool wrapping and extension hook interception
- Richer tool rendering in the UI
- Provider/model-specific tool capability handling
- Broader surrounding utilities used by the TUI and extension runtime

The Python tools are intentionally narrow and workspace-scoped; this is by design, but it is still a gap from the extensibility of the original.

## 5. Sessions, branching, and compaction

The original `pi` has a more advanced session system.

Python currently includes:

- JSON session files
- Session save/load
- Rename
- Switch
- Fork

Missing from Python:

- Session tree navigation UI
- Branch summarization
- Auto-compaction
- Manual `/compact` support
- Context window management strategies close to original behavior
- Richer session metadata handling
- Broader resumed-session UX

This is one of the most important functional gaps after UI/provider support.

## 6. Command surface

Python currently supports a small subset of slash commands.

Current Python command set:

- `/help`
- `/model`
- `/base-url`
- `/tools`
- `/runtime`
- `/runtime warnings`
- `/runtime json`
- `/runtime prompt`
- `/runtime json prompt`
- `/sessions`
- `/switch`
- `/fork`
- `/rename`
- `/reload`
- `/reload code`
- `/save`
- `/exit`
- `/skill:<name>`
- `/template:<name>`

Original `pi` exposes a much broader operational command surface, including workflow-specific commands for auth, model management, settings, reload, compaction, sessions, and more.

Missing from Python includes at least:

- `/login`
- `/compact`
- richer settings/model management commands
- commands related to themes, prompts, packages, or extension resources
- any RPC-facing command surface

## 7. Extensibility systems

The original project is designed to be extended.

Original `pi` includes first-class concepts for:

- Extensions
- Skills
- Prompt templates
- Themes
- Pi packages

Python currently includes a minimal local capability layer for:

- Skills loaded from `skill.yaml`
- Prompt templates loaded from prompt markdown files
- Session-scoped activation of discovered resources

Python still does not include:

- An extension system
- Theme support
- Package-managed capability distribution
- Hook/interception surfaces comparable to the original project

That means Python is no longer just a single fixed harness, but it is still far from a customizable platform comparable to the original project.

## 8. Export and auxiliary features

Original `pi` includes additional product features outside the core loop.

Missing from Python:

- HTML export
- image-related utilities and richer multimodal flow
- changelog/help utilities tied to the larger project
- package installation/update/remove/list workflows
- platform-specific polish found in the original project docs and implementation

## 9. Validation and test maturity

Python currently relies on targeted smoke validation.

Current Python validation style:

- `compileall`
- local smoke scripts
- interactive CLI smoke checks

Missing from Python compared with the original project:

- broader automated test suite structure
- provider cross-tests
- abort/context/Unicode/tool edge-case coverage at the same depth
- regression coverage for session and reload behavior

## Suggested convergence order

If the goal is selective convergence rather than full parity, the recommended order is:

1. Session and compaction improvements
2. Provider abstraction beyond OpenAI-compatible only
3. Richer config/resource loading
4. Broader command surface
5. Extension/skill/prompt-template system
6. RPC mode
7. TUI and theme system

Reasoning:

- Session quality and provider breadth improve core usefulness fastest.
- Resource/config maturity is required before meaningful extensibility.
- TUI parity is expensive and should come after the runtime architecture is stronger.

## Non-goals unless explicitly requested

The following should not be assumed as automatic goals for the Python project:

- Full feature parity with original `pi`
- Immediate TUI parity
- Reproducing the entire monorepo architecture in Python
- Reproducing every provider/auth flow
- Reproducing extension/package ecosystems in one step

The Python project should continue to converge incrementally, with each new subsystem justified by user value and maintenance cost.

## Update guidance

When the Python project gains a new subsystem or meaningfully narrows a gap, update this document by:

- moving the capability from “missing” to “present” or “partially present”
- describing the exact current boundary, not the aspirational end state
- avoiding vague claims like “supports parity” unless the behavior is actually comparable

## 2026-03-14 update

This report was updated to reflect capability runtime work that had already landed in code but was still underreported in the gap summary.

Newly present or partially present:

- Registry-driven command dispatch instead of a single hard-coded CLI command tree
- Prompt assembly beyond a single direct `system_prompt` string
- Local prompt loading from `.astra/prompts/*.md`
- Local skill loading from `.astra/skills/*/skill.yaml`
- Config-driven prompt ordering and config-driven skill enablement
- Process-session activation via `/skill:<name>` and `/template:<name>`
- Runtime inspection via `/runtime` and `/runtime warnings`
- Final prompt inspection via `/runtime prompt` and `/runtime json prompt`

The current prompt inspection focuses on the effective assembled prompt and the fragments that actually participated in assembly.

What this narrows:

- It meaningfully narrows the gap around core extensibility plumbing.
- It partially narrows the gap around broader command surface.
- It partially narrows the gap around config/resource loading.

What is still missing even after this update:

- No persistent skill/template activation state across process restarts
- No standalone context-source abstraction yet; skill context files are still folded into prompt text
- No compaction strategy or `/compact`
- No Python code extensions or extension hook pipeline
- No provider capability-aware prompt/tool activation
- No automatic file watching; resource changes still require `/reload`
- No structured skipped-fragment inspection yet; prompt inspection currently focuses on the effective assembled result
