from __future__ import annotations

from pathlib import Path

import pytest

from astra.config import CapabilitiesConfig, PromptRuntimeConfig, SkillCapabilityConfig
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


def test_runtime_discovered_skill_is_inert_until_activated(tmp_path: Path, runtime_config_factory) -> None:
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
    runtime.reload(cfg)

    assert runtime.has_skill("review")
    assert "skill prompt body" not in runtime.inspect_prompt(cfg).assembled
    assert "skill prompt body" in runtime.inspect_prompt(cfg, ["skill:review"]).assembled


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


def test_runtime_enables_skill_from_config(tmp_path: Path, runtime_config_factory) -> None:
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
    (skill_dir / "checklist.md").write_text("enabled by config", encoding="utf-8")

    runtime = CapabilityRuntime(cwd)
    cfg = runtime_config_factory(
        capabilities=CapabilitiesConfig(skills=SkillCapabilityConfig(enabled=["review"])),
        prompts=PromptRuntimeConfig(order=["builtin:base", "config:system"]),
    )
    runtime.reload(cfg)

    assert "enabled by config" in runtime.inspect_prompt(cfg).assembled
