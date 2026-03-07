from __future__ import annotations

import argparse
import importlib
import os
import signal
import sys
from pathlib import Path

from .agent import Agent, AgentConfig
from .config import ConfigError, ConfigManager, ResolvedRuntimeConfig, RuntimeConfig, ToolRuntimeConfig, resolve_runtime_config
from .session import SessionStore


def print_help() -> None:
    print("/help")
    print("/model [name]")
    print("/base-url [url]")
    print("/tools")
    print("/sessions")
    print("/switch <session-id>")
    print("/fork [name]")
    print("/rename <name>")
    print("/reload")
    print("/reload code")
    print("/save")
    print("/exit")


def print_sessions(store: SessionStore) -> None:
    sessions = store.list()
    if not sessions:
        print("No sessions")
        return
    for session in sessions:
        name = session.name or "(unnamed)"
        parent = f" parent={session.parent_session_id}" if session.parent_session_id else ""
        print(f"{session.id}  {name}  cwd={session.cwd}{parent}  updated={session.updated_at}")


def print_reload_summary(agent: Agent, message: str) -> None:
    print(message)
    enabled_tools = ", ".join(agent.tools) or "(none)"
    print(f"model={agent.config.model}")
    print(f"base_url={agent.config.base_url}")
    print(f"tools={enabled_tools}")
    print(f"read.max_lines={agent.runtime_config.tools.read_max_lines}")
    print(f"bash.timeout_seconds={agent.runtime_config.tools.bash_timeout_seconds}")
    print(f"bash.max_output_bytes={agent.runtime_config.tools.bash_max_output_bytes}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pyi")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--session")
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--system-prompt")
    parser.add_argument("prompt", nargs="*")
    return parser.parse_args(argv)


def stream_callback(event_type: str, payload: dict) -> None:
    if event_type == "text_delta":
        print(payload["delta"], end="", flush=True)
    elif event_type == "tool_call":
        print(f"\n[tool:{payload['name']}]", flush=True)
    elif event_type == "tool_result":
        print(f"\n[tool-result:{payload['name']}]", flush=True)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required")
    cwd = Path(args.cwd).resolve()
    store = SessionStore()
    config_manager = ConfigManager()

    def clone_tools_config(config: ToolRuntimeConfig) -> ToolRuntimeConfig:
        return ToolRuntimeConfig(
            enabled_tools=list(config.enabled_tools),
            read_max_lines=config.read_max_lines,
            bash_timeout_seconds=config.bash_timeout_seconds,
            bash_max_output_bytes=config.bash_max_output_bytes,
        )

    def clone_runtime_config(runtime: ResolvedRuntimeConfig) -> ResolvedRuntimeConfig:
        return ResolvedRuntimeConfig(
            model=runtime.model,
            base_url=runtime.base_url,
            system_prompt=runtime.system_prompt,
            tools=clone_tools_config(runtime.tools),
        )

    def load_runtime() -> ResolvedRuntimeConfig:
        nonlocal config_manager
        try:
            raw_config = config_manager.load(cwd)
        except ConfigError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            raw_config = RuntimeConfig()
        return resolve_runtime_config(raw_config, args.model, args.base_url, args.system_prompt, env=os.environ)

    runtime_config = load_runtime()
    agent = Agent(
        AgentConfig(
            model=runtime_config.model,
            api_key=api_key,
            base_url=runtime_config.base_url,
            cwd=cwd,
            system_prompt=runtime_config.system_prompt,
        ),
        session_store=store,
    )
    if args.session and not args.new_session:
        agent.load_session(args.session)
        startup_runtime = ResolvedRuntimeConfig(
            model=agent.config.model,
            base_url=runtime_config.base_url,
            system_prompt=agent.config.system_prompt,
            tools=clone_tools_config(runtime_config.tools),
        )
    else:
        startup_runtime = runtime_config
    startup_reload = agent.reload_runtime(startup_runtime)
    if not startup_reload.success:
        print(f"Warning: {startup_reload.message}", file=sys.stderr)

    def handle_sigint(_signum: int, _frame: object) -> None:
        if agent.is_streaming:
            agent.abort()
            print("\n[aborted]", flush=True)
            return
        raise KeyboardInterrupt

    def reload_runtime_from_config() -> None:
        if agent.is_streaming:
            print("Cannot reload while a response is streaming.")
            return
        resolved_runtime = load_runtime()
        result = agent.reload_runtime(resolved_runtime)
        if result.success:
            print_reload_summary(agent, result.message)
        else:
            print(f"Reload failed: {result.message}")

    def reload_code_modules() -> None:
        nonlocal agent, config_manager
        if agent.is_streaming:
            print("Cannot reload while a response is streaming.")
            return
        try:
            config_module = importlib.reload(importlib.import_module("pyi.config"))
            importlib.reload(importlib.import_module("pyi.tools"))
            importlib.reload(importlib.import_module("pyi.provider"))
            agent_module = importlib.reload(importlib.import_module("pyi.agent"))
        except Exception as exc:
            print(f"Code reload failed: {exc}")
            return
        config_manager = config_module.ConfigManager()
        runtime_after_code_reload = config_module.ResolvedRuntimeConfig(
            model=agent.config.model,
            base_url=agent.config.base_url,
            system_prompt=agent.config.system_prompt,
            tools=config_module.ToolRuntimeConfig(
                enabled_tools=list(agent.runtime_config.tools.enabled_tools),
                read_max_lines=agent.runtime_config.tools.read_max_lines,
                bash_timeout_seconds=agent.runtime_config.tools.bash_timeout_seconds,
                bash_max_output_bytes=agent.runtime_config.tools.bash_max_output_bytes,
            ),
        )
        new_agent = agent_module.Agent(
            agent_module.AgentConfig(
                model=agent.config.model,
                api_key=agent.config.api_key,
                base_url=agent.config.base_url,
                cwd=agent.config.cwd,
                system_prompt=agent.config.system_prompt,
            ),
            session_store=agent.session_store,
        )
        new_agent.session = agent.session
        new_agent.pending_tool_calls = set(agent.pending_tool_calls)
        new_agent.error = agent.error
        result = new_agent.reload_runtime(runtime_after_code_reload)
        agent = new_agent
        print("Code modules reloaded.")
        if result.success:
            reload_runtime_from_config()
        else:
            print(f"Reload failed after code reload: {result.message}")

    def handle_command(line: str) -> bool:
        nonlocal agent, args
        if line == "/help":
            print_help()
            return True
        if line == "/reload":
            reload_runtime_from_config()
            return True
        if line == "/reload code":
            reload_code_modules()
            return True
        if line.startswith("/model"):
            parts = line.split(maxsplit=1)
            if len(parts) == 1:
                print(agent.config.model)
            else:
                args.model = parts[1].strip()
                new_runtime = ResolvedRuntimeConfig(
                    model=args.model,
                    base_url=agent.config.base_url,
                    system_prompt=agent.config.system_prompt,
                    tools=clone_tools_config(agent.runtime_config.tools),
                )
                result = agent.reload_runtime(new_runtime)
                if result.success:
                    print(f"Model set to {agent.config.model}")
                else:
                    print(f"Failed to set model: {result.message}")
            return True
        if line.startswith("/base-url"):
            parts = line.split(maxsplit=1)
            if len(parts) == 1:
                print(agent.config.base_url)
            else:
                args.base_url = parts[1].strip()
                new_runtime = ResolvedRuntimeConfig(
                    model=agent.config.model,
                    base_url=args.base_url,
                    system_prompt=agent.config.system_prompt,
                    tools=clone_tools_config(agent.runtime_config.tools),
                )
                result = agent.reload_runtime(new_runtime)
                if result.success:
                    print(f"Base URL set to {agent.config.base_url}")
                else:
                    print(f"Failed to set base URL: {result.message}")
            return True
        if line == "/tools":
            print_reload_summary(agent, "Current runtime")
            return True
        if line == "/sessions":
            print_sessions(store)
            return True
        if line.startswith("/switch "):
            session_id = line.split(maxsplit=1)[1].strip()
            agent.load_session(session_id)
            switched_runtime = ResolvedRuntimeConfig(
                model=agent.config.model,
                base_url=agent.config.base_url,
                system_prompt=agent.config.system_prompt,
                tools=clone_tools_config(agent.runtime_config.tools),
            )
            agent.reload_runtime(switched_runtime)
            print(f"Switched to {agent.session.id}")
            return True
        if line.startswith("/fork"):
            parts = line.split(maxsplit=1)
            name = parts[1].strip() if len(parts) > 1 else None
            new_id = agent.fork_session(name=name)
            print(f"Forked to {new_id}")
            return True
        if line.startswith("/rename "):
            agent.session.name = line.split(maxsplit=1)[1].strip()
            agent.save_session()
            print(f"Renamed to {agent.session.name}")
            return True
        if line == "/save":
            agent.save_session()
            print(f"Saved {agent.session.id}")
            return True
        if line == "/exit":
            raise EOFError
        return False

    signal.signal(signal.SIGINT, handle_sigint)

    if args.prompt:
        text = " ".join(args.prompt).strip()
        if text:
            result = agent.prompt(text, on_event=stream_callback)
            print()
            if result.error:
                print(result.error, file=sys.stderr)
                raise SystemExit(1)
        return

    print(f"Session {agent.session.id}")
    print_help()
    while True:
        try:
            line = input("pyi> ").strip()
            if not line:
                continue
            if line.startswith("/") and handle_command(line):
                continue
            result = agent.prompt(line, on_event=stream_callback)
            print()
            if result.error:
                print(result.error, file=sys.stderr)
        except EOFError:
            print()
            agent.save_session()
            return
        except KeyboardInterrupt:
            print()
