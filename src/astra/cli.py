from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .app import (
    AgentFactory,
    AstraApp,
    AstraAppOptions,
    ConfigManagerFactory,
    RuntimeFactory,
    SessionStoreFactory,
)
from .cli_commands import CommandRegistry, CommandSpec
from .models import AgentEvent, AgentRunResult, SessionSummary


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


def print_sessions(app: AstraApp) -> None:
    sessions = app.list_sessions()
    current_session_id = app.current_session_id()
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


def build_runtime_summary(app: AstraApp) -> dict[str, object]:
    return app.get_runtime_summary()


def build_runtime_prompt_summary(app: AstraApp) -> dict[str, object]:
    summary = app.get_runtime_prompt_summary()
    return {
        "assembled": summary.assembled,
        "char_length": summary.char_length,
        "fragment_count": summary.fragment_count,
        "fragments": [
            {
                "key": fragment.key,
                "source": fragment.source,
                "text_length": fragment.text_length,
            }
            for fragment in summary.fragments
        ],
    }


def print_reload_summary(app: AstraApp, message: str, warnings: list[str] | None = None) -> None:
    summary = build_runtime_summary(app)
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


def print_tools_summary(app: AstraApp) -> None:
    summary = app.get_tools_summary()
    defaults = summary["tool_defaults"]
    print("Tools summary")
    print(f"tools={', '.join(summary['tools']) or '(none)'}")
    print(f"read.max_lines={defaults['read_max_lines']}")
    print(f"bash.timeout_seconds={defaults['bash_timeout_seconds']}")
    print(f"bash.max_output_bytes={defaults['bash_max_output_bytes']}")


def print_skills_list(app: AstraApp) -> None:
    summary = build_runtime_summary(app)
    skills = app.get_skills()
    print("Skills")
    if "read" not in summary["tools"] and skills:
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


def print_templates_list(app: AstraApp) -> None:
    templates = app.get_templates()
    print("Templates")
    if not templates:
        print("No templates available.")
        return

    for name in templates:
        print(f"- {name}")


def print_runtime_config_summary(app: AstraApp) -> None:
    summary = build_runtime_summary(app)
    print("Runtime config")
    print(f"model={summary['model']}")
    print(f"base_url={summary['base_url']}")
    print(f"tools={', '.join(summary['tools']) or '(none)'}")
    print(f"read.max_lines={summary['tool_defaults']['read_max_lines']}")
    print(f"bash.timeout_seconds={summary['tool_defaults']['bash_timeout_seconds']}")
    print(f"bash.max_output_bytes={summary['tool_defaults']['bash_max_output_bytes']}")


def print_runtime_summary(app: AstraApp, show_warnings_only: bool = False) -> None:
    summary = build_runtime_summary(app)
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


def print_runtime_prompt(app: AstraApp) -> None:
    inspection = app.get_runtime_prompt_summary()
    print("Runtime prompt")
    print(f"fragments={inspection.fragment_count}")
    print(f"char_length={inspection.char_length}")
    if inspection.fragments:
        for index, fragment in enumerate(inspection.fragments, start=1):
            print(f"fragment[{index}]={fragment.key} source={fragment.source} chars={fragment.text_length}")
    else:
        print("fragments=(none)")
    print("assembled_with_boundaries:")
    if inspection.fragments:
        total = len(inspection.fragments)
        for index, fragment in enumerate(inspection.fragments, start=1):
            print(
                f"----- fragment[{index}/{total}] BEGIN key={fragment.key} source={fragment.source} chars={fragment.text_length} -----"
            )
            text = app.prompt_fragment_text(fragment.key)
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


def print_help(app: AstraApp) -> None:
    for entry in app.help_entries():
        print(entry.usage)


def _print_run_result(result: AgentRunResult) -> None:
    print()
    if result.error:
        print(result.error, file=sys.stderr)


def handle_extension_command(app: AstraApp, line: str, run_streaming: Callable[[Callable[[], Any]], Any]) -> bool:
    if line.startswith("/skill:"):
        remainder = line[len("/skill:") :].strip()
        if not remainder:
            return False
        name, _, request_text = remainder.partition(" ")
        if not name:
            return False
        request_text = request_text.strip()
        if request_text:
            try:
                result = run_streaming(lambda: app.run_skill(name, request_text))
            except ValueError as exc:
                print(str(exc))
                return True
            _print_run_result(result)
            return True

        result = app.arm_skill(name)
        print(result.message)
        return True

    if line.startswith("/template:"):
        remainder = line[len("/template:") :].strip()
        name, _, request_text = remainder.partition(" ")
        request_text = request_text.strip()
        if not name or not request_text:
            print("Usage: /template:<name> <request>")
            return True
        try:
            result = run_streaming(lambda: app.run_template(name, request_text))
        except ValueError as exc:
            print(str(exc))
            return True
        _print_run_result(result)
        return True

    return False


def run_user_prompt(app: AstraApp, run_streaming: Callable[[Callable[[], Any]], Any], text: str) -> AgentRunResult:
    result = run_streaming(lambda: app.submit_prompt(text))
    _print_run_result(result)
    return result


def print_resume_sessions(app: AstraApp) -> list[SessionSummary]:
    candidates = app.list_resume_candidates()
    if not candidates:
        print("No sessions")
        return []

    print("Sessions")
    for index, session in enumerate(candidates, start=1):
        print(f"{index}. {session.name or '(unnamed)'}")
    return [
        SessionSummary(
            id=session.id,
            name=session.name,
            cwd=session.cwd,
            updated_at=session.updated_at,
            parent_session_id=session.parent_session_id,
        )
        for session in candidates
    ]


def main(
    argv: list[str] | None = None,
    *,
    agent_factory: AgentFactory | None = None,
    runtime_factory: RuntimeFactory | None = None,
    session_store_factory: SessionStoreFactory | None = None,
    config_manager_factory: ConfigManagerFactory | None = None,
) -> None:
    args = parse_args(argv or sys.argv[1:])
    app = AstraApp(
        AstraAppOptions(
            cwd=args.cwd,
            session_id=args.session,
            new_session=args.new_session,
            model_override=args.model,
            base_url_override=args.base_url,
            system_prompt_override=args.system_prompt,
        ),
        agent_factory=agent_factory,
        runtime_factory=runtime_factory,
        session_store_factory=session_store_factory,
        config_manager_factory=config_manager_factory,
    )
    startup_result = app.startup()
    for warning in startup_result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    command_registry = CommandRegistry()

    def handle_sigint(_signum: int, _frame: object) -> None:
        if app.is_streaming:
            app.abort()
            print("\n[aborted]", flush=True)
            return
        raise KeyboardInterrupt

    def run_streaming(callable_: Callable[[], Any]) -> Any:
        unsubscribe = app.subscribe(stream_event)
        try:
            return callable_()
        finally:
            unsubscribe()

    def register_commands() -> None:
        def help_command(_line: str) -> bool:
            print_help(app)
            return True

        def reload_command(line: str) -> bool:
            command_name, _, remainder = line.partition(" ")
            if command_name != "/reload":
                return False
            if remainder == "code":
                result = app.reload_code()
                print(result.message)
                if not result.error:
                    print_reload_summary(app, "Reloaded runtime configuration.", result.warnings)
                else:
                    print(result.error)
                return True
            if remainder:
                return False
            result = app.reload_runtime()
            if result.success:
                print_reload_summary(app, result.message, result.warnings)
            else:
                print(f"Reload failed: {result.message}")
            return True

        def model_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if not remainder:
                print(app.get_model())
                return True
            result = app.set_model(remainder.strip())
            print(result.message)
            return True

        def base_url_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if not remainder:
                print(app.get_base_url())
                return True
            result = app.set_base_url(remainder.strip())
            print(result.message)
            return True

        def tools_command(_line: str) -> bool:
            print_tools_summary(app)
            return True

        def skills_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if remainder.strip():
                return False
            print_skills_list(app)
            return True

        def templates_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            if remainder.strip():
                return False
            print_templates_list(app)
            return True

        def runtime_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            normalized_remainder = remainder.strip()
            if normalized_remainder == "json":
                print(json.dumps(build_runtime_summary(app), ensure_ascii=False, indent=2))
                return True
            if normalized_remainder == "json prompt":
                payload = build_runtime_summary(app)
                payload["prompt"] = build_runtime_prompt_summary(app)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return True
            if normalized_remainder == "warnings":
                print_runtime_summary(app, show_warnings_only=True)
                return True
            if normalized_remainder == "prompt":
                print_runtime_prompt(app)
                return True
            if normalized_remainder:
                return False
            print_runtime_summary(app)
            return True

        def sessions_command(_line: str) -> bool:
            print_sessions(app)
            return True

        def switch_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            session_id = remainder.strip()
            if not session_id:
                return False
            result = app.switch_session(session_id)
            if result.error:
                print(result.error)
            else:
                print(result.message)
            return True

        def resume_command(_line: str) -> bool:
            sessions = print_resume_sessions(app)
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
            result = app.resume_session(selected.id)
            if result.error:
                print(result.error)
                return True
            print(result.message)
            print_runtime_config_summary(app)
            return True

        def fork_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            name = remainder.strip() or None
            result = app.fork_session(name=name)
            print(result.message)
            return True

        def rename_command(line: str) -> bool:
            _command_name, _, remainder = line.partition(" ")
            name = remainder.strip()
            if not name:
                return False
            result = app.rename_session(name)
            print(result.message)
            return True

        def save_command(_line: str) -> bool:
            result = app.save_session()
            print(result.message)
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
            CommandSpec(
                name="/runtime",
                usage="/runtime | /runtime warnings | /runtime json | /runtime prompt | /runtime json prompt",
                summary="Show capability runtime state",
                handler=runtime_command,
            )
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
            result = run_user_prompt(app, run_streaming, text)
            if result.error:
                raise SystemExit(1)
        return

    print(f"Session {app.current_session_id() or '(new)'}")
    print_help(app)
    while True:
        try:
            line = read_cli_line("astra> ").strip()
            if not line:
                continue
            if line.startswith("/") and command_registry.dispatch(line):
                continue
            if handle_extension_command(app, line, run_streaming):
                continue
            run_user_prompt(app, run_streaming, line)
        except EOFError:
            print()
            if app.has_materialized_session():
                app.save_session()
            return
        except KeyboardInterrupt:
            print()
