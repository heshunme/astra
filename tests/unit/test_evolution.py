from __future__ import annotations

import pytest

from astra.evolution import EvolutionRequest, SkillEvolutionService
from astra.models import AgentConversationState, AgentRuntimeState, AgentSnapshot, Message, ToolCall


pytestmark = pytest.mark.unit


def test_evolution_creates_project_skill_from_latest_completed_turn(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    snapshot = AgentSnapshot(
        conversation=AgentConversationState(
            messages=[
                Message(role="user", content="Please review src/demo.py for issues."),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(id="call-1", name="read", arguments='{"path":"src/demo.py"}'),
                        ToolCall(id="call-2", name="grep", arguments='{"pattern":"TODO","path":"src"}'),
                    ],
                ),
                Message(role="tool_result", content="OK", tool_call_id="call-1", tool_name="read"),
                Message(role="tool_result", content="OK", tool_call_id="call-2", tool_name="grep"),
                Message(role="assistant", content="Found an unchecked branch and a stale TODO."),
            ]
        ),
        runtime=AgentRuntimeState(cwd=str(cwd), runtime_config=runtime_config_factory()),
    )

    service = SkillEvolutionService(cwd)
    outcome = service.evolve(snapshot, EvolutionRequest(goal="Capture repeatable review workflow"))

    skill_dir = cwd / ".astra" / "skills" / "code-review-workflow"
    assert outcome.created == ["code_review_workflow"]
    assert outcome.updated == []
    assert outcome.warnings == []
    assert outcome.skill_name == "code_review_workflow"
    assert (skill_dir / "skill.yaml").exists()
    assert (skill_dir / "checklist.md").exists()
    assert "Capture repeatable review workflow" in (skill_dir / "checklist.md").read_text(encoding="utf-8")


def test_evolution_skips_slash_command_turns(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    snapshot = AgentSnapshot(
        conversation=AgentConversationState(
            messages=[
                Message(
                    role="user",
                    content="Please use the skill 'review' for this turn only.",
                    metadata={"raw_user_input": "/skill:review check this"},
                ),
                Message(role="assistant", content="done"),
            ]
        ),
        runtime=AgentRuntimeState(cwd=str(cwd), runtime_config=runtime_config_factory()),
    )

    outcome = SkillEvolutionService(cwd).evolve(snapshot)

    assert outcome.created == []
    assert outcome.updated == []
    assert outcome.skipped == ["no_reusable_experience"]
    assert any("No completed normal user turn" in warning for warning in outcome.warnings)


def test_evolution_is_idempotent_for_same_snapshot(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    snapshot = AgentSnapshot(
        conversation=AgentConversationState(
            messages=[
                Message(role="user", content="Investigate why tests are failing."),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="call-1", name="bash", arguments='{"command":"pytest"}')],
                ),
                Message(role="tool_result", content="FAIL", tool_call_id="call-1", tool_name="bash"),
                Message(role="assistant", content="The failure comes from a missing fixture."),
            ]
        ),
        runtime=AgentRuntimeState(cwd=str(cwd), runtime_config=runtime_config_factory()),
    )
    service = SkillEvolutionService(cwd)

    first = service.evolve(snapshot)
    second = service.evolve(snapshot)

    assert first.created == ["debug_issue_workflow"]
    assert second.created == []
    assert second.updated == []
    assert second.skipped == ["no_changes"]


def test_evolution_soft_fails_on_invalid_existing_skill_yaml(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "code-review-workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text("name: [", encoding="utf-8")
    (skill_dir / "checklist.md").write_text("# Existing\n", encoding="utf-8")

    snapshot = AgentSnapshot(
        conversation=AgentConversationState(
            messages=[
                Message(role="user", content="Please review src/demo.py for issues."),
                Message(role="assistant", content="Review found one issue."),
            ]
        ),
        runtime=AgentRuntimeState(cwd=str(cwd), runtime_config=runtime_config_factory()),
    )

    outcome = SkillEvolutionService(cwd).evolve(snapshot)

    assert outcome.created == []
    assert outcome.updated == []
    assert outcome.skipped == ["invalid_skill_yaml"]
    assert any("Failed to parse existing skill file" in warning for warning in outcome.warnings)
