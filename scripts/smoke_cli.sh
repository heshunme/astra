#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN_DEFAULT="$ROOT_DIR/.venv/bin/python"
PYTHON_BIN="${ASTRA_SMOKE_PYTHON:-$PYTHON_BIN_DEFAULT}"
ENV_FILE="${ASTRA_SMOKE_ENV_FILE:-$ROOT_DIR/.env}"
RUN_PYTEST=1
LIVE_PROVIDER=0
KEEP_TEMP=0

usage() {
  cat <<'EOF'
Usage: bash scripts/smoke_cli.sh [options]

Runs a local smoke pass for the current Astra repository.

Options:
  --python PATH       Python interpreter to use. Defaults to .venv/bin/python.
  --env-file PATH     Env file for --live-provider/--real. Defaults to <repo>/.env.
  --skip-pytest       Skip pytest and run only compile/help plus CLI smoke.
  --live-provider     Run one extra real provider prompt at the end.
  --real              Alias for --live-provider.
  --keep-temp         Keep the temporary HOME/workspace directory.
  -h, --help          Show this help text.

Environment:
  ASTRA_SMOKE_PYTHON       Default Python interpreter override.
  ASTRA_SMOKE_ENV_FILE     Env file override for --live-provider/--real.
  ASTRA_SMOKE_LIVE_MODEL   Model to use for --live-provider/--real.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      shift
      if [[ $# -eq 0 ]]; then
        echo "Missing value for --python" >&2
        exit 2
      fi
      PYTHON_BIN="$1"
      ;;
    --env-file)
      shift
      if [[ $# -eq 0 ]]; then
        echo "Missing value for --env-file" >&2
        exit 2
      fi
      ENV_FILE="$1"
      ;;
    --skip-pytest)
      RUN_PYTEST=0
      ;;
    --live-provider|--real)
      LIVE_PROVIDER=1
      ;;
    --keep-temp)
      KEEP_TEMP=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python interpreter not found or not executable: $PYTHON_BIN" >&2
  exit 1
fi

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/astra-smoke.XXXXXX")"
HOME_DIR="$TMP_ROOT/home"
WORKSPACE="$TMP_ROOT/workspace"

cleanup() {
  local exit_code=$?
  if [[ "$KEEP_TEMP" -eq 1 || "$exit_code" -ne 0 ]]; then
    echo "Temporary smoke directory: $TMP_ROOT" >&2
    return
  fi
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

log_step() {
  printf '\n==> %s\n' "$1"
}

assert_contains() {
  local file_path="$1"
  local expected="$2"
  if ! grep -Fq "$expected" "$file_path"; then
    echo "Expected to find '$expected' in $file_path" >&2
    echo "----- $file_path -----" >&2
    cat "$file_path" >&2
    echo "----------------------" >&2
    exit 1
  fi
}

run_and_capture() {
  local output_path="$1"
  shift
  "$@" >"$output_path" 2>&1
}

run_with_input() {
  local output_path="$1"
  local input_text="$2"
  shift 2
  printf '%s' "$input_text" | "$@" >"$output_path" 2>&1
}

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

create_workspace() {
  mkdir -p "$HOME_DIR" "$WORKSPACE/.astra/prompts" "$WORKSPACE/.astra/skills/review"

  cat >"$WORKSPACE/.env" <<'EOF'
OPENAI_API_KEY=test-key
EOF

  cat >"$WORKSPACE/.astra/config.yaml" <<'EOF'
model: smoke-config-model
system_prompt: Smoke config system prompt.

tools:
  enabled: [read, write, edit, ls, find, grep, bash]
  defaults:
    read:
      max_lines: 250
    bash:
      timeout_seconds: 15
      max_output_bytes: 8192

prompts:
  order:
    - builtin:base
    - config:system
    - prompt:repo-rules

capabilities:
  skills:
    enabled: []
EOF

  cat >"$WORKSPACE/.astra/prompts/repo-rules.md" <<'EOF'
Repo rules prompt body.
EOF

  cat >"$WORKSPACE/.astra/skills/review/skill.yaml" <<'EOF'
name: review
summary: Review checklist
prompt_files:
  - checklist.md
EOF

  cat >"$WORKSPACE/.astra/skills/review/checklist.md" <<'EOF'
Review checklist prompt body.
EOF

  cat >"$WORKSPACE/note.txt" <<'EOF'
live smoke sentinel 4731
EOF
}

prepare_live_provider_workspace_env() {
  local env_target="$1"
  local workspace_env="$WORKSPACE/.env"

  if [[ -n "${OPENAI_API_KEY:-}" && "${OPENAI_API_KEY:-}" != "test-key" ]]; then
    return 0
  fi

  if [[ ! -f "$env_target" ]]; then
    echo "Live provider mode needs a real OPENAI_API_KEY in the shell or an env file at: $env_target" >&2
    exit 1
  fi

  rm -f "$workspace_env"
  ln -s "$env_target" "$workspace_env"
}

resolve_session_ids() {
  HOME="$HOME_DIR" "$PYTHON_BIN" -c '
import json
import os
from pathlib import Path

base = Path(os.environ["HOME"]) / ".astra-python" / "sessions"
original = None
forked = None
for path in base.glob("*.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("parent_session_id"):
        forked = data["id"]
    else:
        original = data["id"]
if not original or not forked:
    raise SystemExit("Could not resolve original/forked session ids")
print(original)
print(forked)
'
}

create_seed_session() {
  HOME="$HOME_DIR" "$PYTHON_BIN" -c '
import os
from pathlib import Path

from astra.session import SessionStore

workspace = Path(os.environ["WORKSPACE"])
store = SessionStore()
session = store.create(cwd=str(workspace), model="seed-model", system_prompt="seed-system", name="seed")
store.save(session)
print(session.id)
'
}

run_local_cli_smoke() {
  local first_output="$TMP_ROOT/cli-first.txt"
  local second_output="$TMP_ROOT/cli-second.txt"
  local third_output="$TMP_ROOT/cli-third.txt"
  local help_output="$TMP_ROOT/help.txt"
  local seed_session_id

  export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
  export WORKSPACE

  log_step "Compile sources"
  (cd "$ROOT_DIR" && "$PYTHON_BIN" -m compileall src)

  log_step "CLI help"
  (cd "$ROOT_DIR" && run_and_capture "$help_output" "$PYTHON_BIN" -m astra --help)
  assert_contains "$help_output" "usage: astra"

  if [[ "$RUN_PYTEST" -eq 1 ]]; then
    log_step "Unit and integration tests"
    (cd "$ROOT_DIR" && "$PYTHON_BIN" -m pytest -q tests/unit tests/integration -m "not contract")
  fi

  create_workspace
  seed_session_id="$(create_seed_session)"

  log_step "Scripted CLI session"
  (cd "$ROOT_DIR" && HOME="$HOME_DIR" run_with_input "$first_output" $'/help\n/tools\n/runtime\n/runtime warnings\n/runtime json\n/runtime prompt\n/runtime json prompt\n/model smoke-cli-model\n/base-url http://cli-gateway.local/v1\n/skill:review\n/template:repo-rules\n/runtime prompt\n/reload\n/reload code\n/fork smoke-copy\n/rename smoke-main\n/save\n/sessions\n/exit\n' "$PYTHON_BIN" -m astra --cwd "$WORKSPACE" --session "$seed_session_id")

  assert_contains "$first_output" "Session "
  assert_contains "$first_output" "Tools summary"
  assert_contains "$first_output" "Runtime summary"
  assert_contains "$first_output" "No runtime warnings"
  assert_contains "$first_output" "\"prompt\""
  assert_contains "$first_output" "Runtime prompt"
  assert_contains "$first_output" "Model set to smoke-cli-model"
  assert_contains "$first_output" "Base URL set to http://cli-gateway.local/v1"
  assert_contains "$first_output" "Activated skill: review"
  assert_contains "$first_output" "Activated template: repo-rules"
  assert_contains "$first_output" "Review checklist prompt body."
  assert_contains "$first_output" "Repo rules prompt body."
  assert_contains "$first_output" "Reloaded runtime configuration."
  assert_contains "$first_output" "Code modules reloaded."
  assert_contains "$first_output" "Forked to "
  assert_contains "$first_output" "Renamed to smoke-main"
  assert_contains "$first_output" "Saved "
  assert_contains "$first_output" "smoke-main"

  mapfile -t session_ids < <(resolve_session_ids)
  if [[ "${#session_ids[@]}" -ne 2 ]]; then
    echo "Expected exactly 2 session ids, got ${#session_ids[@]}" >&2
    exit 1
  fi

  log_step "Session resume smoke"
  (cd "$ROOT_DIR" && HOME="$HOME_DIR" run_with_input "$second_output" $'/resume\n1\n/exit\n' "$PYTHON_BIN" -m astra --cwd "$WORKSPACE")
  assert_contains "$second_output" "Runtime config"
  assert_contains "$second_output" "Resumed "

  log_step "Session switch smoke"
  (cd "$ROOT_DIR" && HOME="$HOME_DIR" run_with_input "$third_output" "/switch ${session_ids[0]}"$'\n/exit\n' "$PYTHON_BIN" -m astra --cwd "$WORKSPACE" --session "${session_ids[1]}")
  assert_contains "$third_output" "Switched to ${session_ids[0]}"
}

run_live_provider_smoke() {
  local live_output="$TMP_ROOT/live-provider.txt"
  local live_model="${ASTRA_SMOKE_LIVE_MODEL:-}"
  local -a cmd=("$PYTHON_BIN" -m astra --cwd "$WORKSPACE")

  if [[ -n "$live_model" ]]; then
    cmd+=(--model "$live_model")
  fi

  prepare_live_provider_workspace_env "$ENV_FILE"

  if [[ -z "${OPENAI_API_KEY:-}" || "${OPENAI_API_KEY:-}" == "test-key" ]]; then
    log_step "Live provider env source"
    echo "Using workspace .env symlink: $WORKSPACE/.env -> $ENV_FILE"
  else
    log_step "Live provider env source"
    echo "Using current shell OPENAI_* environment"
  fi

  log_step "Live provider smoke"
  (
    cd "$ROOT_DIR" &&
      HOME="$HOME_DIR" "${cmd[@]}" \
        "Use the read tool to read note.txt, then repeat its exact contents in one sentence."
  ) >"$live_output" 2>&1

  assert_contains "$live_output" "[tool:read]"
  assert_contains "$live_output" "[tool-result:read]"
  assert_contains "$live_output" "live smoke sentinel 4731"
}

run_local_cli_smoke

if [[ "$LIVE_PROVIDER" -eq 1 ]]; then
  run_live_provider_smoke
fi

printf '\nSmoke script completed successfully.\n'
