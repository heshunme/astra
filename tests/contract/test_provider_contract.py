from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from astra.provider import OpenAICompatibleProvider, ProviderAborted, ProviderError, ProviderRequest


pytestmark = pytest.mark.contract


class _UsagePayload:
    def model_dump(self, exclude_none: bool = False) -> dict[str, int]:
        return {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


class _ClosableStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)

    def close(self) -> None:
        self.closed = True


def test_provider_translates_litellm_stream_and_normalizes_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}
    chunks = [
        {
            "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
            "choices": [
                {
                    "delta": {"content": "hello"},
                    "finish_reason": None,
                }
            ],
        },
        SimpleNamespace(
            usage=_UsagePayload(),
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call-1",
                                function=SimpleNamespace(name="read", arguments='{"path":"a.txt"}'),
                            )
                        ]
                    ),
                    finish_reason="tool_calls",
                )
            ],
        ),
    ]
    stream = _ClosableStream(chunks)

    def fake_completion(**kwargs):
        captured_kwargs.update(kwargs)
        return stream

    monkeypatch.setattr("astra.provider.litellm.completion", fake_completion)

    provider = OpenAICompatibleProvider("http://127.0.0.1:4000/v1")
    request = ProviderRequest(
        model="gpt-5.2",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "read"}}],
        api_key="test-key",
        base_url=provider.base_url,
        runtime_env={"OPENAI_API_KEY": "test-key"},
    )

    events = list(provider.stream_chat(request))

    assert captured_kwargs["model"] == "openai/gpt-5.2"
    assert captured_kwargs["api_base"] == "http://127.0.0.1:4000/v1"
    assert captured_kwargs["api_key"] == "test-key"
    assert captured_kwargs["tool_choice"] == "auto"
    assert [event.type for event in events] == ["usage", "text_delta", "usage", "tool_call_delta", "done"]
    assert events[1].delta == "hello"
    assert events[3].tool_name == "read"
    assert events[3].tool_arguments_delta == '{"path":"a.txt"}'
    assert stream.closed is True


def test_provider_passes_provider_qualified_models_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}

    def fake_completion(**kwargs):
        captured_kwargs.update(kwargs)
        return _ClosableStream(
            [
                {
                    "choices": [
                        {
                            "delta": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ]
        )

    monkeypatch.setattr("astra.provider.litellm.completion", fake_completion)

    provider = OpenAICompatibleProvider("http://localhost:11434")
    request = ProviderRequest(
        model="ollama/llama3.2",
        messages=[],
        tools=[],
        api_key=None,
        base_url=provider.base_url,
        runtime_env={},
    )

    list(provider.stream_chat(request))

    assert captured_kwargs["model"] == "ollama/llama3.2"
    assert captured_kwargs["api_base"] == "http://localhost:11434"
    assert "api_key" not in captured_kwargs
    assert "tools" not in captured_kwargs
    assert "tool_choice" not in captured_kwargs


def test_provider_overlays_runtime_env_for_litellm_and_restores_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str | None] = {}

    def fake_completion(**_kwargs):
        captured_env["anthropic"] = os.environ.get("ANTHROPIC_API_KEY")
        captured_env["gemini"] = os.environ.get("GEMINI_API_KEY")
        return _ClosableStream(
            [
                {
                    "choices": [
                        {
                            "delta": {"content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ]
        )

    monkeypatch.setattr("astra.provider.litellm.completion", fake_completion)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell-anthropic")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    provider = OpenAICompatibleProvider("http://127.0.0.1:4000/v1")
    request = ProviderRequest(
        model="anthropic/claude-sonnet-4-5",
        messages=[],
        tools=[],
        api_key=None,
        base_url=provider.base_url,
        runtime_env={
            "ANTHROPIC_API_KEY": "dotenv-anthropic",
            "GEMINI_API_KEY": "dotenv-gemini",
        },
    )

    list(provider.stream_chat(request))

    assert captured_env["anthropic"] == "dotenv-anthropic"
    assert captured_env["gemini"] == "dotenv-gemini"
    assert os.environ.get("ANTHROPIC_API_KEY") == "shell-anthropic"
    assert "GEMINI_API_KEY" not in os.environ


def test_provider_maps_litellm_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_completion(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("astra.provider.litellm.completion", fake_completion)

    provider = OpenAICompatibleProvider("http://127.0.0.1:4000/v1")
    request = ProviderRequest(
        model="gpt-5.2",
        messages=[],
        tools=[],
        api_key="k",
        base_url=provider.base_url,
        runtime_env={"OPENAI_API_KEY": "k"},
    )

    with pytest.raises(ProviderError, match="boom"):
        list(provider.stream_chat(request))


def test_provider_maps_abort_like_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_completion(**_kwargs):
        raise RuntimeError("stream closed")

    monkeypatch.setattr("astra.provider.litellm.completion", fake_completion)

    provider = OpenAICompatibleProvider("http://127.0.0.1:4000/v1")
    request = ProviderRequest(
        model="gpt-5.2",
        messages=[],
        tools=[],
        api_key="k",
        base_url=provider.base_url,
        runtime_env={"OPENAI_API_KEY": "k"},
    )

    with pytest.raises(ProviderAborted, match="Provider stream aborted"):
        list(provider.stream_chat(request))


def test_close_active_stream_closes_stream_object() -> None:
    stream = _ClosableStream([])
    provider = OpenAICompatibleProvider("http://127.0.0.1:4000/v1")
    provider._active_stream = stream

    provider.close_active_stream()

    assert stream.closed is True
    assert provider._active_stream is None
