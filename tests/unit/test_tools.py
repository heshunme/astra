from __future__ import annotations

import json
from pathlib import Path

import pytest

from astra.config import ToolRuntimeConfig
from astra.models import ToolContext
from astra.tools import (
    build_all_tools,
    build_default_tools,
    edit_tool,
    execute_tool,
    read_tool,
    resolve_workspace_path,
    write_tool,
)


pytestmark = pytest.mark.unit


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        cwd=workspace,
        workspace_root=workspace,
        timeout_seconds=1,
        max_output_bytes=1024,
        read_max_lines=2,
    )


def test_resolve_workspace_path_blocks_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="Path escapes workspace"):
        resolve_workspace_path(workspace, "../outside.txt")


def test_read_tool_honors_read_max_lines(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "sample.txt"
    file_path.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")

    result = read_tool({"path": "sample.txt"}, _ctx(workspace))

    assert not result.is_error
    assert "1: line1" in result.text
    assert "2: line2" in result.text
    assert "line3" not in result.text
    assert "[Truncated to 2 lines]" in result.text


def test_read_tool_allows_registered_skill_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_file = tmp_path / "global-skills" / "review" / "checklist.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("line1\nline2\n", encoding="utf-8")

    ctx = _ctx(workspace)
    ctx.skill_file_aliases["skill://review/checklist.md"] = skill_file

    result = read_tool({"path": "skill://review/checklist.md"}, ctx)

    assert not result.is_error
    assert "1: line1" in result.text


def test_read_tool_rejects_unknown_skill_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_tool({"path": "skill://review/checklist.md"}, _ctx(workspace))

    assert result.is_error
    assert "Unknown skill file: skill://review/checklist.md" in result.text


def test_write_tool_rejects_skill_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="Skill files are read-only"):
        write_tool({"path": "skill://review/checklist.md", "content": "x"}, _ctx(workspace))


def test_edit_tool_rejects_skill_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="Skill files are read-only"):
        edit_tool(
            {"path": "skill://review/checklist.md", "old_text": "a", "new_text": "b"},
            _ctx(workspace),
        )


def test_execute_tool_returns_timeout_for_long_bash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bash_tool = build_all_tools()["bash"]

    result = execute_tool(
        bash_tool,
        json.dumps({"command": "sleep 2", "timeout_seconds": 1}),
        _ctx(workspace),
    )

    assert result.is_error
    assert "Command timed out" in result.text


def test_execute_tool_includes_nonzero_exit_code(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bash_tool = build_all_tools()["bash"]

    result = execute_tool(
        bash_tool,
        json.dumps({"command": "echo boom 1>&2; exit 5"}),
        _ctx(workspace),
    )

    assert result.is_error
    assert "boom" in result.text
    assert "Command exited with code 5" in result.text


def test_build_default_tools_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError, match="Unknown tools in config"):
        build_default_tools(ToolRuntimeConfig(enabled_tools=["read", "nope"]))
