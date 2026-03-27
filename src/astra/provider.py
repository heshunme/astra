from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

import litellm

from .models import ProviderEvent


class ProviderError(RuntimeError):
    pass


class ProviderAborted(ProviderError):
    pass


@dataclass(slots=True)
class ProviderRequest:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    api_key: str | None
    base_url: str
    temperature: float = 0.0


def _get_value(payload: Any, key: str, default: Any = None) -> Any:
    if payload is None:
        return default
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _coerce_dict(payload: Any) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=False)
        if isinstance(dumped, dict):
            return dumped
    return None


def _coerce_list(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, tuple):
        return list(payload)
    return []


def _normalize_model(model: str) -> str:
    return model if "/" in model else f"openai/{model}"


def _looks_like_abort(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    abort_markers = ("abort", "cancel", "closed", "close", "disconnect", "generatorexit", "stopiteration")
    return any(marker in name or marker in message for marker in abort_markers)


class OpenAICompatibleProvider:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._active_stream: Any | None = None

    def close_active_stream(self) -> None:
        stream = self._active_stream
        self._active_stream = None
        if stream is None:
            return
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    def _completion_kwargs(self, request: ProviderRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": _normalize_model(request.model),
            "messages": request.messages,
            "stream": True,
            "temperature": request.temperature,
            "api_base": request.base_url,
        }
        if request.api_key:
            kwargs["api_key"] = request.api_key
        if request.tools:
            kwargs["tools"] = request.tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _stream_chunks(self, request: ProviderRequest) -> Any:
        return litellm.completion(**self._completion_kwargs(request))

    def _usage_from_chunk(self, chunk: Any) -> dict[str, Any] | None:
        usage = _get_value(chunk, "usage")
        usage_dict = _coerce_dict(usage)
        if usage_dict is not None:
            return usage_dict
        return usage if isinstance(usage, dict) else None

    def stream_chat(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        try:
            stream = self._stream_chunks(request)
            self._active_stream = stream
            done_emitted = False
            for chunk in stream:
                usage = self._usage_from_chunk(chunk)
                if usage:
                    yield ProviderEvent(type="usage", usage=usage)

                choices = _coerce_list(_get_value(chunk, "choices"))
                if not choices:
                    continue

                delta = _get_value(choices[0], "delta")
                content = _get_value(delta, "content")
                if isinstance(content, str) and content:
                    yield ProviderEvent(type="text_delta", delta=content)

                for tool_call in _coerce_list(_get_value(delta, "tool_calls")):
                    function = _get_value(tool_call, "function")
                    yield ProviderEvent(
                        type="tool_call_delta",
                        index=_get_value(tool_call, "index"),
                        tool_call_id=_get_value(tool_call, "id"),
                        tool_name=_get_value(function, "name"),
                        tool_arguments_delta=_get_value(function, "arguments", "") or "",
                    )

                finish_reason = _get_value(choices[0], "finish_reason")
                if finish_reason in {"stop", "tool_calls"}:
                    yield ProviderEvent(type="done")
                    done_emitted = True
                    break
        except Exception as exc:
            if _looks_like_abort(exc):
                raise ProviderAborted("Provider stream aborted") from exc
            raise ProviderError(str(exc)) from exc
        finally:
            try:
                self.close_active_stream()
            except Exception:
                pass

        if not done_emitted:
            yield ProviderEvent(type="done")
