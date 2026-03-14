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
DEFAULT_ENV_FILE = ROOT_DIR / ".env"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manual_cli.py",
        description="Prepare a temporary Astra workspace and launch the CLI for manual testing.",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Env file to link into the temporary workspace. Defaults to <repo>/.env.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter used to launch `python -m astra`. Defaults to the current interpreter.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the temporary directory after the CLI exits successfully.",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Prepare the workspace but do not start the CLI.",
    )
    return parser.parse_args(argv)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")


def link_or_copy_env(env_file: Path, workspace_env: Path) -> str:
    workspace_env.parent.mkdir(parents=True, exist_ok=True)
    if workspace_env.exists() or workspace_env.is_symlink():
        workspace_env.unlink()
    try:
        workspace_env.symlink_to(env_file)
        return "symlink"
    except OSError:
        shutil.copy2(env_file, workspace_env)
        return "copy"


def create_workspace(workspace: Path, env_file: Path) -> str:
    env_mode = link_or_copy_env(env_file, workspace / ".env")

    write_text(
        workspace / ".astra" / "config.yaml",
        """
        system_prompt: Manual CLI test workspace. Prefer reading files before making claims.

        tools:
          enabled: [read, write, edit, ls, find, grep, bash]
          defaults:
            read:
              max_lines: 250
            bash:
              timeout_seconds: 20
              max_output_bytes: 16384

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
        """,
    )

    write_text(
        workspace / ".astra" / "prompts" / "repo-rules.md",
        """
        Repo rules prompt body.
        Treat this workspace as disposable, but still verify files before editing.
        """,
    )
    write_text(
        workspace / ".astra" / "prompts" / "pairing.md",
        """
        Pairing template body.
        Explain your next action before using tools, then keep responses concise.
        """,
    )
    write_text(
        workspace / ".astra" / "skills" / "review" / "skill.yaml",
        """
        name: review
        summary: Review checklist for manual testing
        prompt_files:
          - checklist.md
        context_files:
          - style.md
        """,
    )
    write_text(
        workspace / ".astra" / "skills" / "review" / "checklist.md",
        """
        Review checklist prompt body.
        Focus on correctness, regressions, and missing validation.
        """,
    )
    write_text(
        workspace / ".astra" / "skills" / "review" / "style.md",
        """
        Review context body.
        Prefer findings first, then open questions, then a short summary.
        """,
    )
    write_text(
        workspace / ".astra" / "skills" / "debug" / "skill.yaml",
        """
        name: debug
        summary: Debugging checklist for manual testing
        prompt_files:
          - checklist.md
        """,
    )
    write_text(
        workspace / ".astra" / "skills" / "debug" / "checklist.md",
        """
        Debug checklist prompt body.
        Reproduce first, isolate second, patch last.
        """,
    )

    write_text(
        workspace / "note.txt",
        """
        manual smoke sentinel 4731
        second line for read max_lines checks
        """,
    )
    write_text(
        workspace / "src" / "demo.py",
        """
        def greet(name: str) -> str:
            return f"hello, {name}"


        def main() -> None:
            print(greet("astra"))


        if __name__ == "__main__":
            main()
        """,
    )
    write_text(
        workspace / "docs" / "plan.md",
        """
        - verify runtime inspection
        - activate a skill
        - activate a template
        - ask the agent to read note.txt
        - try a small edit in src/demo.py
        """,
    )
    write_text(
        workspace / "logs" / "app.log",
        """
        2026-03-14 INFO startup complete
        2026-03-14 WARN manual test warning example
        """,
    )
    write_text(
        workspace / "MANUAL_TESTING.md",
        """
        Suggested commands:

        /tools
        /runtime
        /runtime warnings
        /runtime prompt
        /runtime json prompt
        /skill:review Review src/demo.py for issues.
        /skill:debug
        /template:pairing
        /reload
        /sessions

        Suggested prompts:

        Read note.txt and summarize it in one sentence.
        Find manual smoke sentinel 4731 in the workspace.
        Review src/demo.py for issues.
        Use bash to print the current directory and list files under docs.
        """,
    )
    return env_mode


def print_intro(temp_root: Path, workspace: Path, env_file: Path, env_mode: str) -> None:
    print(f"Temporary root: {temp_root}")
    print(f"Workspace:      {workspace}")
    print(f"Env source:     {env_file}")
    print(f"Env mode:       {env_mode}")
    print()
    print("Suggested slash commands:")
    print("  /tools")
    print("  /runtime")
    print("  /runtime prompt")
    print("  /runtime json prompt")
    print("  /skill:review Review src/demo.py for issues.")
    print("  /skill:debug")
    print("  /template:pairing")
    print("  /reload")
    print()
    print("Suggested prompts:")
    print("  Read note.txt and summarize it in one sentence.")
    print("  Find manual smoke sentinel 4731 in the workspace.")
    print("  Review src/demo.py for issues.")
    print("  Use bash to print the current directory and list files under docs.")
    print()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_file = Path(args.env_file).expanduser().resolve()
    python_bin = Path(args.python).expanduser()

    if not python_bin.exists():
        print(f"Python interpreter not found: {python_bin}", file=sys.stderr)
        return 1
    if not env_file.exists():
        print(f"Env file not found: {env_file}", file=sys.stderr)
        return 1

    temp_root = Path(tempfile.mkdtemp(prefix="astra-manual-"))
    home_dir = temp_root / "home"
    workspace = temp_root / "workspace"
    home_dir.mkdir(parents=True, exist_ok=True)

    env_mode = create_workspace(workspace, env_file)
    print_intro(temp_root, workspace, env_file, env_mode)
    sys.stdout.flush()

    result_code = 0
    if args.no_launch:
        print("Workspace prepared. CLI launch skipped.")
    else:
        env = os.environ.copy()
        env["HOME"] = str(home_dir)
        env["PYTHONPATH"] = str(ROOT_DIR / "src") + os.pathsep + env.get("PYTHONPATH", "")

        result = subprocess.run(
            [str(python_bin), "-m", "astra", "--cwd", str(workspace)],
            cwd=ROOT_DIR,
            env=env,
            check=False,
        )
        result_code = result.returncode

    if args.cleanup and result_code == 0:
        shutil.rmtree(temp_root, ignore_errors=True)
    else:
        print()
        print(f"Workspace kept at: {workspace}")
        print(f"Temp root kept at: {temp_root}")

    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
