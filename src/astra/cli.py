from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .agent import Agent, AgentConfig
from .config import (
    ConfigError,
    ConfigManager,
    DotenvError,
    ResolvedRuntimeConfig,
    RuntimeConfig,
    clone_resolved_runtime_config,
    merged_env,
    resolve_runtime_config,
)
from .models import AgentEvent, Session
from .runtime import CapabilityRuntime, CommandRegistry, CommandSpec
from .session import SessionStore, agent_snapshot_to_dict, agent_snapshot_from_dict, apply_agent_snapshot_to_session, session_to_agent_snapshot


AgentFactory = Callable[[AgentConfig, CapabilityRuntime], Agent]
RuntimeFactory = Callable[[Path], CapabilityRuntime]
SessionStoreFactory = Callable[[], SessionStore]
ConfigManagerFactory = Callable[[], ConfigManager]


@dataclass(slots=True)
class CliSessionState:
    session: Session
    materialized: bool = False


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


def stream_event(event: AgentEvent) -> None:
    if event.type == "message_update":
        delta = event.payload.get("delta", "")
        if isinstance(delta, str):
            print(delta, end="", flush=True)
    elif event.type == "tool_execution_start":
        name = event.payload.get("name", "unknown")
        print(f"\n[tool:{name}]", flush=True)
    elif event.type == "tool_execution_end":
        name = event.payload.get("name", "unknown")
        print(f"\n[tool-result:{name}]", flush=True)


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return f"{value[: width - 3]}..."


def _display_path(raw_path: str) -> str:
    try:
        path = Path(raw_path).resolve()
        home = Path.home().resolve()
        try:
            relative = path.relative_to(home)
        except ValueError:
            return str(path)
        return "~" if not relative.parts else f"~/{relative.as_posix()}"
    except OSError:
        return raw_path


def _display_timestamp(value: str) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%MZ")


def _normalize_cwd(raw_path: str | Path) -> str:
    try:
        return str(Path(raw_path).resolve())
    except OSError:
        return str(raw_path)


def print_sessions(store: SessionStore, current_session_id: str | None = None) -> None:
    sessions = store.list()
    if not sessions:
        print("No sessions")
        return

    headers = ("CUR", "ID", "NAME", "UPDATED", "CWD", "PARENT")
    rows: list[tuple[str, str, str, str, str, str]] = []
    for session in sessions:
        rows.append(
            (
                "*" if session.id == current_session_id else " ",
                _truncate(session.id, 12),
                _truncate(session.name or "(unnamed)", 24),
                _display_timestamp(session.updated_at),
                _truncate(_display_path(session.cwd), 36),
                _truncate(session.parent_session_id or "-", 12),
            )
        )

    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    print("Sessions")
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * widths[index] for index in range(len(headers))))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    if current_session_id:
        print("* current session")


def print_reload_summary(agent: Agent, message: str, warnings: list[str] | None = None) -> None:
    summary = agent.inspect_runtime()
    conflicts = summary["skills"]["conflicts"]
    print(message)
    print(f"model={summary['model']}")
    print(f"base_url={summary['base_url']}")
    print(f"tools={', '.join(summary['tools']) or '(none)'}")
    print(f"skills.available={', '.join(summary['skills']['available']) or '(none)'}")
    print(f"skills.history_only={', '.join(summary['skills']['history_only']) or '(none)'}")
    print(f"skills.pending={summary['skills']['pending'] or '(none)'}")
    print(f"skills.conflicts={len(conflicts)}")
    print(f"templates.available={', '.join(summary['templates']['available']) or '(none)'}")
    print(f"prompts.loaded={len(summary['prompts']['loaded'])}")
    print(f"skills.loaded={len(summary['skills']['loaded'])}")
    print(f"read.max_lines={summary['tool_defaults']['read_max_lines']}")
    print(f"bash.timeout_seconds={summary['tool_defaults']['bash_timeout_seconds']}")
    print(f"bash.max_output_bytes={summary['tool_defaults']['bash_max_output_bytes']}")
    for warning in warnings or []:
        print(f"warning={warning}")


def print_tools_summary(agent: Agent) -> None:
    summary = agent.inspect_runtime()
    print("Tools summary")
    print(f"tools={', '.join(summary['tools']) or '(none)'}")
    print(f"read.max_lines={summary['tool_defaults']['read_max_lines']}")
    print(f"bash.timeout_seconds={summary['tool_defaults']['bash_timeout_seconds']}")
    print(f"bash.max_output_bytes={summary['tool_defaults']['bash_max_output_bytes']}")


def print_skills_list(agent: Agent) -> None:
    skills = agent.available_skills()
    print("Skills")
    if "read" not in agent.tools and skills:
        print("/skill:<name> is unavailable because the read tool is disabled.")
        print("Enable the read tool to use discovered skills.")
        return
    if not skills:
        print("No skills available.")
        return

    for entry in skills:
        print(f"- {entry.name}: {entry.summary}")
        if entry.when_to_use:
            print(f"  Use when: {entry.when_to_use}")
        if entry.source_label:
            print(f"  Source: {entry.source_label}")
        if entry.shadowed_sources:
            print(f"  Shadowed definitions: {len(entry.shadowed_sources)}")


def print_templates_list(agent: Agent) -> None:
    templates = agent.runtime.list_template_names()
    print("Templates")
    if not templates:
        print("No templates available.")
        return

    for name in templates:
        print(f"- {name}")


def print_runtime_config_summary(agent: Agent) -> None:
    summary = agent.inspect_runtime()
    print("Runtime config")
    print(f"model={summary['model']}")
    print(f"base_url={summary['base_url']}")
    print(f"tools={', '.join(summary['tools']) or '(none)'}")
    print(f"read.max_lines={summary['tool_defaults']['read_max_lines']}")
    print(f"bash.timeout_seconds={summary['tool_defaults']['bash_timeout_seconds']}")
    print(f"bash.max_output_bytes={summary['tool_defaults']['bash_max_output_bytes']}")


def print_resume_sessions(store: SessionStore, current_cwd: Path):
    normalized_cwd = _normalize_cwd(current_cwd)
    sessions = [session for session in store.list() if _normalize_cwd(session.cwd) == normalized_cwd]
    if not sessions:
        print("No sessions")
        return []

    print("Sessions")
    for index, session in enumerate(sessions, start=1):
        print(f"{index}. {session.name or '(unnamed)'}")
    return sessions


def build_runtime_summary(agent: Agent) -> dict[str, object]:
    return agent.inspect_runtime()


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
    print(f"tools={', '.join(summary['tools']) or '(none)'}")
    print(f"prompts.order={', '.join(prompt_summary['order']) or '(none)'}")
    print(f"prompts.available={', '.join(prompt_summary['available']) or '(none)'}")
    print(f"skills.available={', '.join(skill_summary['available']) or '(none)'}")
    print(f"skills.history_only={', '.join(skill_summary['history_only']) or '(none)'}")
    print(f"skills.pending={skill_summary['pending'] or '(none)'}")
    print(f"skills.conflicts={len(skill_summary['conflicts'])}")
    print(f"templates.available={', '.join(template_summary['available']) or '(none)'}")
    print(f"prompts.loaded={', '.join(prompt_summary['loaded']) or '(none)'}")
    print(f"skills.loaded={', '.join(skill_summary['loaded']) or '(none)'}")
    print(f"warnings.count={len(warnings)}")


def print_runtime_prompt(agent: Agent) -> None:
    inspection = agent.inspect_prompt()
    prompt_summary = build_runtime_prompt_summary(agent)
    fragments = prompt_summary["fragments"]
    prompt_fragments = agent.runtime.snapshot().prompt_fragments
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
    print("assembled_with_boundaries:")
    if inspection.fragments:
        total = len(inspection.fragments)
        for index, fragment in enumerate(inspection.fragments, start=1):
            print(f"----- fragment[{index}/{total}] BEGIN key={fragment.key} source={fragment.source} chars={fragment.text_length} -----")
            prompt_fragment = prompt_fragments.get(fragment.key)
            text = prompt_fragment.text.strip() if prompt_fragment is not None else agent.prompt_fragment_text(fragment.key)
            if text:
                print(text)
            else:
                print("(empty)")
            print(f"----- fragment[{index}/{total}] END -----")
    else:
        print("(empty)")


def read_cli_line(prompt: str = "astra> ") -> str:
    try:
        return input(prompt)
    except UnicodeDecodeError:
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is None:
            raise
        raw_line = buffer.readline()
        if not isinstance(raw_line, (bytes, bytearray)):
            raise
        if not raw_line:
            raise EOFError
        print(
            "Warning: stdin contains non-UTF-8 bytes; invalid bytes were replaced.",
            file=sys.stderr,
        )
        return bytes(raw_line).decode("utf-8", errors="replace").rstrip("\r\n")


def handle_extension_command(agent: Agent, line: str, run_streaming: Callable[[Callable[[], object]], object]) -> tuple[bool, bool]:
    if line.startswith("/skill:"):
        remainder = line[len("/skill:") :].strip()
        if not remainder:
            return False, False
        name, _, request_text = remainder.partition(" ")
        if not name:
            return False, False
        request_text = request_text.strip()
        if request_text:
            try:
                result = run_streaming(lambda: agent.run_skill(name, request_text, line))
            except ValueError as exc:
                print(str(exc))
                return True, False
            print()
            if result.error:
                print(result.error, file=sys.stderr)
            return True, True

        success, message = agent.arm_skill(name, line)
        print(message)
        return True, False

    if line.startswith("/template:"):
        remainder = line[len("/template:") :].strip()
        name, _, request_text = remainder.partition(" ")
        request_text = request_text.strip()
        if not name or not request_text:
            print("Usage: /template:<name> <request>")
            return True, False
        try:
            result = run_streaming(lambda: agent.run_template(name, request_text, line))
        except ValueError as exc:
            print(str(exc))
            return True, False
        print()
        if result.error:
            print(result.error, file=sys.stderr)
        return True, True

    result = run_streaming(lambda: agent.try_handle_extension_command(line))
    if result is None:
        return False, False
    if result.message:
        print(result.message)
    if result.run_result is not None:
        print()
        if result.run_result.error:
            print(result.run_result.error, file=sys.stderr)
    return True, result.persist_state

    return False, False


def main(
    argv: list[str] | None = None,
    *,
    agent_factory: AgentFactory | None = None,
    runtime_factory: RuntimeFactory | None = None,
    session_store_factory: SessionStoreFactory | None = None,
    config_manager_factory: ConfigManagerFactory | None = None,
) -> None:
    args = parse_args(argv or sys.argv[1:])
    runtime_builder = runtime_factory or CapabilityRuntime
    store_builder = session_store_factory or SessionStore
    config_builder = config_manager_factory or ConfigManager
    cwd = Path(args.cwd).resolve()
    store = store_builder()
    config_manager = config_builder()
    runtime_env: dict[str, str] = {}

    def load_env() -> dict[str, str]:
        try:
            return merged_env(cwd, env=os.environ)
        except DotenvError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            return dict(os.environ)

    def load_runtime() -> ResolvedRuntimeConfig:
        nonlocal config_manager, runtime_env
        runtime_env = load_env()
        try:
            raw_config = config_manager.load(cwd)
        except ConfigError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
            raw_config = RuntimeConfig()
        return resolve_runtime_config(raw_config, args.model, args.base_url, args.system_prompt, env=runtime_env)

    runtime_config = load_runtime()
    api_key = runtime_env.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required")

    capability_runtime = runtime_builder(cwd)
    agent_builder = agent_factory or (lambda cfg, runtime: Agent(cfg, runtime))
    agent = agent_builder(
        AgentConfig(
            model=runtime_config.model,
            api_key=api_key,
            base_url=runtime_config.base_url,
            cwd=cwd,
            system_prompt=runtime_config.system_prompt,
        ),
        capability_runtime,
    )

    session_state = CliSessionState(
        session=store.create(cwd=str(cwd), model=runtime_config.model, system_prompt=runtime_config.system_prompt),
        materialized=False,
    )

    def current_session_id() -> str | None:
        if not session_state.materialized:
            return None
        return session_state.session.id

    def print_help() -> None:
        for line in command_registry.help_lines():
            print(line)
        for line in agent.extension_command_usages():
            print(line)

    def _set_default_session_name(text: str) -> None:
        if session_state.materialized:
            return
        if (session_state.session.name or "").strip():
            return
        normalized = text.strip()
        if normalized:
            session_state.session.name = normalized

    def persist_agent_state(create_if_needed: bool = False) -> bool:
        if not session_state.materialized and not create_if_needed:
            return False
        if not session_state.materialized and not agent.messages:
            return False
        apply_agent_snapshot_to_session(session_state.session, agent.snapshot())
        store.save(session_state.session)
        session_state.materialized = True
        return True

    def restore_session(session: Session, active_runtime: ResolvedRuntimeConfig) -> bool:
        snapshot = session_to_agent_snapshot(session, active_runtime)
        agent.restore(snapshot)
        resumed_runtime = clone_resolved_runtime_config(active_runtime)
        resumed_runtime.model = snapshot.runtime.runtime_config.model
        resumed_runtime.base_url = snapshot.runtime.runtime_config.base_url
        resumed_runtime.system_prompt = snapshot.runtime.runtime_config.system_prompt
        result = agent.apply_runtime_config(resumed_runtime)
        if not result.success:
            print(f"Failed to restore session: {result.message}")
            return False
        session_state.session = session
        session_state.materialized = True
        return True

    startup_reload = agent.apply_runtime_config(runtime_config)
    if not startup_reload.success:
        print(f"Warning: {startup_reload.message}", file=sys.stderr)
    elif startup_reload.warnings:
        for warning in startup_reload.warnings:
            print(f"Warning: {warning}", file=sys.stderr)

    if args.session and not args.new_session:
        loaded_session = store.load(args.session)
        if not restore_session(loaded_session, runtime_config):
            raise SystemExit(1)

    command_registry = CommandRegistry()

    def handle_sigint(_signum: int, _frame: object) -> None:
        if agent.is_streaming:
            agent.abort()
            print("\n[aborted]", flush=True)
            return
        raise KeyboardInterrupt

    def run_streaming(callable_: Callable[[], object]) -> object:
        unsubscribe = agent.subscribe(stream_event)
        try:
            return callable_()
        finally:
            unsubscribe()

    def reload_runtime_from_config() -> None:
        if agent.is_streaming:
            print("Cannot reload while a response is streaming.")
            return
        resolved_runtime = load_runtime()
        result = agent.apply_runtime_config(resolved_runtime)
        if result.success:
            print_reload_summary(agent, result.message, result.warnings)
            if session_state.materialized:
                persist_agent_state()
        else:
            print(f"Reload failed: {result.message}")

    def reload_code_modules() -> None:
        nonlocal agent, config_manager
        if agent.is_streaming:
            print("Cannot reload while a response is streaming.")
            return
        snapshot_dict = agent_snapshot_to_dict(agent.snapshot())
        try:
            config_module = importlib.reload(importlib.import_module("astra.config"))
            importlib.reload(importlib.import_module("astra.tools"))
            importlib.reload(importlib.import_module("astra.provider"))
            importlib.reload(importlib.import_module("astra.runtime.builtin_capabilities"))
            runtime_module = importlib.reload(importlib.import_module("astra.runtime.runtime"))
            session_module = importlib.reload(importlib.import_module("astra.session"))
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
                ),
            ),
        )
        restored_snapshot = session_module.agent_snapshot_from_dict(snapshot_dict, runtime_after_code_reload)
        new_runtime = runtime_module.CapabilityRuntime(Path(restored_snapshot.runtime.cwd or cwd))
        agent = agent_module.Agent(
            agent_module.AgentConfig(
                model=restored_snapshot.runtime.runtime_config.model,
                api_key=api_key,
                base_url=restored_snapshot.runtime.runtime_config.base_url,
                cwd=Path(restored_snapshot.runtime.cwd or cwd),
                system_prompt=restored_snapshot.runtime.runtime_config.system_prompt,
            ),
            capability_runtime=new_runtime,
            initial_snapshot=restored_snapshot,
        )
        print("Code modules reloaded.")
        reload_runtime_from_config()

    def run_user_prompt(text: str):
        _set_default_session_name(text)
        result = run_streaming(lambda: agent.prompt(text, raw_input=text))
        print()
        if getattr(result, "error", None):
            print(result.error, file=sys.stderr)
        persist_agent_state(create_if_needed=True)
        return result

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
            agent.set_model(args.model)
            if session_state.materialized:
                persist_agent_state()
            print(f"Model set to {agent.config.model}")
            return True

        def base_url_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if not remainder:
                print(agent.config.base_url)
                return True
            args.base_url = remainder.strip()
            agent.set_base_url(args.base_url)
            if session_state.materialized:
                persist_agent_state()
            print(f"Base URL set to {agent.config.base_url}")
            return True

        def tools_command(_line: str) -> bool:
            print_tools_summary(agent)
            return True

        def skills_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if remainder.strip():
                return False
            print_skills_list(agent)
            return True

        def templates_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if remainder.strip():
                return False
            print_templates_list(agent)
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
            print_sessions(store, current_session_id=current_session_id())
            return True

        def switch_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            session_id = remainder.strip()
            if not session_id:
                return False
            loaded_session = store.load(session_id)
            active_runtime = clone_resolved_runtime_config(agent.runtime_config)
            if not restore_session(loaded_session, active_runtime):
                return True
            print(f"Switched to {session_state.session.id}")
            return True

        def resume_command(_line: str) -> bool:
            sessions = print_resume_sessions(store, Path(agent.runtime_state.cwd))
            if not sessions:
                return True

            selection = read_cli_line("resume> ").strip()
            try:
                session_index = int(selection)
            except ValueError:
                print("Invalid session number.")
                return True

            if session_index < 1 or session_index > len(sessions):
                print("Invalid session number.")
                return True

            selected = sessions[session_index - 1]
            loaded_session = store.load(selected.id)
            active_runtime = clone_resolved_runtime_config(agent.runtime_config)
            if not restore_session(loaded_session, active_runtime):
                return True
            resumed_name = session_state.session.name or "(unnamed)"
            print(f"Resumed {resumed_name} ({session_state.session.id})")
            print_runtime_config_summary(agent)
            return True

        def fork_command(line: str) -> bool:
            if not session_state.materialized:
                print("No saved session to fork.")
                return True
            persist_agent_state()
            _command_name, _, remainder = line.partition(" ")
            name = remainder.strip() or None
            forked = store.fork(session_state.session.id, name=name)
            session_state.session = forked
            session_state.materialized = True
            print(f"Forked to {forked.id}")
            return True

        def rename_command(line: str) -> bool:
            if not session_state.materialized:
                print("No saved session to rename.")
                return True
            _command_name, _, remainder = line.partition(" ")
            name = remainder.strip()
            if not name:
                return False
            session_state.session.name = name
            persist_agent_state()
            print(f"Renamed to {session_state.session.name}")
            return True

        def save_command(_line: str) -> bool:
            if not session_state.materialized:
                print("No session to save.")
                return True
            persist_agent_state()
            print(f"Saved {session_state.session.id}")
            return True

        def exit_command(_line: str) -> bool:
            raise EOFError

        command_registry.register(CommandSpec(name="/help", usage="/help", summary="Show help", handler=help_command))
        command_registry.register(
            CommandSpec(name="/reload", usage="/reload | /reload code", summary="Reload runtime or code", handler=reload_command)
        )
        command_registry.register(CommandSpec(name="/model", usage="/model [name]", summary="Show or set model", handler=model_command))
        command_registry.register(
            CommandSpec(name="/base-url", usage="/base-url [url]", summary="Show or set base URL", handler=base_url_command)
        )
        command_registry.register(
            CommandSpec(name="/tools", usage="/tools", summary="Show enabled tools and defaults", handler=tools_command)
        )
        command_registry.register(
            CommandSpec(name="/skills", usage="/skills", summary="List available skills", handler=skills_command)
        )
        command_registry.register(
            CommandSpec(name="/templates", usage="/templates", summary="List available templates", handler=templates_command)
        )
        command_registry.register(
            CommandSpec(name="/runtime", usage="/runtime | /runtime warnings | /runtime json | /runtime prompt | /runtime json prompt", summary="Show capability runtime state", handler=runtime_command)
        )
        command_registry.register(
            CommandSpec(name="/sessions", usage="/sessions", summary="List saved sessions", handler=sessions_command)
        )
        command_registry.register(
            CommandSpec(name="/resume", usage="/resume", summary="Resume a saved session by number", handler=resume_command)
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

    register_commands()
    signal.signal(signal.SIGINT, handle_sigint)

    if args.prompt:
        text = " ".join(args.prompt).strip()
        if text:
            result = run_user_prompt(text)
            if getattr(result, "error", None):
                raise SystemExit(1)
        return

    print(f"Session {current_session_id() or '(new)'}")
    print_help()
    while True:
        try:
            line = read_cli_line("astra> ").strip()
            if not line:
                continue
            if line.startswith("/") and command_registry.dispatch(line):
                continue
            handled_extension, create_session = handle_extension_command(agent, line, run_streaming)
            if handled_extension:
                if create_session:
                    persist_agent_state(create_if_needed=True)
                elif session_state.materialized:
                    persist_agent_state()
                continue
            run_user_prompt(line)
        except EOFError:
            print()
            if session_state.materialized:
                persist_agent_state()
            return
        except KeyboardInterrupt:
            print()
