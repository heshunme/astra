from __future__ import annotations

from pathlib import Path

import pytest

from astra.config import PromptRuntimeConfig, ToolRuntimeConfig
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
    assert snapshot.skills["review"].files == [str((skill_dir / "checklist.md").resolve())]
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
