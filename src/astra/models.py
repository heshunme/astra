from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TypeAlias


MessageRole = Literal["user", "assistant", "tool_result"]
ProviderEventType = Literal["text_delta", "tool_call_delta", "usage", "done"]
ToolHandler: TypeAlias = Callable[[dict[str, Any], "ToolContext"], "ToolResult"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class Message:
    role: MessageRole
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderEvent:
    type: ProviderEventType
    delta: str = ""
    index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str = ""
    usage: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolContext:
    cwd: Path
    workspace_root: Path
    timeout_seconds: int
    max_output_bytes: int
    read_max_lines: int


@dataclass(slots=True)
class ToolResult:
    text: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: ToolHandler


@dataclass(slots=True)
class Session:
    id: str
    name: str | None
    cwd: str
    created_at: str
    updated_at: str
    model: str
    system_prompt: str
    messages: list[Message]
    parent_session_id: str | None = None


@dataclass(slots=True)
class SessionSummary:
    id: str
    name: str | None
    cwd: str
    updated_at: str
    parent_session_id: str | None


@dataclass(slots=True)
class AgentRunResult:
    assistant_messages: list[Message]
    tool_results: list[Message]
    usage: dict[str, Any] | None = None
    error: str | None = None
