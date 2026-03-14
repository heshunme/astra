from __future__ import annotations

from pathlib import Path

import pytest

from astra.agent import Agent, AgentConfig
from astra.config import ToolRuntimeConfig
from astra.models import AgentEvent, Message, ProviderEvent
from astra.runtime import CapabilityRuntime


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


class RecordingProvider:
    def __init__(self, events: list[ProviderEvent]):
        self.events = events
        self.requests = []

    def close_active_stream(self) -> None:
        return None

    def stream_chat(self, request):
        self.requests.append(request)
        for event in self.events:
            yield event


def _build_agent(cwd: Path, runtime_config) -> Agent:
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
    )
    reload_result = agent.apply_runtime_config(runtime_config)
    assert reload_result.success
    return agent


def test_agent_event_stream_wraps_single_prompt(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    agent = _build_agent(cwd, runtime_config_factory())
    agent.provider = FakeProvider([[ProviderEvent(type="text_delta", delta="ok"), ProviderEvent(type="done")]])

    events: list[AgentEvent] = []
    unsubscribe = agent.subscribe(events.append)
    try:
        result = agent.prompt("hello")
    finally:
        unsubscribe()

    assert result.error is None
    assert [event.type for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert agent.messages[0].role == "user"
    assert agent.messages[0].content == "hello"


def test_agent_tool_call_round_trip(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "note.txt").write_text("hello from file\n", encoding="utf-8")

    agent = _build_agent(cwd, runtime_config_factory())
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
    agent = _build_agent(cwd, runtime_config_factory())

    agent.is_streaming = True
    result = agent.apply_runtime_config(runtime_config_factory())

    assert not result.success
    assert "Cannot reload while a response is streaming" in result.message


def test_agent_abort_delegates_to_provider(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    agent = _build_agent(cwd, runtime_config_factory())

    fake_provider = FakeProvider([[ProviderEvent(type="done")]])
    agent.provider = fake_provider

    agent.abort()

    assert fake_provider.closed


def test_agent_wait_for_idle_returns_immediately_when_not_streaming(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    agent = _build_agent(cwd, runtime_config_factory())

    assert agent.wait_for_idle(timeout=0.01)


def test_agent_inline_skill_command_runs_through_core_extension_handler(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: Review checklist
when_to_use: Use for code review requests.
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("Review checklist prompt body.", encoding="utf-8")

    agent = _build_agent(cwd, runtime_config_factory())
    provider = RecordingProvider([ProviderEvent(type="text_delta", delta="done"), ProviderEvent(type="done")])
    agent.provider = provider

    result = agent.try_handle_extension_command("/skill:review Review src/demo.py for issues.")

    assert result is not None
    assert result.run_result is not None
    assert result.run_result.error is None
    assert provider.requests
    sent_messages = provider.requests[0].messages
    assert sent_messages[0]["role"] == "system"
    assert "Skill catalog for this session" in str(sent_messages[0]["content"])
    assert "Review checklist" in str(sent_messages[0]["content"])
    assert sent_messages[1]["role"] == "user"
    assert "Please use the skill 'review' for this turn only." in str(sent_messages[1]["content"])
    assert agent.messages[0].metadata["raw_user_input"] == "/skill:review Review src/demo.py for issues."


def test_agent_pending_skill_consumes_next_prompt_once(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "debug"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: debug
summary: Debug checklist
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("Debug checklist prompt body.", encoding="utf-8")

    agent = _build_agent(cwd, runtime_config_factory())

    armed = agent.try_handle_extension_command("/skill:debug")
    assert armed is not None
    assert armed.message == "Next message will use skill: debug"
    assert agent.pending_skill_name == "debug"

    consumed, rewritten, metadata = agent.consume_pending_skill_prompt("Investigate why tests fail.")
    assert consumed
    assert "Please use the skill 'debug' for this turn only." in rewritten
    assert metadata is not None
    assert agent.pending_skill_name is None

    consumed_again, plain_text, plain_metadata = agent.consume_pending_skill_prompt("A plain follow-up.")
    assert consumed_again
    assert plain_text == "A plain follow-up."
    assert plain_metadata is None


def test_agent_snapshot_restore_round_trip_preserves_runtime_state(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    prompt_dir = cwd / ".astra" / "prompts"
    skill_dir = cwd / ".astra" / "skills" / "review"
    prompt_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (prompt_dir / "repo-rules.md").write_text("Use the repo rules.", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: Review checklist
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("Review checklist prompt body.", encoding="utf-8")

    agent = _build_agent(cwd, runtime_config_factory())
    agent.replace_messages([Message(role="user", content="hello")])
    agent.activate_template("repo-rules")
    agent.arm_skill("review", "/skill:review")

    snapshot = agent.snapshot()
    restored = _build_agent(cwd, runtime_config_factory())
    restored.restore(snapshot)

    assert restored.messages[0].content == "hello"
    assert restored.active_templates == ["repo-rules"]
    assert restored.pending_skill_name == "review"
    assert restored.runtime_config.model == agent.runtime_config.model


def test_agent_marks_removed_skills_as_history_only_after_reload(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: Review checklist
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("Review checklist prompt body.", encoding="utf-8")

    agent = _build_agent(cwd, runtime_config_factory())
    assert agent.available_skill_names() == ["review"]

    (skill_dir / "skill.yaml").unlink()
    (skill_dir / "checklist.md").unlink()
    skill_dir.rmdir()

    result = agent.apply_runtime_config(runtime_config_factory())

    assert result.success
    assert agent.available_skill_names() == []
    assert agent.history_only_skill_names() == ["review"]
    assert "review" not in agent.current_system_prompt
    success, message = agent.arm_skill("review", "/skill:review")
    assert not success
    assert "no longer available" in message


def test_agent_rejects_skill_usage_when_read_tool_is_disabled(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: Review checklist
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("Review checklist prompt body.", encoding="utf-8")

    agent = _build_agent(
        cwd,
        runtime_config_factory(
            tools=ToolRuntimeConfig(enabled_tools=["write", "edit", "ls", "find", "grep", "bash"])
        ),
    )

    assert "Skill catalog for this session" not in agent.current_system_prompt
    assert "review" not in agent.current_system_prompt
    success, message = agent.arm_skill("review", "/skill:review")
    assert not success
    assert "read tool is disabled" in message
