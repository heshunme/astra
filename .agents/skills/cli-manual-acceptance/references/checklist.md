# CLI Manual Acceptance Checklist

Use this checklist when the user asks to walk through the repository's hand-run CLI validation flow.

## Recommended Execution Path

Use the following setup unless the user explicitly asks for a different path:

```text
env OPENAI_API_KEY=test-key .venv/bin/python -m compileall src
env OPENAI_API_KEY=test-key .venv/bin/python -m astra --help
.venv/bin/python scripts/manual_cli.py --no-launch
env HOME=<temp_root>/home .venv/bin/python -m astra --cwd <workspace>
```

Notes:

- Assume commands are run from the repository root so `.venv/bin/python` resolves correctly.
- Prefer `.venv/bin/python` for the interactive session. Bare system `python` may fail with missing editable-install dependencies such as `yaml`.
- Prefer `.venv/bin/python` for the non-interactive prechecks as well so the whole manual acceptance flow uses a single interpreter baseline.
- Capture the `temp_root` and `workspace` paths printed by `.venv/bin/python scripts/manual_cli.py --no-launch`; they are needed for the interactive launch.
- Do not export `OPENAI_API_KEY` in the shell, otherwise the environment will override `.env` and the live-provider step may fail with 401.
- Feed slash commands in small batches so the first mismatch is attributable.

## Command Order

Run the commands in this order unless the user explicitly wants a scoped subset:

```text
/help
/tools
/skills
/templates
/runtime
/runtime warnings
/runtime json
/runtime prompt
/runtime json prompt
/model
/model smoke-model
/skill:review
/skill:debug
/template:pairing Summarize docs/plan.md in one sentence.
/runtime prompt
/runtime json prompt
/base-url
/base-url http://gateway.local/v1
/sessions
/resume
/fork smoke-copy
/rename smoke-main
/save
/reload
/reload code
/exit
```

## Expected Observations

- `/help`
  Expect the core slash commands plus `/skill:<name> [request]` and `/template:<name>`.
- `/tools`
  Expect enabled tools and the configured `read.max_lines`, `bash.timeout_seconds`, and `bash.max_output_bytes`.
- `/skills`
  Expect discovered skills, or a clear empty-state / read-disabled message. Skill files should be shown as `skill://...` aliases rather than host absolute paths. If duplicate names exist, expect source labels and shadowed-definition counts.
- `/templates`
  Expect discovered templates by name. Do not expect persistent active-state markers.
- `/runtime`
  Expect tools, prompt order, discovered prompts, available skills, pending skill state, duplicate-skill conflict count, available templates, and warning count.
- `/runtime warnings`
  Expect either explicit warnings or `No runtime warnings`.
- `/runtime json`
  Expect the same runtime state in machine-readable form. If duplicate skill names exist, expect `skills.conflicts` entries that identify the winner and shadowed definitions.
- `/runtime prompt`
  Expect fragment count, fragment metadata, and the assembled prompt with fragment boundaries.
- `/runtime json prompt`
  Expect a JSON payload that includes `prompt`, `fragment_count`, and assembled prompt metadata.
- `/model`
  Expect the current model value.
- `/model smoke-model`
  Expect `Model set to smoke-model`.
- `/skill:review`
  Expect a one-shot pending skill message.
- `/skill:debug`
  Expect a one-shot pending skill message that replaces the prior pending skill.
- `/template:pairing Summarize docs/plan.md in one sentence.`
  Expect an immediate one-turn request rewrite and model execution. Run this before changing `base_url` so template semantics are not masked by an intentionally unreachable gateway.
- `/runtime prompt` after `/template:pairing ...`
  Expect no new `prompt:pairing` fragment in the assembled system prompt. Prompt inspection should remain aligned with the actual provider system prompt.
- `/runtime json prompt` after `/template:pairing ...`
  Expect the same prompt payload as `/runtime prompt`, with no template-only fragment added.
- `/base-url`
  Expect the current base URL.
- `/base-url http://gateway.local/v1`
  Expect `Base URL set to http://gateway.local/v1`.
- `/sessions`
  Expect either no saved sessions or a session table. Before a normal user prompt, slash commands alone should not materialize a new session.
- `/resume`
  Expect a numbered list for the current cwd only, then a restored session summary after selection. The resumed runtime should reflect the saved session snapshot first, not the current YAML/env state.
- `/fork smoke-copy`
  Expect either `No saved session to fork.` or a new fork id once a saved session exists.
- `/rename smoke-main`
  Expect either `No saved session to rename.` or the updated name once a saved session exists.
- `/save`
  Expect either `No session to save.` or `Saved ...` once a saved session exists.
- `/reload`
  Expect runtime re-application from the current env/YAML without losing intended conversation/session state. After resuming an older session, this is the point where current config should replace the restored snapshot runtime.
- `/reload code`
  Expect code reload plus a follow-up runtime reload summary. Conversation state and runtime-only session state should survive the code reload path.

## High-Value Checks

- Confirm that `/runtime prompt` and `/runtime json prompt` describe the same assembled prompt.
- Confirm that discovered skills stay inert until explicitly used.
- Confirm that `/template:<name> <request>` rewrites one turn only and does not change prompt assembly or create persistent template state.
- Confirm that session restore paths preserve the saved runtime snapshot first, including `model`, `base_url`, tools, prompt order, capability paths, pending skill trigger, and conversation state.
- Confirm that `/reload` switches an already restored session back to the current env/YAML-derived runtime.
- Confirm that reload is blocked while a response is streaming if that case is under test.
- Confirm that any failure before the interactive CLI starts is classified as environment setup or interpreter/bootstrap friction before treating it as a product regression.

## Failure Hints

- Help output mismatch usually points at CLI command registration or extension command exposure.
- Skill or template listing mismatch usually points at capability discovery, duplicate-name resolution, alias generation, or disabled `read`.
- Prompt mismatch usually points at prompt assembly order, missing fragments, or confusion between system-prompt inspection and one-turn template rewriting.
- Session restore mismatch usually points at snapshot persistence or restore ordering across CLI, session, and agent runtime layers.
- Reload mismatch usually points at `apply_runtime_config`, code reload reconstruction, or runtime snapshot cloning.
