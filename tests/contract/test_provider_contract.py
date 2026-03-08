from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import pytest

from astra.provider import OpenAICompatibleProvider, ProviderError, ProviderRequest


pytestmark = pytest.mark.contract


@contextmanager
def run_server(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_provider_parses_sse_stream() -> None:
    class SSEHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args) -> None:  # pragma: no cover
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            chunks = [
                {
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                    "choices": [{"delta": {"content": "hello"}, "finish_reason": None}],
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ]
            for payload in chunks:
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    with run_server(SSEHandler) as server:
        provider = OpenAICompatibleProvider(f"http://127.0.0.1:{server.server_port}/v1")
        request = ProviderRequest(model="m", messages=[], tools=[], api_key="k", base_url=provider.base_url)
        events = list(provider.stream_chat(request))

    assert [event.type for event in events] == ["usage", "text_delta", "tool_call_delta", "done"]
    assert events[1].delta == "hello"
    assert events[2].tool_name == "read"
    assert events[2].tool_arguments_delta == '{"path":"a.txt"}'


def test_provider_raises_on_non_2xx() -> None:
    class ErrorHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args) -> None:  # pragma: no cover
            return

        def do_POST(self) -> None:  # noqa: N802
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"boom")

    with run_server(ErrorHandler) as server:
        provider = OpenAICompatibleProvider(f"http://127.0.0.1:{server.server_port}/v1")
        request = ProviderRequest(model="m", messages=[], tools=[], api_key="k", base_url=provider.base_url)
        with pytest.raises(ProviderError, match="Provider request failed with 500"):
            list(provider.stream_chat(request))
