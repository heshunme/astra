from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TypeAlias

from .config import ResolvedRuntimeConfig, clone_resolved_runtime_config


MessageRole = Literal["user", "assistant", "tool_result"]
ProviderEventType = Literal["text_delta", "tool_call_delta", "usage", "done"]
AgentEventType = Literal[
    "agent_start",
    "turn_start",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "turn_end",
    "agent_end",
    "error",
    "state_changed",
]
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
class SkillCatalogEntry:
    name: str
    summary: str
    when_to_use: str = ""
    files: list[str] = field(default_factory=list)
    source: str = ""
    source_label: str = ""
    shadowed_sources: list[str] = field(default_factory=list)
    history_only: bool = False


@dataclass(slots=True)
class PendingSkillTriggerState:
    name: str
    raw_command: str


@dataclass(slots=True)
class AgentConversationState:
    messages: list[Message] = field(default_factory=list)


@dataclass(slots=True)
class AgentRuntimeState:
    cwd: str
    runtime_config: ResolvedRuntimeConfig
    skill_catalog_snapshot: list[SkillCatalogEntry] = field(default_factory=list)
    pending_skill_trigger: PendingSkillTriggerState | None = None


@dataclass(slots=True)
class AgentSnapshot:
    conversation: AgentConversationState
    runtime: AgentRuntimeState


@dataclass(slots=True)
class AgentEvent:
    type: AgentEventType
    payload: dict[str, Any] = field(default_factory=dict)


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
    skill_catalog_snapshot: list[SkillCatalogEntry] = field(default_factory=list)
    parent_session_id: str | None = None
    agent_snapshot: AgentSnapshot | None = None


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


@dataclass(slots=True)
class CoreCommandResult:
    message: str | None = None
    run_result: AgentRunResult | None = None
    error: str | None = None
    persist_state: bool = False


def clone_tool_call(tool_call: ToolCall) -> ToolCall:
    return ToolCall(id=tool_call.id, name=tool_call.name, arguments=tool_call.arguments)


def clone_message(message: Message) -> Message:
    return Message(
        role=message.role,
        content=message.content,
        tool_call_id=message.tool_call_id,
        tool_name=message.tool_name,
        tool_calls=[clone_tool_call(tool_call) for tool_call in message.tool_calls],
        created_at=message.created_at,
        metadata=dict(message.metadata),
    )


def clone_messages(messages: list[Message]) -> list[Message]:
    return [clone_message(message) for message in messages]


def clone_skill_catalog_entry(entry: SkillCatalogEntry) -> SkillCatalogEntry:
    return SkillCatalogEntry(
        name=entry.name,
        summary=entry.summary,
        when_to_use=entry.when_to_use,
        files=list(entry.files),
        source=entry.source,
        source_label=entry.source_label,
        shadowed_sources=list(entry.shadowed_sources),
        history_only=entry.history_only,
    )


def clone_skill_catalog(entries: list[SkillCatalogEntry]) -> list[SkillCatalogEntry]:
    return [clone_skill_catalog_entry(entry) for entry in entries]


def clone_pending_skill_trigger(
    trigger: PendingSkillTriggerState | None,
) -> PendingSkillTriggerState | None:
    if trigger is None:
        return None
    return PendingSkillTriggerState(name=trigger.name, raw_command=trigger.raw_command)


def clone_agent_snapshot(snapshot: AgentSnapshot) -> AgentSnapshot:
    return AgentSnapshot(
        conversation=AgentConversationState(messages=clone_messages(snapshot.conversation.messages)),
        runtime=AgentRuntimeState(
            cwd=snapshot.runtime.cwd,
            runtime_config=clone_resolved_runtime_config(snapshot.runtime.runtime_config),
            skill_catalog_snapshot=clone_skill_catalog(snapshot.runtime.skill_catalog_snapshot),
            pending_skill_trigger=clone_pending_skill_trigger(snapshot.runtime.pending_skill_trigger),
        ),
    )
