from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import (
    DEFAULT_BASE_URL,
    CapabilitiesConfig,
    PromptCapabilityConfig,
    PromptRuntimeConfig,
    ResolvedRuntimeConfig,
    SkillCapabilityConfig,
    ToolRuntimeConfig,
    clone_resolved_runtime_config,
)
from .models import (
    AgentConversationState,
    AgentRuntimeState,
    AgentSnapshot,
    Message,
    PendingSkillTriggerState,
    Session,
    SessionSummary,
    SkillCatalogEntry,
    ToolCall,
    clone_agent_snapshot,
    clone_messages,
    clone_skill_catalog,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _runtime_config_to_dict(config: ResolvedRuntimeConfig) -> dict:
    return {
        "model": config.model,
        "base_url": config.base_url,
        "system_prompt": config.system_prompt,
        "tools": {
            "enabled_tools": list(config.tools.enabled_tools),
            "read_max_lines": config.tools.read_max_lines,
            "bash_timeout_seconds": config.tools.bash_timeout_seconds,
            "bash_max_output_bytes": config.tools.bash_max_output_bytes,
        },
        "prompts": {
            "order": list(config.prompts.order),
        },
        "capabilities": {
            "prompts": {"paths": list(config.capabilities.prompts.paths)},
            "skills": {"paths": list(config.capabilities.skills.paths)},
        },
    }


def _runtime_config_from_dict(data: dict, fallback: ResolvedRuntimeConfig) -> ResolvedRuntimeConfig:
    tools_raw = data.get("tools", {})
    prompts_raw = data.get("prompts", {})
    capabilities_raw = data.get("capabilities", {})
    prompt_caps = capabilities_raw.get("prompts", {})
    skill_caps = capabilities_raw.get("skills", {})
    return ResolvedRuntimeConfig(
        model=str(data.get("model", fallback.model)),
        base_url=str(data.get("base_url", fallback.base_url)),
        system_prompt=str(data.get("system_prompt", fallback.system_prompt)),
        tools=ToolRuntimeConfig(
            enabled_tools=list(tools_raw.get("enabled_tools", fallback.tools.enabled_tools)),
            read_max_lines=int(tools_raw.get("read_max_lines", fallback.tools.read_max_lines)),
            bash_timeout_seconds=int(tools_raw.get("bash_timeout_seconds", fallback.tools.bash_timeout_seconds)),
            bash_max_output_bytes=int(tools_raw.get("bash_max_output_bytes", fallback.tools.bash_max_output_bytes)),
        ),
        prompts=PromptRuntimeConfig(order=list(prompts_raw.get("order", fallback.prompts.order))),
        capabilities=CapabilitiesConfig(
            prompts=PromptCapabilityConfig(paths=list(prompt_caps.get("paths", fallback.capabilities.prompts.paths))),
            skills=SkillCapabilityConfig(paths=list(skill_caps.get("paths", fallback.capabilities.skills.paths))),
        ),
    )


def agent_snapshot_to_dict(snapshot: AgentSnapshot) -> dict:
    return {
        "conversation": {
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                    "tool_name": message.tool_name,
                    "tool_calls": [
                        {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments}
                        for tool_call in message.tool_calls
                    ],
                    "created_at": message.created_at,
                    "metadata": dict(message.metadata),
                }
                for message in snapshot.conversation.messages
            ]
        },
        "runtime": {
            "cwd": snapshot.runtime.cwd,
            "runtime_config": _runtime_config_to_dict(snapshot.runtime.runtime_config),
            "skill_catalog_snapshot": [
                {
                    "name": entry.name,
                    "summary": entry.summary,
                    "when_to_use": entry.when_to_use,
                    "files": list(entry.files),
                    "source": entry.source,
                    "history_only": entry.history_only,
                }
                for entry in snapshot.runtime.skill_catalog_snapshot
            ],
            "templates": list(snapshot.runtime.templates),
            "pending_skill_trigger": (
                {
                    "name": snapshot.runtime.pending_skill_trigger.name,
                    "raw_command": snapshot.runtime.pending_skill_trigger.raw_command,
                }
                if snapshot.runtime.pending_skill_trigger is not None
                else None
            ),
        },
    }


def agent_snapshot_from_dict(data: dict, fallback_runtime_config: ResolvedRuntimeConfig) -> AgentSnapshot:
    conversation_raw = data.get("conversation", {})
    runtime_raw = data.get("runtime", {})
    messages = _messages_from_list(conversation_raw.get("messages", []))
    skill_catalog = _skill_catalog_from_list(runtime_raw.get("skill_catalog_snapshot", []))
    pending_skill_raw = runtime_raw.get("pending_skill_trigger")
    pending_skill = None
    if isinstance(pending_skill_raw, dict):
        name = pending_skill_raw.get("name")
        raw_command = pending_skill_raw.get("raw_command")
        if isinstance(name, str) and isinstance(raw_command, str):
            pending_skill = PendingSkillTriggerState(name=name, raw_command=raw_command)
    runtime_config_raw = runtime_raw.get("runtime_config")
    runtime_config = fallback_runtime_config
    if isinstance(runtime_config_raw, dict):
        runtime_config = _runtime_config_from_dict(runtime_config_raw, fallback_runtime_config)
    snapshot_cwd = runtime_raw.get("cwd")
    if not isinstance(snapshot_cwd, str) or not snapshot_cwd.strip():
        snapshot_cwd = ""
    return AgentSnapshot(
        conversation=AgentConversationState(messages=messages),
        runtime=AgentRuntimeState(
            cwd=snapshot_cwd,
            runtime_config=runtime_config,
            skill_catalog_snapshot=skill_catalog,
            templates=list(runtime_raw.get("templates", [])),
            pending_skill_trigger=pending_skill,
        ),
    )


def session_to_dict(session: Session) -> dict:
    payload = {
        "id": session.id,
        "name": session.name,
        "cwd": session.cwd,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "model": session.model,
        "system_prompt": session.system_prompt,
        "skill_catalog_snapshot": [
            {
                "name": entry.name,
                "summary": entry.summary,
                "when_to_use": entry.when_to_use,
                "files": list(entry.files),
                "source": entry.source,
                "history_only": entry.history_only,
            }
            for entry in session.skill_catalog_snapshot
        ],
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "tool_call_id": message.tool_call_id,
                "tool_name": message.tool_name,
                "tool_calls": [
                    {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments}
                    for tool_call in message.tool_calls
                ],
                "created_at": message.created_at,
                "metadata": dict(message.metadata),
            }
            for message in session.messages
        ],
        "parent_session_id": session.parent_session_id,
    }
    if session.agent_snapshot is not None:
        payload["agent_snapshot"] = agent_snapshot_to_dict(session.agent_snapshot)
    return payload


def session_from_dict(data: dict) -> Session:
    messages = _messages_from_list(data.get("messages", []))
    skill_catalog_snapshot = _skill_catalog_from_list(data.get("skill_catalog_snapshot", []))
    fallback_runtime_config = ResolvedRuntimeConfig(
        model=data["model"],
        base_url=DEFAULT_BASE_URL,
        system_prompt=data.get("system_prompt", ""),
    )
    agent_snapshot = None
    raw_agent_snapshot = data.get("agent_snapshot")
    if isinstance(raw_agent_snapshot, dict):
        agent_snapshot = agent_snapshot_from_dict(raw_agent_snapshot, fallback_runtime_config)
        if not agent_snapshot.runtime.cwd:
            agent_snapshot.runtime.cwd = data["cwd"]
    return Session(
        id=data["id"],
        name=data.get("name"),
        cwd=data["cwd"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        model=data["model"],
        system_prompt=data.get("system_prompt", ""),
        messages=messages,
        skill_catalog_snapshot=skill_catalog_snapshot,
        parent_session_id=data.get("parent_session_id"),
        agent_snapshot=agent_snapshot,
    )


def apply_agent_snapshot_to_session(session: Session, snapshot: AgentSnapshot) -> None:
    session.cwd = snapshot.runtime.cwd
    session.model = snapshot.runtime.runtime_config.model
    session.system_prompt = snapshot.runtime.runtime_config.system_prompt
    session.messages = clone_messages(snapshot.conversation.messages)
    session.skill_catalog_snapshot = clone_skill_catalog(snapshot.runtime.skill_catalog_snapshot)
    session.agent_snapshot = clone_agent_snapshot(snapshot)


def session_to_agent_snapshot(session: Session, fallback_runtime_config: ResolvedRuntimeConfig) -> AgentSnapshot:
    if session.agent_snapshot is not None:
        return clone_agent_snapshot(session.agent_snapshot)
    runtime_config = clone_resolved_runtime_config(fallback_runtime_config)
    runtime_config.model = session.model
    runtime_config.system_prompt = session.system_prompt
    return AgentSnapshot(
        conversation=AgentConversationState(messages=clone_messages(session.messages)),
        runtime=AgentRuntimeState(
            cwd=session.cwd,
            runtime_config=runtime_config,
            skill_catalog_snapshot=clone_skill_catalog(session.skill_catalog_snapshot),
            templates=[],
            pending_skill_trigger=None,
        ),
    )


def _messages_from_list(raw_messages: list[object]) -> list[Message]:
    messages: list[Message] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        tool_calls = [
            ToolCall(
                id=str(tool_call.get("id", "")),
                name=str(tool_call.get("name", "")),
                arguments=str(tool_call.get("arguments", "")),
            )
            for tool_call in raw.get("tool_calls", [])
            if isinstance(tool_call, dict)
        ]
        messages.append(
            Message(
                role=raw["role"],
                content=raw.get("content", ""),
                tool_call_id=raw.get("tool_call_id"),
                tool_name=raw.get("tool_name"),
                tool_calls=tool_calls,
                created_at=raw.get("created_at", ""),
                metadata=raw.get("metadata", {}),
            )
        )
    return messages


def _skill_catalog_from_list(raw_entries: list[object]) -> list[SkillCatalogEntry]:
    entries: list[SkillCatalogEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        entries.append(
            SkillCatalogEntry(
                name=raw.get("name", ""),
                summary=raw.get("summary", ""),
                when_to_use=raw.get("when_to_use", ""),
                files=list(raw.get("files", [])),
                source=raw.get("source", ""),
                history_only=bool(raw.get("history_only", False)),
            )
        )
    return entries


class SessionStore:
    def __init__(self, base_dir: Path | None = None):
        root = base_dir or Path.home() / ".astra-python" / "sessions"
        self.base_dir = root
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.base_dir / f"{session_id}.json"

    def create(self, cwd: str, model: str, system_prompt: str, name: str | None = None) -> Session:
        now = utc_now()
        return Session(
            id=uuid.uuid4().hex,
            name=name,
            cwd=cwd,
            created_at=now,
            updated_at=now,
            model=model,
            system_prompt=system_prompt,
            messages=[],
            skill_catalog_snapshot=[],
        )

    def load(self, session_id: str) -> Session:
        path = self._session_path(session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return session_from_dict(data)

    def save(self, session: Session) -> None:
        session.updated_at = utc_now()
        path = self._session_path(session.id)
        path.write_text(json.dumps(session_to_dict(session), ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []
        for path in sorted(self.base_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            data = json.loads(path.read_text(encoding="utf-8"))
            summaries.append(
                SessionSummary(
                    id=data["id"],
                    name=data.get("name"),
                    cwd=data.get("cwd", ""),
                    updated_at=data.get("updated_at", ""),
                    parent_session_id=data.get("parent_session_id"),
                )
            )
        return summaries

    def fork(self, session_id: str, name: str | None = None) -> Session:
        session = self.load(session_id)
        now = utc_now()
        forked = Session(
            id=uuid.uuid4().hex,
            name=name,
            cwd=session.cwd,
            created_at=now,
            updated_at=now,
            model=session.model,
            system_prompt=session.system_prompt,
            messages=clone_messages(session.messages),
            skill_catalog_snapshot=clone_skill_catalog(session.skill_catalog_snapshot),
            parent_session_id=session.id,
            agent_snapshot=clone_agent_snapshot(session.agent_snapshot) if session.agent_snapshot is not None else None,
        )
        self.save(forked)
        return forked
