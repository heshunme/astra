# Python coding-agent runtime

This repository contains a Python coding-agent runtime that is moving toward a reusable core engine plus a thin application layer.

- LiteLLM-backed streaming chat
- Tool calling loop
- Agent-core state machine with a stable event stream
- Workspace-scoped file and shell tools
- Local JSON session persistence
- Session switching, renaming, and forking
- Manual runtime reload via `/reload`
- Project prompt and skill loading via the capability runtime
- Foundations for a self-evolving agent stack
- Current gap tracking in `GAP_REPORT.md`

## Requirements

- Python 3.11+
- `litellm==1.82.6` is installed with the package
- Provider credentials via LiteLLM-supported environment variables
- Optional: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`
- Optional project `.env` file (for `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, or other LiteLLM provider vars)

Use provider-qualified model names when possible, for example `openai/gpt-5`, `anthropic/claude-sonnet-4-5`, or `ollama/llama3.2`. Unqualified names such as `gpt-5.2` are still accepted and are treated as `openai/gpt-5.2`.

## Quick start

```bash
uv venv .venv
. .venv/Scripts/activate
uv pip install -e .
astra --model openai/gpt-5 --base-url http://your-gateway/v1
```

## Codex sandbox

If you run this repository inside a Codex `workspace-write` sandbox, make sure Codex is allowed to both reach the network and write to the directories that `uv` uses for caches and tool data. Add this to your Codex `config.toml`:

```toml
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
writable_roots = [
  "/tmp",
  "~/.cache/uv",
]
```

Without those settings, commands such as `uv run python -m compileall src`, `uv run python -m astra --help`, or `uv pip install -e .` can fail with network, cache-lock, or read-only cache directory errors even when Astra itself is fine.

## Architecture
The project is in a transition state, but the code now follows a four-layer shape:

- `core engine`
  - Owns conversation state, provider/tool orchestration, event emission, abort, and generic snapshot/restore mechanics
- `coding-agent`
  - Owns runtime reload application, prompt assembly, typed skill/template behavior, runtime inspection, and other coding-agent-specific policy
- `application service`
  - The exported `AstraApp` type owns config loading, session persistence, startup/restore orchestration, runtime/session commands, and `/reload code`
- CLI
  - Owns terminal I/O, signal handling, and slash-command parsing that translates interactive commands such as `/model`, `/base-url`, `/skills`, `/templates`, `/reload`, `/sessions`, `/skill:<name>`, and `/template:<name> <request>` into `AstraApp` typed calls

Longer-term architecture direction, including core-engine goals and self-evolution layering, is documented in `docs/evolution_strategy.md`.

The current reusable non-CLI entrypoint is the exported `AstraApp` type. `Agent` remains the lower-level coding-agent facade over the internal core engine.

For a current architecture survey in Chinese, see `docs/architecture.zh-CN.md`.

## Evolution direction
The project is moving toward a reusable core engine plus a thin application layer, while the near-term product priority is self-evolving agent infrastructure. See `docs/evolution_strategy.md` for the detailed direction and layering rules.

## Session storage

Sessions are stored under `~/.astra-python/sessions` by default.

Starting the CLI does not create a saved session by itself. A new session is written only after you send a normal user message to the model. Slash commands such as `/help`, `/runtime`, `/tools`, `/skills`, `/templates`, `/model`, `/base-url`, `/reload`, `/save`, `/rename`, and `/fork` do not create a new session on their own.

For a new conversation, the first normal user prompt becomes the default saved session name until you change it with `/rename`.

Saved sessions persist both message history and the agent snapshot needed to restore runtime-only state for that session, including:

- pending one-shot skill trigger
- the full resolved runtime config for that session, including `model`, `base_url`, `system_prompt`, tool enablement and defaults, prompt order, and capability paths

When you restore a session via `--session`, `/switch`, or `/resume`, Astra reapplies that saved runtime snapshot before continuing. Use `/reload` when you explicitly want to switch back to the current env and YAML-derived runtime.
Interactive commands such as `/model` and `/base-url` change only the current session runtime and any snapshots saved from it; they do not replace the env/YAML-derived baseline that `/reload` and `/reload code` restore.

## Reloadable config

Reloadable config is read from:

- Global: `~/.astra-python/config.yaml`
- Project: `.astra/config.yaml`

Project config overrides global config. Apply changes with `/reload`.

Environment variables are also loaded from `<cwd>/.env` (if present). Values already set in the process environment take precedence over `.env`.

Example:

```yaml
model: openai/gpt-5-mini
base_url: http://your-gateway/v1
system_prompt: Be strict about verifying edits before writing.

tools:
  enabled: [read, write, edit, bash, grep, find, ls]
  defaults:
    read:
      max_lines: 600
    bash:
      timeout_seconds: 90
      max_output_bytes: 65536

prompts:
  order:
    - builtin:base
    - config:system
    - prompt:repo-rules

capabilities:
  prompts:
    paths: []
  skills:
    paths: []
```

`base_url` remains Astra's runtime setting and now maps to LiteLLM `api_base`. First-pass multi-provider support intentionally relies on LiteLLM's own environment-variable conventions rather than new Astra-specific config keys.

## Prompt and skill resources

Project prompt files:

- `.astra/prompts/*.md`

Project skills:

- `~/.astra-python/skills/*/skill.yaml`
- `.astra/skills/*/skill.yaml`
- `.astra/skills/*/*.md`
- `capabilities.skills.paths/*/skill.yaml`
- `capabilities.skills.paths/*/*.md`

Minimal `skill.yaml` example:

```yaml
name: review
summary: Add structured review guidance.
when_to_use: Use when the user asks for a code review.
prompt_files:
  - checklist.md
context_files:
  - style.md
```

Skill files stay on disk until the model reads them with the `read` tool. Astra injects a generated skill catalog into the system prompt on every turn so the model knows which skills are available, what they are for, and which files to read on demand.

Skill resources are exposed to the model as read-only virtual paths such as `skill://review/checklist.md`. The `read` tool resolves only the currently discovered skill aliases plus normal workspace-relative paths; it does not expose host absolute paths for global or extra-path skills.

If multiple discovered skills share the same `name`, Astra resolves them with a fixed priority instead of silently using scan order:

- project: `.astra/skills`
- extra paths: `capabilities.skills.paths` in config order, where later paths override earlier paths
- global: `~/.astra-python/skills`

Duplicate names remain usable, but Astra emits runtime warnings and exposes the winner plus shadowed definitions through `/skills`, `/runtime`, and `/runtime json`.

## Runtime inspection

Use runtime inspection commands to verify what the agent is currently using.

- `/tools`
  - Enabled tools and tool default limits (`read.max_lines`, bash timeout, bash output cap)
- `/skills`
  - Available skills for the current runtime, including summary, optional `when_to_use`, active source, and shadowed-definition count when there is a duplicate-name conflict
- `/templates`
  - Available templates for the current runtime

- `/runtime`
  - Human-readable runtime summary
- `/runtime warnings`
  - Only runtime warnings
- `/runtime json`
  - Machine-readable runtime state summary
- `/runtime prompt`
  - Human-readable assembled system prompt plus fragment order and sources
- `/runtime json prompt`
  - Machine-readable assembled system prompt plus fragment metadata

Example:

```text
/runtime prompt
Runtime prompt
fragments=3
char_length=240
fragment[1]=builtin:base source=builtin chars=86
fragment[2]=session:skills-catalog source=coding-agent chars=117
fragment[3]=prompt:repo-rules source=E:\repo\.astra\prompts\repo-rules.md chars=37
assembled_with_boundaries:
...
```

This is the preferred way to check whether config, prompt files, and the generated skill catalog match the final system prompt sent to the provider.

## Supported commands

- `/help`
- `/model [name]`
- `/base-url [url]`
- `/tools`
- `/skills`
- `/templates`
- `/runtime`
- `/runtime warnings`
- `/runtime json`
- `/runtime prompt`
- `/runtime json prompt`
- `/sessions`
- `/resume`
- `/switch <session-id>`
- `/fork [name]`
- `/rename <name>`
- `/reload`
- `/reload code`
- `/save`
- `/exit`
- `/skill:<name> [request]`
- `/template:<name> <request>`

`/save`, `/rename`, and `/fork` require an existing saved session. If you have only used slash commands in the current CLI process, they will print a message instead of creating an empty session.

Use `/resume` to interactively list saved sessions by number and reopen one without typing a session id. After resuming, Astra prints the effective runtime configuration for that session without replaying message history.

Use `/skills` to inspect the currently usable skill catalog from the CLI. The listed files are `skill://...` aliases that can be passed to the `read` tool. If skills are discovered but the `read` tool is disabled, Astra prints a note instead of advertising unusable `/skill:<name>` actions.

Use `/runtime json` when you need full duplicate-skill diagnostics. Skill source fields are exposed as virtual identifiers such as `skill://review`, not host absolute paths. The `skills.conflicts` array includes the winning source plus every shadowed source for each duplicated skill name.

Use `/templates` to list discovered templates.

Use `/runtime prompt` for a human-readable view of the fully assembled system prompt and the fragment order that produced it.

Use `/runtime json prompt` for a machine-readable version of the same inspection data.

`/skill:<name> <request>` rewrites that input into a normal user message for a single turn. `/skill:<name>` without a request arms the next normal user message once, then clears itself. In both cases the rewritten natural-language request is what gets stored in session history, while the raw slash command is preserved only in message metadata.

`/template:<name> <request>` rewrites that input into a normal user message for a single turn. The template body is injected at the top of that rewritten user message, and the raw slash command plus template metadata are preserved in message metadata. It does not modify the system prompt or create any persistent active-template state.

In non-interactive mode, for example `python -m astra "hello"`, prompt failures exit with status code `1`.

## Testing

Detailed Chinese validation guide:

- `TESTING.zh-CN.md`

Install test dependencies:

```powershell
uv pip install -e ".[test]"
```

Fast validation (recommended for PR checks):

```powershell
uv run python -m compileall src
uv run python -m astra --help
uv run python -m pytest -q tests/unit tests/integration -m "not slow and not contract" --cov=astra --cov-fail-under=50
```

One-command local smoke for the current CLI/runtime surface:

```bash
bash scripts/smoke_cli.sh
```

The smoke flow is implemented in `scripts/smoke_cli.py`. `scripts/smoke_cli.sh` is a thin compatibility wrapper that forwards to the Python implementation.

It runs `compileall`, CLI help, unit/integration tests excluding contract tests, and a scripted local CLI session that exercises `/tools`, `/runtime`, `/reload`, `/reload code`, session commands, and capability activation without requiring a real provider key.

Direct Python entrypoint:

```bash
 scripts/smoke_cli.py
```

To add one real end-to-end provider call after the local smoke:

```bash
bash scripts/smoke_cli.sh --live-provider
```

`--real` is an alias for `--live-provider`.

For `--live-provider` / `--real`, the script will first use the current shell environment. If a real `OPENAI_API_KEY` is not already set, it will replace the temporary workspace `.env` with a symlink to the repository `.env`, so the live smoke can reuse the same project credentials without exporting them manually. You can also point it at another file:

```bash
bash scripts/smoke_cli.sh --real --env-file /path/to/.env
```

Manual CLI sandbox for hands-on testing:

```bash
 scripts/manual_cli.py
```

This prepares a temporary workspace with:

- `.astra/config.yaml`
- prompt templates under `.astra/prompts`
- sample skills under `.astra/skills`
- sample files for `read`, `edit`, `find`, `grep`, `ls`, and `bash`
- a workspace `.env` symlink or copy sourced from the repository `.env`

It then launches `python -m astra --cwd <temp-workspace>` so you can manually test the CLI against a disposable project. Use `--no-launch` to only prepare the workspace, or `--cleanup` to remove it after the session exits successfully.

Extended validation (recommended for nightly runs):

```powershell
uv run python -m pytest -q -m "contract or slow"
```
