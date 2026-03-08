from __future__ import annotations

import http.client
import json
import ssl
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

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
    api_key: str
    base_url: str
    temperature: float = 0.0


class SSEStream:
    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse):
        self._connection = connection
        self._response = response
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._connection.close()

    def iter_events(self) -> Iterator[str]:
        data_lines: list[str] = []
        while True:
            raw_line = self._response.readline()
            if not raw_line:
                if data_lines:
                    yield "\n".join(data_lines)
                return
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())


class OpenAICompatibleProvider:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._active_stream: SSEStream | None = None

    def close_active_stream(self) -> None:
        if self._active_stream is not None:
            self._active_stream.close()
            self._active_stream = None

    def _build_connection(self, parsed_url: Any) -> http.client.HTTPConnection:
        port = parsed_url.port
        if parsed_url.scheme == "https":
            context = ssl.create_default_context()
            return http.client.HTTPSConnection(parsed_url.hostname, port or 443, context=context, timeout=300)
        return http.client.HTTPConnection(parsed_url.hostname, port or 80, timeout=300)

    def _request_stream(self, request: ProviderRequest) -> SSEStream:
        target = f"{request.base_url.rstrip('/')}/chat/completions"
        parsed = urlparse(target)
        path = parsed.path or "/chat/completions"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection = self._build_connection(parsed)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "tools": request.tools,
            "tool_choice": "auto",
            "stream": True,
            "temperature": request.temperature,
            "stream_options": {"include_usage": True},
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {request.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            error_body = response.read().decode("utf-8", errors="replace")
            connection.close()
            raise ProviderError(f"Provider request failed with {response.status}: {error_body}")
        return SSEStream(connection, response)

    def stream_chat(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        stream = self._request_stream(request)
        self._active_stream = stream
        try:
            for payload in stream.iter_events():
                if payload == "[DONE]":
                    yield ProviderEvent(type="done")
                    break
                chunk = json.loads(payload)
                usage = chunk.get("usage")
                if usage:
                    yield ProviderEvent(type="usage", usage=usage)
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield ProviderEvent(type="text_delta", delta=content)
                for tool_call in delta.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    yield ProviderEvent(
                        type="tool_call_delta",
                        index=tool_call.get("index"),
                        tool_call_id=tool_call.get("id"),
                        tool_name=function.get("name"),
                        tool_arguments_delta=function.get("arguments") or "",
                    )
                finish_reason = choices[0].get("finish_reason")
                if finish_reason in {"stop", "tool_calls"}:
                    yield ProviderEvent(type="done")
                    break
        except OSError as exc:
            raise ProviderAborted("Provider stream aborted") from exc
        finally:
            stream.close()
            if self._active_stream is stream:
                self._active_stream = None
