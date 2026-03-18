from __future__ import annotations

from pathlib import Path

import pytest

from astra.agent import Agent, AgentConfig
from astra.evolution import SkillEvolutionService
from astra.models import AgentConversationState, AgentRuntimeState, AgentSnapshot, Message, ProviderEvent, ToolCall
from astra.runtime import CapabilityRuntime


pytestmark = pytest.mark.integration


class FakeProvider:
    def __init__(self, event_batches: list[list[ProviderEvent]]):
        self._event_batches = event_batches
        self._index = 0

    def close_active_stream(self) -> None:
        return None

    def stream_chat(self, _request):
        events = self._event_batches[self._index]
        self._index += 1
        for event in events:
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


def test_evolution_written_skill_is_discoverable_by_runtime_reload(
    tmp_path: Path,
    runtime_config_factory,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    snapshot = AgentSnapshot(
        conversation=AgentConversationState(
            messages=[
                Message(role="user", content="Please review src/demo.py for issues."),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="call-1", name="read", arguments='{"path":"src/demo.py"}')],
                ),
                Message(role="tool_result", content="OK", tool_call_id="call-1", tool_name="read"),
                Message(role="assistant", content="Found one correctness issue."),
            ]
        ),
        runtime=AgentRuntimeState(cwd=str(cwd), runtime_config=runtime_config_factory()),
    )

    outcome = SkillEvolutionService(cwd).evolve(snapshot)

    assert outcome.created == ["code_review_workflow"]

    runtime = CapabilityRuntime(cwd)
    reload_result = runtime.reload(runtime_config_factory())

    assert "code_review_workflow" in reload_result.skills
    assert reload_result.skills["code_review_workflow"].summary.startswith("Reusable workflow for")


def test_evolution_can_run_from_real_agent_snapshot(
    tmp_path: Path,
    runtime_config_factory,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / "demo.py").write_text("value = 1\n", encoding="utf-8")
    agent = _build_agent(cwd, runtime_config_factory())
    agent.provider = FakeProvider(
        [
            [
                ProviderEvent(
                    type="tool_call_delta",
                    index=0,
                    tool_call_id="call-1",
                    tool_name="read",
                    tool_arguments_delta='{"path":"demo.py"}',
                ),
                ProviderEvent(type="done"),
            ],
            [
                ProviderEvent(type="text_delta", delta="Found one issue and one follow-up check."),
                ProviderEvent(type="done"),
            ],
        ]
    )

    result = agent.prompt("Please review demo.py for issues.")
    assert result.error is None

    outcome = SkillEvolutionService(cwd).evolve(agent.snapshot())

    assert outcome.created == ["code_review_workflow"]
    assert outcome.skill_name == "code_review_workflow"
    assert any(path.endswith("skill.yaml") for path in outcome.written_files)

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(runtime_config_factory())
    assert "code_review_workflow" in snapshot.skills
