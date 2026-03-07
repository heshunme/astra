# Python Replica Rules

These instructions apply to the entire `python/` directory tree.

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
  - `.venv\Scripts\python.exe -m pyi --help`
- Package install/update:
  - `uv pip install -e .`
- Do not run repo-level `npm` commands for work limited to `python/` unless explicitly asked.

## Configuration
- Reloadable config files:
  - Global: `~/.pyi-python/config.yaml`
  - Project: `.pyi/config.yaml`
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

## Documentation
- Update `README.md` when changing CLI flags, slash commands, config keys, or runtime reload behavior.
- Keep examples PowerShell-friendly unless cross-platform behavior is the point of the change.

## Git and scope hygiene
- Only modify files under `python/` unless the user explicitly asks for broader changes.
- Keep changes minimal and consistent with the existing Python package layout.
