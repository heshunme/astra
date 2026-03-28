from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .config import (
    DEFAULT_BASH_MAX_OUTPUT_BYTES,
    DEFAULT_BASH_TIMEOUT_SECONDS,
    DEFAULT_ENABLED_TOOLS,
    DEFAULT_READ_MAX_LINES,
    ToolRuntimeConfig,
)
from .models import ToolContext, ToolResult, ToolSpec


DEFAULT_TIMEOUT_SECONDS = DEFAULT_BASH_TIMEOUT_SECONDS
DEFAULT_MAX_OUTPUT_BYTES = DEFAULT_BASH_MAX_OUTPUT_BYTES
DEFAULT_MAX_READ_LINES = DEFAULT_READ_MAX_LINES
SKIP_DIRS = {".git", "node_modules", "dist", "build", "coverage", "__pycache__", ".venv"}
BUILTIN_TOOLS_ORDER = tuple(DEFAULT_ENABLED_TOOLS)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_workspace_path(workspace_root: Path, raw_path: str) -> Path:
    if raw_path.startswith("skill://"):
        raise ValueError(f"Skill files are read-only and must be accessed via the read tool: {raw_path}")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve()
    root = workspace_root.resolve()
    if not _is_relative_to(resolved, root):
        raise ValueError(f"Path escapes workspace: {raw_path}")
    return resolved


def resolve_readable_path(ctx: ToolContext, raw_path: str) -> Path:
    if raw_path.startswith("skill://"):
        path = ctx.skill_file_aliases.get(raw_path)
        if path is None:
            raise ValueError(f"Unknown skill file: {raw_path}")
        return path
    return resolve_workspace_path(ctx.workspace_root, raw_path)


def truncate_tail(text: str, max_output_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_output_bytes:
        return text, False
    tail = encoded[-max_output_bytes:]
    return tail.decode("utf-8", errors="ignore"), True


def format_tool_result(result: ToolResult) -> str:
    prefix = "ERROR\n" if result.is_error else "OK\n"
    return prefix + result.text


def read_tool(args: dict, ctx: ToolContext) -> ToolResult:
    try:
        path = resolve_readable_path(ctx, args["path"])
    except ValueError as exc:
        return ToolResult(text=str(exc), is_error=True)
    if not path.exists():
        return ToolResult(text=f"File not found: {path}", is_error=True)
    if not path.is_file():
        return ToolResult(text=f"Not a file: {path}", is_error=True)
    start_line = max(int(args.get("start_line", 1)), 1)
    end_line = args.get("end_line")
    max_lines = max(int(args.get("max_lines", ctx.read_max_lines)), 1)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return ToolResult(text=f"Binary or non-UTF8 file: {path}", is_error=True)
    sliced = lines[start_line - 1 : end_line if end_line else None]
    if len(sliced) > max_lines:
        sliced = sliced[:max_lines]
        truncated = True
    else:
        truncated = False
    body = "\n".join(f"{start_line + index}: {line}" for index, line in enumerate(sliced))
    if truncated:
        body += f"\n\n[Truncated to {max_lines} lines]"
    return ToolResult(text=body or "(empty file)")


def write_tool(args: dict, ctx: ToolContext) -> ToolResult:
    path = resolve_workspace_path(ctx.workspace_root, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    return ToolResult(text=f"Wrote {path}")


def _match_line_numbers(text: str, needle: str) -> list[int]:
    line_numbers: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index == -1:
            return line_numbers
        line_numbers.append(text.count("\n", 0, index) + 1)
        start = index + len(needle)


def edit_tool(args: dict, ctx: ToolContext) -> ToolResult:
    path = resolve_workspace_path(ctx.workspace_root, args["path"])
    if not path.exists() or not path.is_file():
        return ToolResult(text=f"File not found: {path}", is_error=True)
    original = path.read_text(encoding="utf-8")
    old_text = args["old_text"]
    new_text = args["new_text"]
    replace_all = bool(args.get("replace_all", False))
    if old_text == "":
        return ToolResult(text="old_text must not be empty", is_error=True)
    if old_text not in original:
        return ToolResult(text="old_text not found", is_error=True)
    count = original.count(old_text)
    if count > 1 and not replace_all:
        line_numbers = ", ".join(str(line_number) for line_number in _match_line_numbers(original, old_text))
        return ToolResult(
            text=(
                f"Ambiguous edit: found {count} matches for old_text in file `{path.name}`.\n"
                "No changes were made.\n\n"
                "Refine old_text to a unique match, or set replace_all=true to replace all occurrences.\n"
                f"Match line numbers: {line_numbers}"
            ),
            is_error=True,
        )
    if replace_all:
        updated = original.replace(old_text, new_text)
    else:
        updated = original.replace(old_text, new_text, 1)
        count = 1
    path.write_text(updated, encoding="utf-8")
    suffix = "s" if count != 1 else ""
    return ToolResult(text=f"Updated {path} ({count} replacement{suffix})")


def ls_tool(args: dict, ctx: ToolContext) -> ToolResult:
    path = resolve_workspace_path(ctx.workspace_root, args.get("path", "."))
    if not path.exists() or not path.is_dir():
        return ToolResult(text=f"Directory not found: {path}", is_error=True)
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        kind = "dir" if child.is_dir() else "file"
        entries.append(f"{kind}\t{child.relative_to(ctx.workspace_root)}")
    return ToolResult(text="\n".join(entries) or "(empty directory)")


def _iter_files(base: Path):
    for path in base.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path


def find_tool(args: dict, ctx: ToolContext) -> ToolResult:
    base = resolve_workspace_path(ctx.workspace_root, args.get("path", "."))
    if not base.exists() or not base.is_dir():
        return ToolResult(text=f"Directory not found: {base}", is_error=True)
    pattern = args["pattern"]
    matches = []
    for path in _iter_files(base):
        if fnmatch.fnmatch(path.name, pattern) or pattern.lower() in path.name.lower():
            matches.append(str(path.relative_to(ctx.workspace_root)))
            if len(matches) >= 200:
                break
    return ToolResult(text="\n".join(matches) or "No matches found")


def grep_tool(args: dict, ctx: ToolContext) -> ToolResult:
    base = resolve_workspace_path(ctx.workspace_root, args.get("path", "."))
    if not base.exists() or not base.is_dir():
        return ToolResult(text=f"Directory not found: {base}", is_error=True)
    flags = 0 if args.get("case_sensitive") else re.IGNORECASE
    try:
        pattern = re.compile(args["pattern"], flags)
    except re.error as exc:
        return ToolResult(text=f"Invalid regex: {exc}", is_error=True)
    matches: list[str] = []
    for path in _iter_files(base):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append(f"{path.relative_to(ctx.workspace_root)}:{line_number}: {line}")
                if len(matches) >= 200:
                    return ToolResult(text="\n".join(matches))
    return ToolResult(text="\n".join(matches) or "No matches found")


def bash_tool(args: dict, ctx: ToolContext) -> ToolResult:
    command = args["command"]
    timeout_seconds = int(args.get("timeout_seconds", ctx.timeout_seconds))
    completed = subprocess.run(
        command,
        cwd=ctx.cwd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    output = completed.stdout
    if completed.stderr:
        if output:
            output += "\n"
        output += completed.stderr
    full_output_path = None
    truncated_output, truncated = truncate_tail(output or "", ctx.max_output_bytes)
    if truncated:
        temp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".log", prefix="astra-bash-")
        try:
            temp.write(output)
            full_output_path = temp.name
        finally:
            temp.close()
    text = truncated_output or "(no output)"
    if full_output_path:
        text += f"\n\n[Truncated output. Full output: {full_output_path}]"
    if completed.returncode != 0:
        text += f"\n\nCommand exited with code {completed.returncode}"
        return ToolResult(text=text, is_error=True)
    return ToolResult(text=text)


def execute_tool(tool: ToolSpec, args_json: str, ctx: ToolContext) -> ToolResult:
    try:
        args = json.loads(args_json or "{}")
    except json.JSONDecodeError as exc:
        return ToolResult(text=f"Invalid tool arguments: {exc}", is_error=True)
    try:
        return tool.handler(args, ctx)
    except subprocess.TimeoutExpired:
        return ToolResult(text="Command timed out", is_error=True)
    except Exception as exc:
        return ToolResult(text=f"Tool failed: {exc}", is_error=True)


def build_all_tools() -> dict[str, ToolSpec]:
    return {
        "read": ToolSpec(
            name="read",
            description="Read a UTF-8 text file from the workspace.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=read_tool,
        ),
        "write": ToolSpec(
            name="write",
            description="Write a UTF-8 text file in the workspace, creating parent directories if needed.",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=write_tool,
        ),
        "edit": ToolSpec(
            name="edit",
            description="Replace text in a file using exact string matching.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            handler=edit_tool,
        ),
        "ls": ToolSpec(
            name="ls",
            description="List files and directories in the workspace.",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            handler=ls_tool,
        ),
        "find": ToolSpec(
            name="find",
            description="Find files by wildcard or partial filename match.",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=find_tool,
        ),
        "grep": ToolSpec(
            name="grep",
            description="Search UTF-8 text files in the workspace with a regular expression.",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "case_sensitive": {"type": "boolean"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=grep_tool,
        ),
        "bash": ToolSpec(
            name="bash",
            description="Run a shell command in the current working directory.",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=bash_tool,
        ),
    }


def build_default_tools(config: ToolRuntimeConfig | None = None) -> dict[str, ToolSpec]:
    effective_config = config or ToolRuntimeConfig()
    all_tools = build_all_tools()
    unknown_tools = [name for name in effective_config.enabled_tools if name not in all_tools]
    if unknown_tools:
        unknown = ", ".join(sorted(unknown_tools))
        raise ValueError(f"Unknown tools in config: {unknown}")
    return {name: all_tools[name] for name in effective_config.enabled_tools}
