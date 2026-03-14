#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PYTHON = ROOT_DIR / ".venv" / "bin" / "python"
DEFAULT_ENV_FILE = ROOT_DIR / ".env"


class SmokeError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="smoke_cli.py",
        description="Run a local smoke pass for the current Astra repository.",
    )
    parser.add_argument(
        "--python",
        default=os.environ.get("ASTRA_SMOKE_PYTHON", str(DEFAULT_PYTHON)),
        help="Python interpreter to use. Defaults to .venv/bin/python.",
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get("ASTRA_SMOKE_ENV_FILE", str(DEFAULT_ENV_FILE)),
        help="Env file for --live-provider/--real. Defaults to <repo>/.env.",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip pytest and run only compile/help plus CLI smoke.",
    )
    parser.add_argument(
        "--live-provider",
        "--real",
        action="store_true",
        dest="live_provider",
        help="Run one extra real provider prompt at the end.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary HOME/workspace directory.",
    )
    return parser.parse_args(argv)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def log_step(title: str) -> None:
    print(f"\n==> {title}")
    sys.stdout.flush()


def run_command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT_DIR,
    input_text: str | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=capture,
        text=True,
        check=False,
    )


def run_checked(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT_DIR,
    input_text: str | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = run_command(args, env=env, cwd=cwd, input_text=input_text, capture=capture)
    if result.returncode != 0:
        if capture:
            if result.stdout:
                print(result.stdout, end="", file=sys.stderr)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
        raise SmokeError(f"Command failed with exit code {result.returncode}: {' '.join(args)}")
    return result


def assert_contains(text: str, expected: str, label: str) -> None:
    if expected not in text:
        raise SmokeError(f"Expected to find {expected!r} in {label}")


def base_env(home_dir: Path, workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["WORKSPACE"] = str(workspace)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT_DIR / "src") if not pythonpath else f"{ROOT_DIR / 'src'}{os.pathsep}{pythonpath}"
    return env


def create_workspace(workspace: Path, home_dir: Path) -> None:
    home_dir.mkdir(parents=True, exist_ok=True)
    (workspace / ".astra" / "prompts").mkdir(parents=True, exist_ok=True)
    (workspace / ".astra" / "skills" / "review").mkdir(parents=True, exist_ok=True)

    write_text(
        workspace / ".env",
        """
        OPENAI_API_KEY=test-key
        """,
    )
    write_text(
        workspace / ".astra" / "config.yaml",
        """
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
        """,
    )
    write_text(
        workspace / ".astra" / "prompts" / "repo-rules.md",
        """
        Repo rules prompt body.
        """,
    )
    write_text(
        workspace / ".astra" / "skills" / "review" / "skill.yaml",
        """
        name: review
        summary: Review checklist
        prompt_files:
          - checklist.md
        """,
    )
    write_text(
        workspace / ".astra" / "skills" / "review" / "checklist.md",
        """
        Review checklist prompt body.
        """,
    )
    write_text(
        workspace / "note.txt",
        """
        live smoke sentinel 4731
        """,
    )


def create_seed_session(python_bin: Path, env: dict[str, str]) -> str:
    result = run_checked(
        [str(python_bin), "-c", _CREATE_SEED_SESSION_SNIPPET],
        env=env,
        capture=True,
    )
    return result.stdout.strip()


def resolve_session_ids(python_bin: Path, env: dict[str, str]) -> list[str]:
    result = run_checked(
        [str(python_bin), "-c", _RESOLVE_SESSION_IDS_SNIPPET],
        env=env,
        capture=True,
    )
    session_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(session_ids) != 2:
        raise SmokeError(f"Expected exactly 2 session ids, got {len(session_ids)}")
    return session_ids


def prepare_live_provider_workspace_env(workspace: Path, env_target: Path) -> None:
    current_key = os.environ.get("OPENAI_API_KEY", "")
    if current_key and current_key != "test-key":
        return

    if not env_target.is_file():
        raise SmokeError(
            f"Live provider mode needs a real OPENAI_API_KEY in the shell or an env file at: {env_target}"
        )

    workspace_env = workspace / ".env"
    if workspace_env.exists() or workspace_env.is_symlink():
        workspace_env.unlink()
    workspace_env.symlink_to(env_target)


def run_local_cli_smoke(
    python_bin: Path,
    temp_root: Path,
    home_dir: Path,
    workspace: Path,
    *,
    run_pytest: bool,
) -> None:
    env = base_env(home_dir, workspace)

    log_step("Compile sources")
    run_checked([str(python_bin), "-m", "compileall", "src"], env=env)

    log_step("CLI help")
    help_result = run_checked([str(python_bin), "-m", "astra", "--help"], env=env, capture=True)
    assert_contains(help_result.stdout, "usage: astra", "CLI help output")

    if run_pytest:
        log_step("Unit and integration tests")
        run_checked(
            [
                str(python_bin),
                "-m",
                "pytest",
                "-q",
                "tests/unit",
                "tests/integration",
                "-m",
                "not contract",
            ],
            env=env,
        )

    create_workspace(workspace, home_dir)
    seed_session_id = create_seed_session(python_bin, env)

    log_step("Scripted CLI session")
    first_result = run_checked(
        [str(python_bin), "-m", "astra", "--cwd", str(workspace), "--session", seed_session_id],
        env=env,
        input_text=_FIRST_SESSION_INPUT,
        capture=True,
    )
    first_output = first_result.stdout
    first_output_path = temp_root / "cli-first.txt"
    first_output_path.write_text(first_output, encoding="utf-8")

    for expected in [
        "Session ",
        "Tools summary",
        "Runtime summary",
        "No runtime warnings",
        '"prompt"',
        "Runtime prompt",
        "Model set to smoke-cli-model",
        "Base URL set to http://cli-gateway.local/v1",
        "Activated skill: review",
        "Activated template: repo-rules",
        "Review checklist prompt body.",
        "Repo rules prompt body.",
        "Reloaded runtime configuration.",
        "Code modules reloaded.",
        "Forked to ",
        "Renamed to smoke-main",
        "Saved ",
        "smoke-main",
    ]:
        assert_contains(first_output, expected, str(first_output_path))

    session_ids = resolve_session_ids(python_bin, env)

    log_step("Session resume smoke")
    second_result = run_checked(
        [str(python_bin), "-m", "astra", "--cwd", str(workspace)],
        env=env,
        input_text="/resume\n1\n/exit\n",
        capture=True,
    )
    second_output = second_result.stdout
    second_output_path = temp_root / "cli-second.txt"
    second_output_path.write_text(second_output, encoding="utf-8")
    assert_contains(second_output, "Runtime config", str(second_output_path))
    assert_contains(second_output, "Resumed ", str(second_output_path))

    log_step("Session switch smoke")
    third_result = run_checked(
        [
            str(python_bin),
            "-m",
            "astra",
            "--cwd",
            str(workspace),
            "--session",
            session_ids[1],
        ],
        env=env,
        input_text=f"/switch {session_ids[0]}\n/exit\n",
        capture=True,
    )
    third_output = third_result.stdout
    third_output_path = temp_root / "cli-third.txt"
    third_output_path.write_text(third_output, encoding="utf-8")
    assert_contains(third_output, f"Switched to {session_ids[0]}", str(third_output_path))


def run_live_provider_smoke(
    python_bin: Path,
    home_dir: Path,
    workspace: Path,
    temp_root: Path,
    env_file: Path,
) -> None:
    prepare_live_provider_workspace_env(workspace, env_file)
    env = base_env(home_dir, workspace)

    if not os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") == "test-key":
        log_step("Live provider env source")
        print(f"Using workspace .env symlink: {workspace / '.env'} -> {env_file}")
    else:
        log_step("Live provider env source")
        print("Using current shell OPENAI_* environment")

    cmd = [str(python_bin), "-m", "astra", "--cwd", str(workspace)]
    live_model = os.environ.get("ASTRA_SMOKE_LIVE_MODEL", "")
    if live_model:
        cmd.extend(["--model", live_model])

    log_step("Live provider smoke")
    live_result = run_checked(
        cmd + ["Use the read tool to read note.txt, then repeat its exact contents in one sentence."],
        env=env,
        capture=True,
    )
    live_output = live_result.stdout
    live_output_path = temp_root / "live-provider.txt"
    live_output_path.write_text(live_output, encoding="utf-8")

    assert_contains(live_output, "[tool:read]", str(live_output_path))
    assert_contains(live_output, "[tool-result:read]", str(live_output_path))
    assert_contains(live_output, "live smoke sentinel 4731", str(live_output_path))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    python_bin = Path(args.python).expanduser()
    env_file = Path(args.env_file).expanduser()
    keep_temp = args.keep_temp
    should_cleanup = True

    if not python_bin.exists() or not os.access(python_bin, os.X_OK):
        print(f"Python interpreter not found or not executable: {python_bin}", file=sys.stderr)
        return 1

    temp_root = Path(tempfile.mkdtemp(prefix="astra-smoke."))
    home_dir = temp_root / "home"
    workspace = temp_root / "workspace"

    try:
        run_local_cli_smoke(
            python_bin,
            temp_root,
            home_dir,
            workspace,
            run_pytest=not args.skip_pytest,
        )
        if args.live_provider:
            run_live_provider_smoke(python_bin, home_dir, workspace, temp_root, env_file)
    except SmokeError as exc:
        should_cleanup = False
        print(exc, file=sys.stderr)
        print(f"Temporary smoke directory: {temp_root}", file=sys.stderr)
        return 1
    finally:
        if keep_temp:
            should_cleanup = False
            print(f"Temporary smoke directory: {temp_root}", file=sys.stderr)
        if should_cleanup:
            shutil.rmtree(temp_root, ignore_errors=True)

    print("\nSmoke script completed successfully.")
    return 0


_CREATE_SEED_SESSION_SNIPPET = """
import os
from pathlib import Path

from astra.session import SessionStore

workspace = Path(os.environ["WORKSPACE"])
store = SessionStore()
session = store.create(cwd=str(workspace), model="seed-model", system_prompt="seed-system", name="seed")
store.save(session)
print(session.id)
"""


_RESOLVE_SESSION_IDS_SNIPPET = """
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
"""


_FIRST_SESSION_INPUT = (
    "/help\n"
    "/tools\n"
    "/runtime\n"
    "/runtime warnings\n"
    "/runtime json\n"
    "/runtime prompt\n"
    "/runtime json prompt\n"
    "/model smoke-cli-model\n"
    "/base-url http://cli-gateway.local/v1\n"
    "/skill:review\n"
    "/template:repo-rules\n"
    "/runtime prompt\n"
    "/reload\n"
    "/reload code\n"
    "/fork smoke-copy\n"
    "/rename smoke-main\n"
    "/save\n"
    "/sessions\n"
    "/exit\n"
)


if __name__ == "__main__":
    raise SystemExit(main())
