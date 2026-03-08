from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .models import Message, Session, SessionSummary, ToolCall


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def session_to_dict(session: Session) -> dict:
    return {
        "id": session.id,
        "name": session.name,
        "cwd": session.cwd,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "model": session.model,
        "system_prompt": session.system_prompt,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "tool_call_id": message.tool_call_id,
                "tool_name": message.tool_name,
                "tool_calls": [asdict(tool_call) for tool_call in message.tool_calls],
                "created_at": message.created_at,
                "metadata": message.metadata,
            }
            for message in session.messages
        ],
        "parent_session_id": session.parent_session_id,
    }


def session_from_dict(data: dict) -> Session:
    messages = []
    for raw in data.get("messages", []):
        tool_calls = [ToolCall(**tool_call) for tool_call in raw.get("tool_calls", [])]
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
    return Session(
        id=data["id"],
        name=data.get("name"),
        cwd=data["cwd"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        model=data["model"],
        system_prompt=data.get("system_prompt", ""),
        messages=messages,
        parent_session_id=data.get("parent_session_id"),
    )


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
            messages=list(session.messages),
            parent_session_id=session.id,
        )
        self.save(forked)
        return forked
