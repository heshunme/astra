# Python core replica

This repository contains a Python implementation of the core `pi-mono` coding-agent flow:

- OpenAI-compatible streaming chat
- Tool calling loop
- Workspace-scoped file and shell tools
- Local JSON session persistence
- Session switching, renaming, and forking
- Manual runtime reload via `/reload`
- Project prompt and skill loading via the capability runtime
- Current gap tracking in `GAP_REPORT.md`

## Requirements

- Python 3.11+
- `OPENAI_API_KEY`
- Optional: `OPENAI_BASE_URL`, `OPENAI_MODEL`
- Optional project `.env` file (for `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`)

## Quick start

```bash
uv venv .venv
. .venv/Scripts/activate
uv pip install -e .
astra --model gpt-4o-mini --base-url http://your-gateway/v1
```

## Session storage

Sessions are stored under `~/.astra-python/sessions` by default.

Starting the CLI does not create a saved session by itself. A new session is written only after you send a normal user message to the model. Slash commands such as `/help`, `/runtime`, `/tools`, `/model`, `/base-url`, `/reload`, `/save`, `/rename`, and `/fork` do not create a new session on their own.

For a new conversation, the first normal user prompt becomes the default saved session name until you change it with `/rename`.

## Reloadable config

Reloadable config is read from:

- Global: `~/.astra-python/config.yaml`
- Project: `.astra/config.yaml`

Project config overrides global config. Apply changes with `/reload`.

Environment variables are also loaded from `<cwd>/.env` (if present). Values already set in the process environment take precedence over `.env`.

Example:

```yaml
model: gpt-4o-mini
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

## Prompt and skill resources

Project prompt files:

- `.astra/prompts/*.md`

Project skills:

- `.astra/skills/*/skill.yaml`
- `.astra/skills/*/*.md`

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

## Runtime inspection

Use runtime inspection commands to verify what the agent is currently using.

- `/tools`
  - Enabled tools and tool default limits (`read.max_lines`, bash timeout, bash output cap)

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
fragment[2]=session:skills-catalog source=session:<id> chars=117
fragment[3]=prompt:repo-rules source=E:\repo\.astra\prompts\repo-rules.md chars=37
assembled:
...
```

This is the preferred way to check whether config, prompt files, the generated skill catalog, and in-session `/template:` changes match the final prompt sent to the provider.

## Supported commands

- `/help`
- `/model [name]`
- `/base-url [url]`
- `/tools`
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
- `/template:<name>`

`/save`, `/rename`, and `/fork` require an existing saved session. If you have only used slash commands in the current CLI process, they will print a message instead of creating an empty session.

Use `/resume` to interactively list saved sessions by number and reopen one without typing a session id. After resuming, Astra prints the effective runtime configuration for that session without replaying message history.

Use `/runtime prompt` for a human-readable view of the fully assembled system prompt and the fragment order that produced it.

Use `/runtime json prompt` for a machine-readable version of the same inspection data.

`/skill:<name> <request>` rewrites that input into a normal user message for a single turn. `/skill:<name>` without a request arms the next normal user message once, then clears itself. In both cases the rewritten natural-language request is what gets stored in session history, while the raw slash command is preserved only in message metadata.

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
