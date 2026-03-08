from __future__ import annotations

from pathlib import Path
import sys
from typing import Callable

import pytest

# Allow running tests without editable install.
ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astra.config import CapabilitiesConfig, PromptRuntimeConfig, ResolvedRuntimeConfig, ToolRuntimeConfig


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _default_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


@pytest.fixture
def runtime_config_factory() -> Callable[..., ResolvedRuntimeConfig]:
    def _build(
        *,
        model: str = "test-model",
        base_url: str = "http://127.0.0.1:9999/v1",
        system_prompt: str = "test system prompt",
        tools: ToolRuntimeConfig | None = None,
        prompts: PromptRuntimeConfig | None = None,
        capabilities: CapabilitiesConfig | None = None,
    ) -> ResolvedRuntimeConfig:
        return ResolvedRuntimeConfig(
            model=model,
            base_url=base_url,
            system_prompt=system_prompt,
            tools=tools or ToolRuntimeConfig(),
            prompts=prompts or PromptRuntimeConfig(),
            capabilities=capabilities or CapabilitiesConfig(),
        )

    return _build
