# Python Replica Rules

These instructions apply to the entire repository tree.

## Purpose
- This package is a Python replica of the core `pi-mono` coding-agent flow.
- Keep the scope focused on: CLI, session persistence, tool calling, runtime reload, and OpenAI-compatible provider support.
- Do not add TUI, RPC, extension ecosystems, or unrelated monorepo concepts unless explicitly requested.

## Environment
- Use `uv` for local environment management.
- Preferred setup:
  - `uv venv .venv`
  - `. .venv/Scripts/activate`
  - `uv pip install -e .`
- You may create and maintain the local `.venv` for this package.
- Do not edit files inside `.venv` directly.

## Commands
- Syntax check / smoke validation:
  - `.venv\Scripts\python.exe -m compileall src`
  - `.venv\Scripts\python.exe -m astra --help`
- Package install/update:
  - `uv pip install -e .`
- Do not run repo-level `npm` commands for work limited to this package unless explicitly asked.

## Configuration
- Reloadable config files:
  - Global: `~/.astra-python/config.yaml`
  - Project: `.astra/config.yaml`
- Precedence:
  - CLI args override YAML
  - Project YAML overrides global YAML
  - Environment variables fill gaps
  - Built-in defaults are last fallback
- Supported runtime settings currently include:
  - `model`
  - `base_url`
  - `system_prompt`
  - `tools.enabled`
  - `tools.defaults.read.max_lines`
  - `tools.defaults.bash.timeout_seconds`
  - `tools.defaults.bash.max_output_bytes`

## Runtime behavior
- `/reload` is the stable manual reload path.
- `/reload code` is best-effort only; do not treat it as a guaranteed hot-swap system.
- `--base-url`, YAML `base_url`, and `/base-url` must stay aligned.
- Preserve session history across runtime reloads.
- Do not reload while a response is streaming.

## Implementation rules
- Prefer standard library modules unless a dependency is clearly justified.
- Keep provider integration OpenAI-compatible unless the task explicitly asks for another provider.
- Keep tools workspace-scoped; never weaken path-safety checks.
- Avoid adding broad abstractions before they are needed.
- Keep CLI behavior simple and explicit.

## Validation expectations
- For code changes, run at least targeted Python validation relevant to the change.
- Prefer small smoke tests over heavyweight test scaffolding unless tests are explicitly requested.
- If you add config behavior, validate both merge precedence and runtime reload.
- If you add CLI behavior, validate the actual command path, not just library calls.

## Working Notes
- Read every target file in full before editing; when several files are coupled, read them as one batch first.
- Prefer replacing long `if/elif` command trees with registries and small handlers before adding new commands.
- When introducing runtime state, separate three layers clearly: persisted config, process runtime state, and session-scoped temporary state.
- Do not silently turn discovery into activation: discovered prompt/skill resources should remain inert until config or command flow enables them.
- Keep `system_prompt` migration incremental: preserve the old input surface, then route it through the new assembler.
- For skill resources, fail soft on malformed YAML or missing files and surface warnings through reload/runtime inspection.
- Before broad refactors, add one small reusable seam first, such as `build_all_tools()` before moving tool selection into a registry.
- Avoid using `/reload code` as the primary development contract; stable behavior should work through `/reload`.
- When changing prompt assembly, keep `/runtime prompt` and `/runtime json prompt` aligned with the exact prompt that the agent will send.

## SOP
- Capability runtime change:
  - Read `src/astra/config.py`, `src/astra/agent.py`, `src/astra/cli.py`, and the target runtime/doc files in full.
  - Add or change config shape first.
  - Add runtime/registry behavior second.
  - Wire agent usage third.
  - Wire CLI commands last.
- Validation sequence:
  - `.venv\Scripts\python.exe -m compileall src`
  - `.venv\Scripts\python.exe -m astra --help`
  - Use a local `python -c` smoke check for config/runtime assembly when network access is not needed.
  - For command-path changes, pipe commands into `astra` with a fake `OPENAI_API_KEY` when the flow does not need a real provider call.
  - For prompt-assembly changes, also smoke-check `/runtime prompt` and `/runtime json prompt`.
- Common mistakes to avoid:
  - Forgetting to carry new runtime fields through clone/reload paths.
  - Making newly discovered resources auto-activate by accident.
  - Changing prompt assembly without updating reload summaries and operator-facing inspection output.
  - Validating only imports instead of validating the actual CLI command path.
  - Building a second prompt-inspection code path in CLI instead of reusing the same runtime/agent assembly logic.

## Documentation
- Update `README.md` when changing CLI flags, slash commands, config keys, or runtime reload behavior.
- Keep examples PowerShell-friendly unless cross-platform behavior is the point of the change.

## Git and scope hygiene
- Only modify files in this repository unless the user explicitly asks for broader changes.
- Keep changes minimal and consistent with the existing Python package layout.
