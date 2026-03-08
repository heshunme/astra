from __future__ import annotations

import time
from pathlib import Path

import pytest

from astra.models import Message, ToolCall
from astra.session import SessionStore


pytestmark = pytest.mark.unit


def test_session_save_and_load_round_trip(tmp_path: Path) -> None:
    store = SessionStore(base_dir=tmp_path)
    session = store.create(cwd="/repo", model="gpt-test", system_prompt="sys", name="demo")
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
