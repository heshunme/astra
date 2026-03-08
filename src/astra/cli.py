from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
from pathlib import Path

from .agent import Agent, AgentConfig
from .config import ConfigError, ConfigManager, ResolvedRuntimeConfig, RuntimeConfig, clone_resolved_runtime_config, resolve_runtime_config
from .runtime import CapabilityRuntime, CommandRegistry, CommandSpec, PrefixCommandSpec
from .session import SessionStore


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="astra")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--session")
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--system-prompt")
    parser.add_argument("prompt", nargs="*")
    return parser.parse_args(argv)


def stream_callback(event_type: str, payload: dict[str, object]) -> None:
    if event_type == "text_delta":
        delta = payload.get("delta", "")
        if isinstance(delta, str):
            print(delta, end="", flush=True)
    elif event_type == "tool_call":
        name = payload.get("name", "unknown")
        print(f"\n[tool:{name}]", flush=True)
    elif event_type == "tool_result":
        name = payload.get("name", "unknown")
        print(f"\n[tool-result:{name}]", flush=True)


def print_sessions(store: SessionStore) -> None:
    sessions = store.list()
    if not sessions:
        print("No sessions")
        return
    for session in sessions:
        name = session.name or "(unnamed)"
        parent = f" parent={session.parent_session_id}" if session.parent_session_id else ""
        print(f"{session.id}  {name}  cwd={session.cwd}{parent}  updated={session.updated_at}")


def print_reload_summary(agent: Agent, message: str, warnings: list[str] | None = None) -> None:
    snapshot = agent.runtime.snapshot()
    print(message)
    enabled_tools = ", ".join(agent.tools) or "(none)"
    active_skills = ", ".join(agent.active_skills) or "(none)"
    active_templates = ", ".join(agent.active_templates) or "(none)"
    print(f"model={agent.config.model}")
    print(f"base_url={agent.config.base_url}")
    print(f"tools={enabled_tools}")
    print(f"skills.active={active_skills}")
    print(f"templates.active={active_templates}")
    print(f"prompts.loaded={len(snapshot.diagnostics.loaded_prompts)}")
    print(f"skills.loaded={len(snapshot.diagnostics.loaded_skills)}")
    print(f"read.max_lines={agent.runtime_config.tools.read_max_lines}")
    print(f"bash.timeout_seconds={agent.runtime_config.tools.bash_timeout_seconds}")
    print(f"bash.max_output_bytes={agent.runtime_config.tools.bash_max_output_bytes}")
    for warning in warnings or []:
        print(f"warning={warning}")


def build_runtime_summary(agent: Agent) -> dict[str, object]:
    snapshot = agent.runtime.snapshot()
    warnings = agent.runtime.warnings()
    return {
        "model": agent.config.model,
        "base_url": agent.config.base_url,
        "tools": list(agent.tools),
        "prompts": {
            "order": list(agent.runtime_config.prompts.order),
            "available": agent.runtime.list_prompt_keys(),
            "loaded": list(snapshot.diagnostics.loaded_prompts),
        },
        "skills": {
            "available": agent.runtime.list_skill_names(),
            "active": list(agent.active_skills),
            "loaded": list(snapshot.diagnostics.loaded_skills),
        },
        "templates": {
            "available": agent.runtime.list_template_names(),
            "active": list(agent.active_templates),
        },
        "tool_defaults": {
            "read_max_lines": agent.runtime_config.tools.read_max_lines,
            "bash_timeout_seconds": agent.runtime_config.tools.bash_timeout_seconds,
            "bash_max_output_bytes": agent.runtime_config.tools.bash_max_output_bytes,
        },
        "warnings": warnings,
    }


def build_runtime_prompt_summary(agent: Agent) -> dict[str, object]:
    inspection = agent.inspect_prompt()
    return {
        "assembled": inspection.assembled,
        "char_length": len(inspection.assembled),
        "fragment_count": len(inspection.fragments),
        "fragments": [
            {
                "key": fragment.key,
                "source": fragment.source,
                "text_length": fragment.text_length,
            }
            for fragment in inspection.fragments
        ],
    }


def print_runtime_summary(agent: Agent, show_warnings_only: bool = False) -> None:
    summary = build_runtime_summary(agent)
    warnings = summary["warnings"]
    prompt_summary = summary["prompts"]
    skill_summary = summary["skills"]
    template_summary = summary["templates"]
    if not isinstance(warnings, list):
        warnings = []
    if show_warnings_only:
        if not warnings:
            print("No runtime warnings")
            return
        for warning in warnings:
            print(warning)
        return

    print("Runtime summary")
    print(f"tools={', '.join(agent.tools) or '(none)'}")
    print(f"prompts.order={', '.join(prompt_summary['order']) or '(none)'}")
    print(f"prompts.available={', '.join(prompt_summary['available']) or '(none)'}")
    print(f"skills.available={', '.join(skill_summary['available']) or '(none)'}")
    print(f"templates.available={', '.join(template_summary['available']) or '(none)'}")
    print(f"skills.active={', '.join(skill_summary['active']) or '(none)'}")
    print(f"templates.active={', '.join(template_summary['active']) or '(none)'}")
    print(f"prompts.loaded={', '.join(prompt_summary['loaded']) or '(none)'}")
    print(f"skills.loaded={', '.join(skill_summary['loaded']) or '(none)'}")
    print(f"warnings.count={len(warnings)}")


def print_runtime_prompt(agent: Agent) -> None:
    prompt_summary = build_runtime_prompt_summary(agent)
    fragments = prompt_summary["fragments"]
    print("Runtime prompt")
    print(f"fragments={prompt_summary['fragment_count']}")
    print(f"char_length={prompt_summary['char_length']}")
    if isinstance(fragments, list) and fragments:
        for index, fragment in enumerate(fragments, start=1):
            print(
                f"fragment[{index}]={fragment['key']} source={fragment['source']} chars={fragment['text_length']}"
            )
    else:
        print("fragments=(none)")
    print("assembled:")
    assembled = prompt_summary["assembled"]
    if isinstance(assembled, str) and assembled:
        print(assembled)
    else:
        print("(empty)")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required")
    cwd = Path(args.cwd).resolve()
    store = SessionStore()
    config_manager = ConfigManager()

    def load_runtime() -> ResolvedRuntimeConfig:
        nonlocal config_manager
        try:
            raw_config = config_manager.load(cwd)
        except ConfigError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            raw_config = RuntimeConfig()
        return resolve_runtime_config(raw_config, args.model, args.base_url, args.system_prompt, env=os.environ)

    runtime_config = load_runtime()
    capability_runtime = CapabilityRuntime(cwd)
    agent = Agent(
        AgentConfig(
            model=runtime_config.model,
            api_key=api_key,
            base_url=runtime_config.base_url,
            cwd=cwd,
            system_prompt=runtime_config.system_prompt,
        ),
        capability_runtime=capability_runtime,
        session_store=store,
    )
    if args.session and not args.new_session:
        agent.load_session(args.session)
        startup_runtime = clone_resolved_runtime_config(runtime_config)
        startup_runtime.model = agent.config.model
        startup_runtime.system_prompt = agent.config.system_prompt
    else:
        startup_runtime = runtime_config
    startup_reload = agent.reload_runtime(startup_runtime)
    if not startup_reload.success:
        print(f"Warning: {startup_reload.message}", file=sys.stderr)
    elif startup_reload.warnings:
        for warning in startup_reload.warnings:
            print(f"Warning: {warning}", file=sys.stderr)

    command_registry = CommandRegistry()

    def print_help() -> None:
        for line in command_registry.help_lines():
            print(line)

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
            print_reload_summary(agent, result.message, result.warnings)
        else:
            print(f"Reload failed: {result.message}")

    def reload_code_modules() -> None:
        nonlocal agent, config_manager
        if agent.is_streaming:
            print("Cannot reload while a response is streaming.")
            return
        try:
            config_module = importlib.reload(importlib.import_module("astra.config"))
            importlib.reload(importlib.import_module("astra.tools"))
            importlib.reload(importlib.import_module("astra.provider"))
            importlib.reload(importlib.import_module("astra.runtime.builtin_capabilities"))
            runtime_module = importlib.reload(importlib.import_module("astra.runtime.runtime"))
            agent_module = importlib.reload(importlib.import_module("astra.agent"))
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
            prompts=config_module.PromptRuntimeConfig(order=list(agent.runtime_config.prompts.order)),
            capabilities=config_module.CapabilitiesConfig(
                prompts=config_module.PromptCapabilityConfig(paths=list(agent.runtime_config.capabilities.prompts.paths)),
                skills=config_module.SkillCapabilityConfig(
                    paths=list(agent.runtime_config.capabilities.skills.paths),
                    enabled=list(agent.runtime_config.capabilities.skills.enabled),
                ),
            ),
        )
        new_runtime = runtime_module.CapabilityRuntime(agent.config.cwd)
        new_agent = agent_module.Agent(
            agent_module.AgentConfig(
                model=agent.config.model,
                api_key=agent.config.api_key,
                base_url=agent.config.base_url,
                cwd=agent.config.cwd,
                system_prompt=agent.config.system_prompt,
            ),
            capability_runtime=new_runtime,
            session_store=agent.session_store,
        )
        new_agent.session = agent.session
        new_agent.pending_tool_calls = set(agent.pending_tool_calls)
        new_agent.error = agent.error
        new_agent._session_prompt_states = {  # type: ignore[attr-defined]
            session_id: agent_module.SessionPromptState(
                skills=list(state.skills),
                templates=list(state.templates),
            )
            for session_id, state in agent._session_prompt_states.items()
        }
        result = new_agent.reload_runtime(runtime_after_code_reload)
        agent = new_agent
        print("Code modules reloaded.")
        if result.success:
            reload_runtime_from_config()
        else:
            print(f"Reload failed after code reload: {result.message}")

    def register_commands() -> None:
        def help_command(_line: str) -> bool:
            print_help()
            return True

        def reload_command(line: str) -> bool:
            command_name, _, remainder = line.partition(" ")
            if command_name != "/reload":
                return False
            if remainder == "code":
                reload_code_modules()
                return True
            if remainder:
                return False
            reload_runtime_from_config()
            return True

        def model_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if not remainder:
                print(agent.config.model)
                return True
            args.model = remainder.strip()
            new_runtime = clone_resolved_runtime_config(agent.runtime_config)
            new_runtime.model = args.model
            result = agent.reload_runtime(new_runtime)
            if result.success:
                print(f"Model set to {agent.config.model}")
            else:
                print(f"Failed to set model: {result.message}")
            return True

        def base_url_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if not remainder:
                print(agent.config.base_url)
                return True
            args.base_url = remainder.strip()
            new_runtime = clone_resolved_runtime_config(agent.runtime_config)
            new_runtime.base_url = args.base_url
            result = agent.reload_runtime(new_runtime)
            if result.success:
                print(f"Base URL set to {agent.config.base_url}")
            else:
                print(f"Failed to set base URL: {result.message}")
            return True

        def tools_command(_line: str) -> bool:
            print_reload_summary(agent, "Current runtime")
            return True

        def runtime_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            normalized_remainder = remainder.strip()
            if normalized_remainder == "json":
                print(json.dumps(build_runtime_summary(agent), ensure_ascii=False, indent=2))
                return True
            if normalized_remainder == "json prompt":
                payload = build_runtime_summary(agent)
                payload["prompt"] = build_runtime_prompt_summary(agent)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return True
            if normalized_remainder == "warnings":
                print_runtime_summary(agent, show_warnings_only=True)
                return True
            if normalized_remainder == "prompt":
                print_runtime_prompt(agent)
                return True
            if normalized_remainder:
                return False
            print_runtime_summary(agent)
            return True

        def sessions_command(_line: str) -> bool:
            print_sessions(store)
            return True

        def switch_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            session_id = remainder.strip()
            if not session_id:
                return False
            agent.load_session(session_id)
            switched_runtime = clone_resolved_runtime_config(agent.runtime_config)
            switched_runtime.model = agent.config.model
            switched_runtime.system_prompt = agent.config.system_prompt
            result = agent.reload_runtime(switched_runtime)
            if not result.success:
                print(f"Failed to switch: {result.message}")
                return True
            print(f"Switched to {agent.session.id}")
            return True

        def fork_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            name = remainder.strip() or None
            new_id = agent.fork_session(name=name)
            print(f"Forked to {new_id}")
            return True

        def rename_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            name = remainder.strip()
            if not name:
                return False
            agent.session.name = name
            agent.save_session()
            print(f"Renamed to {agent.session.name}")
            return True

        def save_command(_line: str) -> bool:
            agent.save_session()
            print(f"Saved {agent.session.id}")
            return True

        def exit_command(_line: str) -> bool:
            raise EOFError

        def skill_prefix_command(_line: str, suffix: str) -> bool:
            name = suffix.strip()
            if not name:
                return False
            success, message = agent.activate_skill(name)
            print(message)
            return True

        def template_prefix_command(_line: str, suffix: str) -> bool:
            name = suffix.strip()
            if not name:
                return False
            success, message = agent.activate_template(name)
            print(message)
            return True

        command_registry.register(CommandSpec(name="/help", usage="/help", summary="Show help", handler=help_command))
        command_registry.register(
            CommandSpec(name="/reload", usage="/reload | /reload code", summary="Reload runtime or code", handler=reload_command)
        )
        command_registry.register(CommandSpec(name="/model", usage="/model [name]", summary="Show or set model", handler=model_command))
        command_registry.register(
            CommandSpec(name="/base-url", usage="/base-url [url]", summary="Show or set base URL", handler=base_url_command)
        )
        command_registry.register(CommandSpec(name="/tools", usage="/tools", summary="Show runtime summary", handler=tools_command))
        command_registry.register(
            CommandSpec(name="/runtime", usage="/runtime | /runtime warnings | /runtime json | /runtime prompt | /runtime json prompt", summary="Show capability runtime state", handler=runtime_command)
        )
        command_registry.register(
            CommandSpec(name="/sessions", usage="/sessions", summary="List saved sessions", handler=sessions_command)
        )
        command_registry.register(
            CommandSpec(name="/switch", usage="/switch <session-id>", summary="Switch sessions", handler=switch_command)
        )
        command_registry.register(CommandSpec(name="/fork", usage="/fork [name]", summary="Fork the current session", handler=fork_command))
        command_registry.register(
            CommandSpec(name="/rename", usage="/rename <name>", summary="Rename the current session", handler=rename_command)
        )
        command_registry.register(CommandSpec(name="/save", usage="/save", summary="Save the current session", handler=save_command))
        command_registry.register(CommandSpec(name="/exit", usage="/exit", summary="Exit the CLI", handler=exit_command))
        command_registry.register_prefix(
            PrefixCommandSpec(
                prefix="/skill:",
                usage="/skill:<name>",
                summary="Activate a discovered skill for the current process session",
                handler=skill_prefix_command,
            )
        )
        command_registry.register_prefix(
            PrefixCommandSpec(
                prefix="/template:",
                usage="/template:<name>",
                summary="Activate a discovered prompt template for the current process session",
                handler=template_prefix_command,
            )
        )

    register_commands()
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
            line = input("astra> ").strip()
            if not line:
                continue
            if line.startswith("/") and command_registry.dispatch(line):
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
