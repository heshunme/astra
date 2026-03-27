from __future__ import annotations

from pathlib import Path

import pytest

from astra.config import CapabilitiesConfig, PromptCapabilityConfig, PromptRuntimeConfig, SkillCapabilityConfig, ToolRuntimeConfig
from astra.runtime import CapabilityRuntime


pytestmark = pytest.mark.unit


def test_runtime_warns_for_missing_prompt_ref(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    prompt_dir = cwd / ".astra" / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "repo-rules.md").write_text("repo prompt body", encoding="utf-8")

    cfg = runtime_config_factory(
        prompts=PromptRuntimeConfig(order=["builtin:base", "config:system", "prompt:repo-rules", "prompt:missing"])
    )
    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(cfg)

    assert "prompt:repo-rules" in snapshot.prompt_fragments
    assert "prompt:repo-rules" in snapshot.diagnostics.loaded_prompts
    assert any("Prompt reference not found: prompt:missing" in warning for warning in snapshot.diagnostics.warnings)


def test_runtime_skill_parse_failure_is_soft_warning(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "broken"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text("name: [", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(runtime_config_factory())

    assert not runtime.has_skill("broken")
    assert any("Failed to parse skill file" in warning for warning in snapshot.diagnostics.warnings)


def test_runtime_discovers_skill_metadata_without_loading_body(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: review checklist
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("skill prompt body", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    cfg = runtime_config_factory(prompts=PromptRuntimeConfig(order=["builtin:base", "config:system"]))
    snapshot = runtime.reload(cfg)

    assert runtime.has_skill("review")
    assert snapshot.skills["review"].summary == "review checklist"
    assert snapshot.skills["review"].files == ["skill://review/checklist.md"]
    assert snapshot.skill_file_aliases["skill://review/checklist.md"] == (skill_dir / "checklist.md").resolve()
    assert "skill prompt body" not in runtime.inspect_prompt(cfg).assembled
    assert "skill:review" not in snapshot.prompt_fragments


def test_runtime_skill_when_to_use_is_optional_metadata(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "debug"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: debug
summary: debug checklist
when_to_use: Use for reproducing and isolating bugs.
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("debug prompt body", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(runtime_config_factory())

    assert snapshot.skills["debug"].when_to_use == "Use for reproducing and isolating bugs."


def test_runtime_rejects_skill_resource_that_escapes_skill_directory(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    escaped_file = cwd / ".astra" / "skills" / "outside.md"
    escaped_file.write_text("outside", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: review checklist
prompt_files:
  - ../outside.md
""".strip(),
        encoding="utf-8",
    )

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(runtime_config_factory())

    assert not runtime.has_skill("review")
    assert "review" not in snapshot.skills
    assert any("Skill resource escapes skill directory" in warning for warning in snapshot.diagnostics.warnings)
    assert all(str(escaped_file.resolve()) not in warning for warning in snapshot.diagnostics.warnings)


def test_runtime_rejects_skill_resource_absolute_path_outside_skill_directory(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    escaped_file = tmp_path / "outside.md"
    escaped_file.write_text("outside", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        f"""
name: review
summary: review checklist
prompt_files:
  - {escaped_file}
""".strip(),
        encoding="utf-8",
    )

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(runtime_config_factory())

    assert not runtime.has_skill("review")
    assert "review" not in snapshot.skills
    assert any("Skill resource escapes skill directory" in warning for warning in snapshot.diagnostics.warnings)
    assert all(str(escaped_file.resolve()) not in warning for warning in snapshot.diagnostics.warnings)


def test_runtime_allows_normalized_skill_resource_that_stays_within_skill_directory(
    tmp_path: Path, runtime_config_factory
) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    nested_dir = skill_dir / "nested"
    nested_dir.mkdir(parents=True)
    (skill_dir / "checklist.md").write_text("skill prompt body", encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: review checklist
prompt_files:
  - nested/../checklist.md
""".strip(),
        encoding="utf-8",
    )

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(runtime_config_factory())

    assert runtime.has_skill("review")
    assert snapshot.skills["review"].files == ["skill://review/checklist.md"]
    assert snapshot.skill_file_aliases["skill://review/checklist.md"] == (skill_dir / "checklist.md").resolve()


def test_runtime_warns_when_skills_exist_but_read_tool_is_disabled(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    skill_dir = cwd / ".astra" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: review checklist
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("skill prompt body", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(
        runtime_config_factory(
            tools=ToolRuntimeConfig(enabled_tools=["write", "edit", "ls", "find", "grep", "bash"])
        )
    )

    assert any("read tool is disabled" in warning for warning in snapshot.diagnostics.warnings)


def test_runtime_resolves_duplicate_skills_with_explicit_priority(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    global_skill_dir = tmp_path / ".astra-python" / "skills" / "review"
    extra_one_dir = cwd / "extras-one" / "review"
    extra_two_dir = cwd / "extras-two" / "review"
    project_skill_dir = cwd / ".astra" / "skills" / "review"

    for index, skill_dir in enumerate((global_skill_dir, extra_one_dir, extra_two_dir, project_skill_dir), start=1):
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text(
            f"""
name: review
summary: review skill {index}
prompt_files:
  - checklist.md
""".strip(),
            encoding="utf-8",
        )
        (skill_dir / "checklist.md").write_text(f"body {index}", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(
        runtime_config_factory(
            capabilities=CapabilitiesConfig(
                prompts=PromptCapabilityConfig(paths=[]),
                skills=SkillCapabilityConfig(paths=["extras-one", "extras-two"]),
            )
        )
    )

    assert snapshot.skills["review"].summary == "review skill 4"
    assert snapshot.skills["review"].source == "skill://review"
    assert snapshot.skills["review"].source_label == "project (.astra/skills)"
    assert snapshot.skills["review"].files == ["skill://review/checklist.md"]
    assert snapshot.skill_file_aliases["skill://review/checklist.md"] == (project_skill_dir / "checklist.md").resolve()
    assert snapshot.skills["review"].shadowed_sources == ["skill://review", "skill://review", "skill://review"]
    assert snapshot.diagnostics.loaded_skills == ["review"]
    assert len(snapshot.diagnostics.skill_conflicts) == 1
    conflict = snapshot.diagnostics.skill_conflicts[0]
    assert conflict.name == "review"
    assert conflict.winner_source == "skill://review"
    assert conflict.winner_source_label == "project (.astra/skills)"
    assert conflict.shadowed_sources == ["skill://review", "skill://review", "skill://review"]
    assert "global (~/.astra-python/skills)" in conflict.shadowed_source_labels
    assert "extra[1]" in conflict.shadowed_source_labels[1]
    assert "extra[2]" in conflict.shadowed_source_labels[2]
    assert any("Skill conflict for review" in warning for warning in snapshot.diagnostics.warnings)


def test_runtime_assigns_skill_aliases_for_global_skills(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    skill_dir = tmp_path / ".astra-python" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """
name: review
summary: global review checklist
prompt_files:
  - checklist.md
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "checklist.md").write_text("global body", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    snapshot = runtime.reload(runtime_config_factory())

    assert snapshot.skills["review"].files == ["skill://review/checklist.md"]
    assert snapshot.skill_file_aliases["skill://review/checklist.md"] == (skill_dir / "checklist.md").resolve()


def test_runtime_template_alias_maps_to_prompt_key(tmp_path: Path, runtime_config_factory) -> None:
    cwd = tmp_path / "workspace"
    prompt_dir = cwd / ".astra" / "prompts"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "repo-rules.md").write_text("template body", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    cfg = runtime_config_factory()
    runtime.reload(cfg)

    inspection = runtime.inspect_prompt(cfg, ["template:repo-rules"])
    assert "template body" in inspection.assembled
    assert "repo-rules" in runtime.list_template_names()
