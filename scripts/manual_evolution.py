#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astra.agent import Agent, AgentConfig
from astra.config import ConfigManager, ResolvedRuntimeConfig, RuntimeConfig, merged_env, resolve_runtime_config
from astra.evolution import EvolutionRequest, SkillEvolutionService
from astra.runtime import CapabilityRuntime

DEFAULT_ENV_FILE = ROOT_DIR / ".env"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manual_evolution.py",
        description="Run a real-provider manual evolution pass from an Agent snapshot.",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Env file used when the current shell does not already provide a real OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ASTRA_EVOLUTION_MODEL", ""),
        help="Optional model override. Defaults to config/env resolution.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ASTRA_EVOLUTION_BASE_URL", ""),
        help="Optional base URL override. Defaults to config/env resolution.",
    )
    parser.add_argument(
        "--goal",
        default="Capture a reusable code review workflow from a real provider run.",
        help="Evolution goal recorded into the generated skill guidance.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary HOME/workspace directory after the script exits.",
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


def create_workspace(workspace: Path, env_file: Path | None) -> str:
    env_mode = "shell"
    if env_file is not None:
        env_mode = link_or_copy_env(env_file, workspace / ".env")

    write_text(
        workspace / ".astra" / "config.yaml",
        """
        system_prompt: Manual evolution test workspace. Verify files before making claims.

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

        capabilities:
          prompts:
            paths: []
          skills:
            paths: []
        """,
    )
    write_text(
        workspace / "src" / "demo.py",
        """
        def divide(total: int, count: int) -> float:
            return total / count


        def render_report(items: list[str]) -> str:
            output = "Items:\\n"
            for item in items:
                output += f"- {item}\\n"
            return output
        """,
    )
    write_text(
        workspace / "README.md",
        """
        Manual evolution workspace.
        Focus on code review style tasks.
        """,
    )
    return env_mode


def build_runtime(cwd: Path, args: argparse.Namespace) -> tuple[dict[str, str], ResolvedRuntimeConfig]:
    env = merged_env(cwd, env=os.environ)
    config_manager = ConfigManager()
    try:
        raw_config = config_manager.load(cwd)
    except Exception:
        raw_config = RuntimeConfig()
    runtime_config = resolve_runtime_config(
        raw_config,
        args.model or None,
        args.base_url or None,
        None,
        env=env,
    )
    return env, runtime_config


def print_step(title: str) -> None:
    print(f"\n==> {title}")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_file = Path(args.env_file).expanduser().resolve()
    current_key = os.environ.get("OPENAI_API_KEY", "")
    use_env_file = not current_key or current_key == "test-key"
    if use_env_file and not env_file.exists():
        print(
            "A real OPENAI_API_KEY is required either in the shell or via --env-file.",
            file=sys.stderr,
        )
        return 1

    temp_root = Path(tempfile.mkdtemp(prefix="astra-evolution-manual-"))
    home_dir = temp_root / "home"
    workspace = temp_root / "workspace"
    home_dir.mkdir(parents=True, exist_ok=True)

    prior_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home_dir)
    env_mode = create_workspace(workspace, env_file if use_env_file else None)

    try:
        env, runtime_config = build_runtime(workspace, args)
        api_key = env.get("OPENAI_API_KEY", "")
        if not api_key or api_key == "test-key":
            print(
                "Resolved environment does not contain a real OPENAI_API_KEY.",
                file=sys.stderr,
            )
            return 1

        print(f"Temp root:  {temp_root}")
        print(f"Workspace:  {workspace}")
        print(f"Env source: {'shell' if env_mode == 'shell' else env_file}")
        print(f"Model:      {runtime_config.model}")
        print(f"Base URL:   {runtime_config.base_url}")

        print_step("Run real provider prompt")
        runtime = CapabilityRuntime(workspace)
        agent = Agent(
            AgentConfig(
                model=runtime_config.model,
                api_key=api_key,
                base_url=runtime_config.base_url,
                cwd=workspace,
                system_prompt=runtime_config.system_prompt,
            ),
            runtime,
        )
        reload_result = agent.apply_runtime_config(runtime_config)
        if not reload_result.success:
            print(f"Runtime reload failed: {reload_result.message}", file=sys.stderr)
            return 1
        prompt = (
            "Review src/demo.py for correctness and maintainability issues. "
            "Use the read tool first, then give a concise review."
        )
        result = agent.prompt(prompt)
        if result.error:
            print(f"Provider run failed: {result.error}", file=sys.stderr)
            return 1
        for message in result.assistant_messages:
            if message.content.strip():
                print(message.content.strip())

        print_step("Evolve skill from real snapshot")
        evolution = SkillEvolutionService(workspace)
        outcome = evolution.evolve(
            agent.snapshot(),
            EvolutionRequest(goal=args.goal),
        )
        print(f"created={outcome.created}")
        print(f"updated={outcome.updated}")
        print(f"skipped={outcome.skipped}")
        print(f"warnings={outcome.warnings}")
        print(f"skill_name={outcome.skill_name}")
        for path in outcome.written_files:
            print(f"wrote={path}")
        if outcome.skill_name is None:
            print("Evolution did not produce a skill.", file=sys.stderr)
            return 1

        print_step("Reload runtime and confirm discovery")
        runtime_snapshot = runtime.reload(runtime_config)
        print(f"loaded_skills={sorted(runtime_snapshot.skills)}")
        if outcome.skill_name not in runtime_snapshot.skills:
            print(
                f"Generated skill {outcome.skill_name} was not rediscovered after reload.",
                file=sys.stderr,
            )
            return 1

        skill_slug = outcome.skill_name.replace("_", "-")
        skill_dir = workspace / ".astra" / "skills" / skill_slug
        print(f"skill_dir={skill_dir}")
        print("checklist_preview:")
        print((skill_dir / "checklist.md").read_text(encoding="utf-8"))
        return 0
    finally:
        if prior_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prior_home
        if args.keep_temp:
            print(f"\nWorkspace kept at: {workspace}")
            print(f"Temp root kept at: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
