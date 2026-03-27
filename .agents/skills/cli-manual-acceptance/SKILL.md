---
name: cli-manual-acceptance
description: Guide the Astra repository's manual CLI acceptance flow for runtime, prompt assembly, skills, templates, reload, and session behavior. Use when the user asks to walk through the hand-run CLI checklist, validate a CLI/runtime/session change manually, or convert the documented manual acceptance sequence into a repeatable review workflow.
---

# CLI Manual Acceptance

## Overview

Run the repository's documented manual CLI acceptance flow in a consistent order. Use it to guide manual validation, check observed output against expected behavior, and report the first broken step with a likely cause.

Read [references/checklist.md](references/checklist.md) before executing the flow. Treat [TESTING.zh-CN.md](../../../TESTING.zh-CN.md) as the source of truth if the repository documentation changes and the reference file looks stale.

Prefer the most stable execution path that worked in practice:

- Use the repository virtualenv interpreter for non-interactive prechecks such as `compileall` and `python -m astra --help`:
  `env OPENAI_API_KEY=test-key .venv/bin/python -m compileall src`
  `env OPENAI_API_KEY=test-key .venv/bin/python -m astra --help`
- Use the repository virtualenv interpreter to create the disposable workspace first so you can capture the exact `temp_root`, `workspace`, and `home` paths:
  `.venv/bin/python scripts/manual_cli.py --no-launch`
- Launch the interactive CLI with the repository virtualenv interpreter, not bare system Python, and run this interactive step outside the sandbox when the environment enforces network restrictions. Do not set `OPENAI_API_KEY` here so the app reads the real key from `<workspace>/.env`:
  `env HOME=<temp_root>/home .venv/bin/python -m astra --cwd <workspace>`.
- Avoid `python -m astra` from `/usr/bin/python` for the manual session; it can miss editable-install dependencies such as `yaml`.
- Assume commands are run from the repository root so `.venv/bin/python` resolves correctly.
- In sandboxed Codex environments, keep the non-interactive prechecks inside the sandbox if they work, but request escalation for the interactive CLI launch so provider access is not blocked by sandbox network policy.

## Workflow

1. Confirm the validation scope.
   Decide whether the user wants the full manual flow or only the subset relevant to CLI, runtime, prompt assembly, session restore, or reload behavior.
2. Prepare the execution path.
   Run the lightweight prechecks with `.venv/bin/python`, create the temporary workspace with `.venv/bin/python scripts/manual_cli.py --no-launch`, and launch the interactive CLI from `.venv/bin/python` with `HOME` pointed at the generated temp home. In Codex sandboxed environments, treat that interactive launch as an escalation step so it runs outside the sandbox. Do not export `OPENAI_API_KEY` in the shell (env vars override `.env`).
3. Read the checklist.
   Use the ordered commands and expected observations from `references/checklist.md`. Do not improvise a new order unless the change scope clearly justifies skipping unrelated steps.
4. Run the flow from low risk to high impact.
   Start with read-only inspection commands, then state-changing commands, then reload and session operations.
5. Compare behavior after each step.
   Check the visible CLI output against the expected signals in the checklist. Send slash commands in small batches so the first divergence stays obvious. Stop claiming success as soon as one step diverges.
6. Report tightly.
   If all checked steps match, summarize what was covered and what was intentionally skipped. If a step fails, report the first failing command, the observed mismatch, and the most likely component to inspect next.

## Operating Rules

- Prefer the documented command order over ad hoc exploration.
- Keep the user informed which step you are currently validating and why it matters.
- Do not claim real provider validation unless a live-provider path was actually run.
- If the sandbox blocks provider access, classify that as an execution-environment limitation and rerun the interactive CLI outside the sandbox before concluding the product is broken.
- Treat slash commands as non-materializing until a normal user prompt is sent.
- When session behavior is under test, pay attention to the saved-vs-current runtime distinction: `model`, `base_url`, tool defaults, prompt order, capability paths, pending one-shot skill state, and loaded session identity.
- When validating template behavior, remember that `/template:<name> <request>` rewrites one user turn only. It does not activate a persistent template mode or add a prompt fragment to `/runtime prompt`.
- Distinguish environment failures from product failures. Missing packages from bare `python` or an unprepared `.venv` are execution-path issues, not CLI regressions.

## Failure Triage

- If `/help` is wrong or missing commands, inspect CLI command registration and extension command exposure.
- If `/skills`, `/templates`, or `/runtime prompt` disagree with expectations, inspect capability discovery, duplicate-skill resolution, skill alias exposure, and prompt assembly.
- If `/reload` or `/reload code` loses state, inspect runtime apply, snapshot restore, and session serialization.
- If `/resume` or `/switch` restores the wrong configuration, inspect session snapshot persistence and restore order.

## Output Shape

When reporting results, use this structure:

- Scope covered
- Commands run
- First failing step, or confirmation that all checked steps matched
- Next file or subsystem to inspect if something diverged

The final execution summary shown to the user must be written in Chinese, even if the checklist commands and intermediate notes stay in English.

## Resources

- Read [references/checklist.md](references/checklist.md) for the ordered command list and expected observations.
