# Python Replica Rules

These instructions apply to the entire repository tree.

## Purpose
- This package is a Python coding-agent runtime.
- Keep scope focused on: core loop, runtime reload, CLI/session adaptation, and higher-level artifact loading.
- Do not add TUI or unrelated monorepo concepts unless explicitly requested.
- Preserve the split between reusable engine behavior and higher-level application policy.
- Project evolution goals and architecture direction are documented in `docs/evolution_strategy.md`.

## Environment
- Use `uv` for local environment management.
- Preferred setup:
  - `uv venv .venv`
  - `. .venv/bin/activate` (Linux/macOS) or `. .venv/Scripts/activate` (PowerShell-compatible docs may still use `Scripts`)
  - `uv pip install -e .`
- Do not edit files inside `.venv` directly.

## Commands
- Syntax/smoke validation:
  - `uv run python -m compileall src`
  - `uv run python -m astra --help`
- Package install/update:
  - `uv pip install -e .`
- Optional local smoke:
  - `bash scripts/smoke_cli.sh`
- Do not run repo-level `npm` commands for work limited to this package unless explicitly asked.

## Configuration
- Reloadable config files:
  - Global: `~/.astra-python/config.yaml`
  - Project: `.astra/config.yaml`
- Resolution precedence:
  - CLI args override YAML.
  - Project YAML overrides global YAML.
  - Environment variables fill remaining gaps.
  - Built-in defaults are final fallback.
- Runtime keys supported by code today:
  - `model`
  - `base_url`
  - `system_prompt`
  - `tools.enabled`
  - `tools.defaults.read.max_lines`
  - `tools.defaults.bash.timeout_seconds`
  - `tools.defaults.bash.max_output_bytes`
  - `prompts.order`
  - `capabilities.prompts.paths`
  - `capabilities.skills.paths`
- `capabilities.skills.enabled` is removed and must not be reintroduced.

## Runtime behavior
- `/reload` is the stable runtime reload path.
- `/reload code` is best-effort developer convenience only.
- Do not reload while a response is streaming.
- Keep `--base-url`, YAML `base_url`, and `/base-url` behavior aligned.
- Session history must persist across runtime reloads and session switching.
- Session snapshot restore must also preserve runtime-only state such as active templates, pending skill trigger, `model`, `base_url`, and `system_prompt`.
- A new session is materialized/saved only after a normal user prompt, not by slash commands alone.
- Prompt assembly must match the exact prompt sent to provider: default refs + discovered fragments + session skill catalog + active templates.

## Skill/Template behavior
- Discovered skills are inert until explicitly used via `/skill:<name> ...` or armed for next turn with `/skill:<name>`.
- `/skill:<name>` applies to one turn only; do not introduce permanent skill mode toggles.
- Skill metadata can be retained in session history, but raw skill files stay on disk and are read on demand via `read` tool.
- `/template:<name>` activates a discovered prompt fragment for the current process session state.
- `/skill:<name>` and `/template:<name>` are core extension commands; keep them discoverable in CLI help output even though CLI built-ins are registered separately.
- Fail soft on malformed skill YAML or missing files and surface warnings through runtime diagnostics.

## Implementation rules
- Prefer standard library modules unless a dependency is clearly justified.
- Keep provider integration OpenAI-compatible unless explicitly asked otherwise.
- Keep tools workspace-scoped; never weaken path-safety checks.
- Keep CLI behavior explicit and minimal.
- Prefer command registries and focused handlers over growing `if/elif` command trees.
- Keep runtime state layers explicit: persisted config, process runtime config, session-scoped temporary state.
- Keep the ownership boundary explicit:
  - `src/astra/agent.py` owns the state machine, event stream, runtime apply/inspect, tool loop, and extension command semantics in the current codebase.
  - `src/astra/cli.py` owns config loading, session store interaction, terminal rendering, and built-in slash commands.

## Validation expectations
- For code changes, run at least targeted validation relevant to the change.
- Prefer small smoke checks over heavy scaffolding unless tests are requested.
- If config or runtime assembly changes, validate merge precedence and `/reload`.
- If CLI behavior changes, validate actual CLI command path (not only imports/unit helpers).
- For prompt assembly changes, validate both `/runtime prompt` and `/runtime json prompt`.

## SOP
- Capability/runtime change order:
  - Read `src/astra/config.py`, `src/astra/runtime/runtime.py`, `src/astra/agent.py`, and `src/astra/cli.py` in full.
  - Change config shape first.
  - Implement runtime/registry behavior second.
  - Wire agent behavior third.
  - Wire CLI command handling last.
- Recommended validation sequence:
  - `uv run python -m compileall src`
  - `uv run python -m astra --help`
  - `python -c` smoke checks for local config/runtime assembly when network calls are unnecessary
  - For command-path changes, pipe scripted input into `python -m astra` with a fake `OPENAI_API_KEY`.
- Common mistakes:
  - Missing new runtime fields during clone/reload/switch paths.
  - Restoring `model`/`system_prompt` but accidentally dropping session-specific `base_url` or template/pending-skill state.
  - Accidentally turning discovery into auto-activation.
  - Letting `/runtime prompt` diverge from actual provider prompt assembly.
  - Hiding core extension commands from `/help` after changing CLI command plumbing.
  - Validating only library imports instead of CLI paths.

## Documentation
- Update `README.md` when changing CLI flags, slash commands, config keys, prompt/skill behavior, or runtime reload behavior.
- After completing any change, explicitly assess whether `README.md` and `AGENTS.md` need updates, and update them when needed.
- Keep examples PowerShell-friendly unless demonstrating shell-specific behavior.

## Git and scope hygiene
- Only modify files in this repository unless explicitly asked for broader changes.
- Keep changes minimal and aligned with the existing Python package layout.
