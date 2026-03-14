from __future__ import annotations

from pathlib import Path

import pytest

from astra.agent import Agent, AgentConfig
from astra.models import ProviderEvent
from astra.runtime import CapabilityRuntime
from astra.session import SessionStore


pytestmark = pytest.mark.integration


class FakeProvider:
    def __init__(self, event_batches: list[list[ProviderEvent]]):
        self._event_batches = event_batches
        self._index = 0
        self.closed = False

    def close_active_stream(self) -> None:
        self.closed = True

    def stream_chat(self, _request):
        events = self._event_batches[self._index]
        self._index += 1
        for event in events:
            yield event


def _build_agent(cwd: Path, runtime_config, store_dir: Path) -> Agent:
    runtime = CapabilityRuntime(cwd)
    agent = Agent(
        AgentConfig(
            model=runtime_config.model,
            api_key="test-key",
            base_url=runtime_config.base_url,
            cwd=cwd,
            system_prompt=runtime_config.system_prompt,
        ),
        capability_runtime=runtime,
        session_store=SessionStore(base_dir=store_dir),
    )
    reload_result = agent.reload_runtime(runtime_config)
    assert reload_result.success
    return agent


def test_agent_does_not_persist_new_session_until_first_prompt(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    store_dir = tmp_path / "sessions"
    runtime_config = runtime_config_factory()

    agent = Agent(
        AgentConfig(
            model=runtime_config.model,
            api_key="test-key",
            base_url=runtime_config.base_url,
            cwd=cwd,
            system_prompt=runtime_config.system_prompt,
        ),
        capability_runtime=CapabilityRuntime(cwd),
        session_store=SessionStore(base_dir=store_dir),
    )

    reload_result = agent.reload_runtime(runtime_config)
    assert reload_result.success
    assert list(store_dir.glob("*.json")) == []

    agent.provider = FakeProvider([[ProviderEvent(type="text_delta", delta="ok"), ProviderEvent(type="done")]])
    result = agent.prompt("hello")

    assert result.error is None
    saved_sessions = list(store_dir.glob("*.json"))
    assert len(saved_sessions) == 1
    loaded = SessionStore(base_dir=store_dir).load(agent.session.id)
    assert loaded.name == "hello"
    assert loaded.messages[0].role == "user"
    assert loaded.messages[0].content == "hello"


def test_agent_persists_first_user_message_even_if_provider_fails(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    store_dir = tmp_path / "sessions"
    runtime_config = runtime_config_factory()

    class FailingProvider:
        def close_active_stream(self) -> None:
            return None

        def stream_chat(self, _request):
            raise RuntimeError("provider failed")

    agent = Agent(
        AgentConfig(
            model=runtime_config.model,
            api_key="test-key",
            base_url=runtime_config.base_url,
            cwd=cwd,
            system_prompt=runtime_config.system_prompt,
        ),
        capability_runtime=CapabilityRuntime(cwd),
        session_store=SessionStore(base_dir=store_dir),
    )
    reload_result = agent.reload_runtime(runtime_config)
    assert reload_result.success
    agent.provider = FailingProvider()

    result = agent.prompt("hello")

    assert result.error == "provider failed"
    loaded = SessionStore(base_dir=store_dir).load(agent.session.id)
    assert loaded.name == "hello"
    assert loaded.messages[0].role == "user"
    assert loaded.messages[0].content == "hello"


def test_agent_preserves_existing_session_name_on_first_prompt(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    store_dir = tmp_path / "sessions"
    runtime_config = runtime_config_factory()

    agent = Agent(
        AgentConfig(
            model=runtime_config.model,
            api_key="test-key",
            base_url=runtime_config.base_url,
            cwd=cwd,
            system_prompt=runtime_config.system_prompt,
        ),
        capability_runtime=CapabilityRuntime(cwd),
        session_store=SessionStore(base_dir=store_dir),
    )
    reload_result = agent.reload_runtime(runtime_config)
    assert reload_result.success
    agent.session.name = "preset"
    agent.provider = FakeProvider([[ProviderEvent(type="text_delta", delta="ok"), ProviderEvent(type="done")]])

    result = agent.prompt("hello")

    assert result.error is None
    loaded = SessionStore(base_dir=store_dir).load(agent.session.id)
    assert loaded.name == "preset"
    assert loaded.messages[0].content == "hello"


def test_agent_tool_call_round_trip(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "note.txt").write_text("hello from file\n", encoding="utf-8")

    runtime_config = runtime_config_factory()
    agent = _build_agent(cwd, runtime_config, tmp_path / "sessions")
    agent.provider = FakeProvider(
        [
            [
                ProviderEvent(
                    type="tool_call_delta",
                    index=0,
                    tool_call_id="call-1",
                    tool_name="read",
                    tool_arguments_delta='{"path":"note.txt"}',
                ),
                ProviderEvent(type="done"),
            ],
            [
                ProviderEvent(type="text_delta", delta="All good."),
                ProviderEvent(type="done"),
            ],
        ]
    )

    result = agent.prompt("inspect note")

    assert result.error is None
    assert len(result.tool_results) == 1
    assert "OK\n1: hello from file" in result.tool_results[0].content
    assert [message.content for message in result.assistant_messages] == ["", "All good."]


def test_agent_reload_blocked_while_streaming(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    runtime_config = runtime_config_factory()
    agent = _build_agent(cwd, runtime_config, tmp_path / "sessions")

    agent.is_streaming = True
    result = agent.reload_runtime(runtime_config)

    assert not result.success
    assert "Cannot reload while a response is streaming" in result.message


def test_agent_abort_delegates_to_provider(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    runtime_config = runtime_config_factory()
    agent = _build_agent(cwd, runtime_config, tmp_path / "sessions")

    fake_provider = FakeProvider([[ProviderEvent(type="done")]])
    agent.provider = fake_provider

    agent.abort()

    assert fake_provider.closed
