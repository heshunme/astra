# Python core replica

This directory contains a Python implementation of the core `pi-mono` coding-agent flow:

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

## Quick start

```bash
cd python
uv venv .venv
. .venv/Scripts/activate
uv pip install -e .
astra --model gpt-4o-mini --base-url http://your-gateway/v1
```

## Session storage

Sessions are stored under `~/.astra-python/sessions` by default.

## Reloadable config

Reloadable config is read from:

- Global: `~/.astra-python/config.yaml`
- Project: `.astra/config.yaml`

Project config overrides global config. Apply changes with `/reload`.

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
    enabled: [review]
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
prompt_files:
  - checklist.md
context_files:
  - style.md
```

Only prompt and skill resources referenced by config or activated in-session are injected into the final system prompt.

## Runtime inspection

Use runtime inspection commands to verify what the agent is currently using.

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
fragments=2
char_length=123
fragment[1]=builtin:base source=builtin chars=86
fragment[2]=prompt:repo-rules source=E:\repo\.astra\prompts\repo-rules.md chars=37
assembled:
...
```

This is the preferred way to check whether config, prompt files, skills, and in-session `/skill:` or `/template:` activations actually changed the final prompt sent to the provider.

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
- `/switch <session-id>`
- `/fork [name]`
- `/rename <name>`
- `/reload`
- `/reload code`
- `/save`
- `/exit`
- `/skill:<name>`
- `/template:<name>`

Use `/runtime prompt` for a human-readable view of the fully assembled system prompt and the fragment order that produced it.

Use `/runtime json prompt` for a machine-readable version of the same inspection data.
