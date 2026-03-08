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
pyi --model gpt-4o-mini --base-url http://your-gateway/v1
```

## Session storage

Sessions are stored under `~/.pyi-python/sessions` by default.

## Reloadable config

Reloadable config is read from:

- Global: `~/.pyi-python/config.yaml`
- Project: `.pyi/config.yaml`

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

- `.pyi/prompts/*.md`

Project skills:

- `.pyi/skills/*/skill.yaml`
- `.pyi/skills/*/*.md`

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

## Supported commands

- `/help`
- `/model [name]`
- `/base-url [url]`
- `/tools`
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
