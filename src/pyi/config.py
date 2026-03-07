from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, cast


DEFAULT_MODEL = "gpt-5.2"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_SYSTEM_PROMPT = "You are a coding agent. Be concise, verify facts from files and use tools when needed."
DEFAULT_ENABLED_TOOLS = ("read", "write", "edit", "ls", "find", "grep", "bash")
DEFAULT_READ_MAX_LINES = 400
DEFAULT_BASH_TIMEOUT_SECONDS = 60
DEFAULT_BASH_MAX_OUTPUT_BYTES = 32 * 1024


class ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolRuntimeConfig:
    enabled_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED_TOOLS))
    read_max_lines: int = DEFAULT_READ_MAX_LINES
    bash_timeout_seconds: int = DEFAULT_BASH_TIMEOUT_SECONDS
    bash_max_output_bytes: int = DEFAULT_BASH_MAX_OUTPUT_BYTES


@dataclass(slots=True)
class RuntimeConfig:
    model: str | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    tools: ToolRuntimeConfig = field(default_factory=ToolRuntimeConfig)


@dataclass(slots=True)
class ResolvedRuntimeConfig:
    model: str
    base_url: str
    system_prompt: str
    tools: ToolRuntimeConfig = field(default_factory=ToolRuntimeConfig)


@dataclass(slots=True)
class ReloadResult:
    success: bool
    message: str
    applied_model: str
    applied_base_url: str
    enabled_tools: list[str]


def clone_tool_runtime_config(config: ToolRuntimeConfig) -> ToolRuntimeConfig:
    return ToolRuntimeConfig(
        enabled_tools=list(config.enabled_tools),
        read_max_lines=config.read_max_lines,
        bash_timeout_seconds=config.bash_timeout_seconds,
        bash_max_output_bytes=config.bash_max_output_bytes,
    )


def resolve_runtime_config(
    config: RuntimeConfig,
    cli_model: str | None,
    cli_base_url: str | None,
    cli_system_prompt: str | None,
    env: Mapping[str, str] | None = None,
) -> ResolvedRuntimeConfig:
    env_map = env or os.environ
    model = cli_model or config.model or env_map.get("OPENAI_MODEL") or DEFAULT_MODEL
    base_url = cli_base_url or config.base_url or env_map.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    system_prompt = cli_system_prompt or config.system_prompt or DEFAULT_SYSTEM_PROMPT
    return ResolvedRuntimeConfig(
        model=model,
        base_url=base_url,
        system_prompt=system_prompt,
        tools=clone_tool_runtime_config(config.tools),
    )


class ConfigManager:
    def __init__(self, global_config_path: Path | None = None):
        self.global_config_path = global_config_path or Path.home() / ".pyi-python" / "config.yaml"

    def project_config_path(self, cwd: Path) -> Path:
        return cwd / ".pyi" / "config.yaml"

    def load(self, cwd: Path) -> RuntimeConfig:
        merged = self._deep_merge(self._read_yaml(self.global_config_path), self._read_yaml(self.project_config_path(cwd)))
        return self._validate(merged)

    def reload(self, cwd: Path) -> RuntimeConfig:
        return self.load(cwd)

    def _read_yaml(self, path: Path) -> dict[str, object]:
        if not path.exists():
            return {}
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigError(f"PyYAML is required to read config file: {path}") from exc
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file must contain a mapping at top level: {path}")
        return cast(dict[str, object], loaded)

    def _deep_merge(self, base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
        merged = dict(base)
        for key, value in override.items():
            base_value = merged.get(key)
            if isinstance(base_value, dict) and isinstance(value, dict):
                merged[key] = self._deep_merge(cast(dict[str, object], base_value), cast(dict[str, object], value))
            else:
                merged[key] = value
        return merged

    def _validate(self, raw: dict[str, object]) -> RuntimeConfig:
        model = self._optional_string(raw.get("model"), "model")
        base_url = self._optional_string(raw.get("base_url"), "base_url")
        system_prompt = self._optional_string(raw.get("system_prompt"), "system_prompt")
        tools = ToolRuntimeConfig()

        tools_raw = raw.get("tools")
        if tools_raw is not None:
            tools_map = self._mapping(tools_raw, "tools")
            enabled_raw = tools_map.get("enabled")
            if enabled_raw is not None:
                enabled_list = self._string_list(enabled_raw, "tools.enabled")
                tools.enabled_tools = enabled_list

            defaults_raw = tools_map.get("defaults")
            if defaults_raw is not None:
                defaults_map = self._mapping(defaults_raw, "tools.defaults")

                read_raw = defaults_map.get("read")
                if read_raw is not None:
                    read_map = self._mapping(read_raw, "tools.defaults.read")
                    max_lines = read_map.get("max_lines")
                    if max_lines is not None:
                        tools.read_max_lines = self._positive_int(max_lines, "tools.defaults.read.max_lines")

                bash_raw = defaults_map.get("bash")
                if bash_raw is not None:
                    bash_map = self._mapping(bash_raw, "tools.defaults.bash")
                    timeout_seconds = bash_map.get("timeout_seconds")
                    max_output_bytes = bash_map.get("max_output_bytes")
                    if timeout_seconds is not None:
                        tools.bash_timeout_seconds = self._positive_int(
                            timeout_seconds,
                            "tools.defaults.bash.timeout_seconds",
                        )
                    if max_output_bytes is not None:
                        tools.bash_max_output_bytes = self._positive_int(
                            max_output_bytes,
                            "tools.defaults.bash.max_output_bytes",
                        )

        return RuntimeConfig(model=model, base_url=base_url, system_prompt=system_prompt, tools=tools)

    def _mapping(self, value: object, label: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ConfigError(f"{label} must be a mapping")
        return cast(dict[str, object], value)

    def _optional_string(self, value: object, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigError(f"{label} must be a string")
        return value

    def _string_list(self, value: object, label: str) -> list[str]:
        if not isinstance(value, list):
            raise ConfigError(f"{label} must be a list of strings")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ConfigError(f"{label} must contain only strings")
            result.append(item)
        return result

    def _positive_int(self, value: object, label: str) -> int:
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(f"{label} must be a positive integer")
        return value
