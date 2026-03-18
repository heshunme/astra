from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from astra.config import ResolvedRuntimeConfig
from astra.models import (
    AgentConversationState,
    AgentRuntimeState,
    AgentSnapshot,
    Message,
    PendingSkillTriggerState,
    SkillCatalogEntry,
    ToolCall,
)
from astra.session import SessionStore


pytestmark = pytest.mark.unit


def test_session_save_and_load_round_trip(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd="/repo", model="gpt-test", system_prompt="sys", name="demo")
    session.skill_catalog_snapshot.append(
        SkillCatalogEntry(
            name="review",
            summary="review checklist",
            when_to_use="Use for code review requests.",
            files=["/repo/.astra/skills/review/checklist.md"],
            source="/repo/.astra/skills/review/skill.yaml",
        )
    )
    session.messages.append(
        Message(
            role="assistant",
            content="hello",
            tool_calls=[ToolCall(id="tc1", name="read", arguments='{"path": "a.txt"}')],
            metadata={"a": 1},
        )
    )

    store.save(session)
    loaded = store.load(session.id)

    assert loaded.id == session.id
    assert loaded.name == "demo"
    assert loaded.model == "gpt-test"
    assert loaded.system_prompt == "sys"
    assert loaded.skill_catalog_snapshot[0].name == "review"
    assert not loaded.skill_catalog_snapshot[0].history_only
    assert loaded.messages[0].tool_calls[0].name == "read"
    assert loaded.messages[0].metadata == {"a": 1}


def test_session_fork_keeps_parent_and_messages(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd="/repo", model="gpt-test", system_prompt="sys")
    session.messages.append(Message(role="user", content="hi"))
    store.save(session)

    forked = store.fork(session.id, name="child")

    assert forked.parent_session_id == session.id
    assert forked.name == "child"
    assert len(forked.messages) == 1
    assert forked.messages[0].content == "hi"


def test_session_list_returns_latest_first(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    first = store.create(cwd="/repo", model="m", system_prompt="")
    store.save(first)
    time.sleep(0.01)
    second = store.create(cwd="/repo", model="m", system_prompt="")
    store.save(second)

    summaries = store.list()

    assert summaries[0].id == second.id
    assert summaries[1].id == first.id


def test_session_load_preserves_agent_snapshot(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd="/repo", model="gpt-test", system_prompt="sys", name="demo")
    session.agent_snapshot = AgentSnapshot(
        conversation=AgentConversationState(messages=[Message(role="user", content="hello")]),
        runtime=AgentRuntimeState(
            cwd="/repo",
            runtime_config=ResolvedRuntimeConfig(
                model="gpt-test",
                base_url="http://gateway/v1",
                system_prompt="sys",
            ),
            skill_catalog_snapshot=[],
            pending_skill_trigger=PendingSkillTriggerState(name="review", raw_command="/skill:review"),
        ),
    )

    store.save(session)
    loaded = store.load(session.id)

    assert loaded.agent_snapshot is not None
    assert loaded.agent_snapshot.runtime.runtime_config.base_url == "http://gateway/v1"
    assert loaded.agent_snapshot.runtime.pending_skill_trigger is not None
    assert loaded.agent_snapshot.runtime.pending_skill_trigger.name == "review"


def test_session_load_ignores_legacy_template_runtime_state(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd="/repo", model="gpt-test", system_prompt="sys", name="demo")
    payload = {
        "id": session.id,
        "name": session.name,
        "cwd": session.cwd,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "model": session.model,
        "system_prompt": session.system_prompt,
        "messages": [],
        "skill_catalog_snapshot": [],
        "agent_snapshot": {
            "conversation": {"messages": []},
            "runtime": {
                "cwd": "/repo",
                "runtime_config": {
                    "model": "gpt-test",
                    "base_url": "http://gateway/v1",
                    "system_prompt": "sys",
                    "tools": {
                        "enabled_tools": ["read", "write", "edit", "ls", "find", "grep", "bash"],
                        "read_max_lines": 400,
                        "bash_timeout_seconds": 60,
                        "bash_max_output_bytes": 32768,
                    },
                    "prompts": {"order": ["builtin:base", "config:system"]},
                    "capabilities": {"prompts": {"paths": []}, "skills": {"paths": []}},
                },
                "skill_catalog_snapshot": [],
                "templates": ["repo-rules"],
                "pending_skill_trigger": None,
            },
        },
    }
    (tmp_path / f"{session.id}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.load(session.id)

    assert loaded.agent_snapshot is not None
    assert loaded.agent_snapshot.runtime.runtime_config.base_url == "http://gateway/v1"
