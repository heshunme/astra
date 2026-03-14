from __future__ import annotations

from pathlib import Path

import pytest

from astra.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ConfigError,
    ConfigManager,
    RuntimeConfig,
    resolve_runtime_config,
)


pytestmark = pytest.mark.unit


def test_resolve_runtime_config_precedence() -> None:
    config = RuntimeConfig(model="config-model", base_url="http://config/v1", system_prompt="config prompt")
    resolved = resolve_runtime_config(
        config,
        cli_model="cli-model",
        cli_base_url=None,
        cli_system_prompt=None,
        env={"OPENAI_MODEL": "env-model", "OPENAI_BASE_URL": "http://env/v1"},
    )

    assert resolved.model == "cli-model"
    assert resolved.base_url == "http://config/v1"
    assert resolved.system_prompt == "config prompt"


def test_resolve_runtime_config_defaults() -> None:
    resolved = resolve_runtime_config(
        RuntimeConfig(),
        cli_model=None,
        cli_base_url=None,
        cli_system_prompt=None,
        env={},
    )

    assert resolved.model == DEFAULT_MODEL
    assert resolved.base_url == DEFAULT_BASE_URL
    assert resolved.system_prompt == ""


def test_config_manager_project_overrides_global(tmp_path: Path) -> None:
    global_config = tmp_path / "global.yaml"
    cwd = tmp_path / "workspace"
    project_config = cwd / ".astra" / "config.yaml"
    project_config.parent.mkdir(parents=True)

    global_config.write_text(
        """
model: global-model
base_url: http://global/v1
system_prompt: global prompt
tools:
  defaults:
    read:
      max_lines: 123
    bash:
      timeout_seconds: 30
      max_output_bytes: 2048
capabilities:
  skills:
    paths: [extras/skills]
""".strip(),
        encoding="utf-8",
    )

    project_config.write_text(
        """
model: project-model
tools:
  enabled: [read, ls]
  defaults:
    bash:
      timeout_seconds: 45
""".strip(),
        encoding="utf-8",
    )

    manager = ConfigManager(global_config_path=global_config)
    loaded = manager.load(cwd)

    assert loaded.model == "project-model"
    assert loaded.base_url == "http://global/v1"
    assert loaded.tools.enabled_tools == ["read", "ls"]
    assert loaded.tools.read_max_lines == 123
    assert loaded.tools.bash_timeout_seconds == 45
    assert loaded.tools.bash_max_output_bytes == 2048
    assert loaded.capabilities.skills.paths == ["extras/skills"]


def test_config_manager_invalid_tools_enabled_type(tmp_path: Path) -> None:
    global_config = tmp_path / "global.yaml"
    cwd = tmp_path / "workspace"
    project_config = cwd / ".astra" / "config.yaml"
    project_config.parent.mkdir(parents=True)

    global_config.write_text("{}", encoding="utf-8")
    project_config.write_text("tools:\n  enabled: read", encoding="utf-8")

    manager = ConfigManager(global_config_path=global_config)
    with pytest.raises(ConfigError, match="tools.enabled"):
        manager.load(cwd)


def test_config_manager_rejects_removed_skills_enabled(tmp_path: Path) -> None:
    global_config = tmp_path / "global.yaml"
    cwd = tmp_path / "workspace"
    project_config = cwd / ".astra" / "config.yaml"
    project_config.parent.mkdir(parents=True)

    global_config.write_text("{}", encoding="utf-8")
    project_config.write_text("capabilities:\n  skills:\n    enabled: [review]\n", encoding="utf-8")

    manager = ConfigManager(global_config_path=global_config)
    with pytest.raises(ConfigError, match="capabilities.skills.enabled has been removed"):
        manager.load(cwd)
