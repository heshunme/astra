from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from .app import AstraApp, AstraAppOptions
from .models import AgentRunResult, Message
from .session import SessionStore


JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None
AppFactory = Callable[[AstraAppOptions], AstraApp]
SessionStoreFactory = Callable[[], SessionStore]

PROTOCOL_VERSION = 1


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: object | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(slots=True)
class ToolCallState:
    id: str
    name: str
    title: str
    raw_input: object | None = None
    status: str = "pending"


@dataclass(slots=True)
class AcpSession:
    session_id: str
    app: AstraApp
    cwd: Path
    active_request_id: str | int | None = None
    cancel_requested: bool = False
    active_tool_calls: dict[str, ToolCallState] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class JsonRpcStream:
    def __init__(self, input_stream: TextIO, output_stream: TextIO):
        self._input = input_stream
        self._output = output_stream
        self._write_lock = threading.Lock()

    def read_message(self) -> dict[str, Any] | None:
        while True:
            line = self._input.readline()
            if line == "":
                return None
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise JsonRpcError(-32700, f"Invalid JSON: {exc.msg}") from exc
            if not isinstance(payload, dict):
                raise JsonRpcError(-32600, "JSON-RPC payload must be an object")
            return payload

    def send_response(self, request_id: str | int | None, result: dict[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    def send_error(self, request_id: str | int | None, code: int, message: str, data: object | None = None) -> None:
        error: dict[str, object] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write_json({"jsonrpc": "2.0", "id": request_id, "error": error})

    def send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "method": method, "params": params})

    def _write_json(self, payload: dict[str, Any]) -> None:
        with self._write_lock:
            self._output.write(json.dumps(payload, ensure_ascii=False))
            self._output.write("\n")
            self._output.flush()


class AcpServer:
    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        app_factory: AppFactory | None = None,
        session_store_factory: SessionStoreFactory | None = None,
    ):
        self._stream = JsonRpcStream(input_stream or sys.stdin, output_stream or sys.stdout)
        self._app_factory = app_factory or (lambda options: AstraApp(options))
        self._session_store_factory = session_store_factory or SessionStore
        self._session_store = self._session_store_factory()
        self._sessions: dict[str, AcpSession] = {}
        self._initialized = False
        self._client_capabilities: dict[str, Any] = {}
        self._threads: list[threading.Thread] = []

    def serve_forever(self) -> None:
        while True:
            try:
                message = self._stream.read_message()
            except JsonRpcError as exc:
                self._stream.send_error(None, exc.code, exc.message, exc.data)
                continue
            if message is None:
                break
            self.handle_message(message)

    def handle_message(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        if message.get("jsonrpc") != "2.0":
            self._stream.send_error(request_id, -32600, "jsonrpc must be '2.0'")
            return
        if not isinstance(method, str):
            self._stream.send_error(request_id, -32600, "method is required")
            return
        if not isinstance(params, dict):
            self._stream.send_error(request_id, -32602, "params must be an object")
            return

        try:
            if "id" not in message:
                self._handle_notification(method, params)
                return
            if method == "session/prompt":
                worker = threading.Thread(
                    target=self._handle_prompt_request,
                    args=(request_id, params),
                    daemon=True,
                )
                worker.start()
                self._threads.append(worker)
                return
            result = self._dispatch_request(method, params)
        except JsonRpcError as exc:
            self._stream.send_error(request_id, exc.code, exc.message, exc.data)
            return
        except Exception as exc:  # pragma: no cover - defensive JSON-RPC boundary
            self._stream.send_error(request_id, -32000, str(exc))
            return

        self._stream.send_response(request_id, result)

    def _dispatch_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != "initialize" and not self._initialized:
            raise JsonRpcError(-32001, "Connection must be initialized first")
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "session/new":
            return self._handle_new_session(params)
        if method == "session/load":
            return self._handle_load_session(params)
        if method == "session/resume":
            return self._handle_resume_session(params)
        if method == "session/list":
            return self._handle_list_sessions(params)
        if method == "session/close":
            return self._handle_close_session(params)
        if method == "session/set_model":
            return self._handle_set_model(params)
        if method == "session/set_config_option":
            return self._handle_set_config_option(params)
        raise JsonRpcError(-32601, f"Method not found: {method}")

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "session/cancel":
            self._handle_cancel(params)
            return
        raise JsonRpcError(-32601, f"Method not found: {method}")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        protocol_version = params.get("protocolVersion", PROTOCOL_VERSION)
        if not isinstance(protocol_version, int):
            raise JsonRpcError(-32602, "protocolVersion must be an integer")
        self._initialized = True
        client_capabilities = params.get("clientCapabilities", {})
        if isinstance(client_capabilities, dict):
            self._client_capabilities = dict(client_capabilities)
        else:
            self._client_capabilities = {}
        return {
            "protocolVersion": protocol_version if protocol_version == PROTOCOL_VERSION else PROTOCOL_VERSION,
            "agentInfo": {
                "name": "astra-agent",
                "title": "Astra ACP",
                "version": "0.1.0",
            },
            "agentCapabilities": {
                "loadSession": True,
                "mcpCapabilities": {"http": False, "sse": False},
                "promptCapabilities": {"audio": False, "embeddedContext": False, "image": False},
                "sessionCapabilities": {"list": {}},
            },
            "authMethods": [],
        }

    def _handle_new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = self._require_absolute_cwd(params.get("cwd"))
        self._require_list(params.get("mcpServers"), "mcpServers")
        app = self._app_factory(AstraAppOptions(cwd=cwd))
        startup = app.startup()
        if startup.error:
            raise JsonRpcError(-32000, startup.error)
        session = AcpSession(
            session_id=app.session_handle_id(),
            app=app,
            cwd=Path(cwd),
        )
        self._sessions[session.session_id] = session
        result = {
            "sessionId": session.session_id,
            "configOptions": self._config_options_for_app(app),
        }
        self._send_available_commands_update(session)
        return result

    def _handle_load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = self._require_absolute_cwd(params.get("cwd"))
        self._require_list(params.get("mcpServers"), "mcpServers")
        session_id = self._require_string(params.get("sessionId"), "sessionId")
        app = self._app_factory(AstraAppOptions(cwd=cwd, session_id=session_id))
        startup = app.startup()
        if startup.error:
            raise JsonRpcError(-32000, startup.error)
        restored_cwd = Path(app.session_cwd()).resolve()
        if restored_cwd != cwd:
            raise JsonRpcError(
                -32000,
                f"Requested cwd does not match restored session cwd: requested={cwd} restored={restored_cwd}",
            )
        session = AcpSession(
            session_id=app.session_handle_id(),
            app=app,
            cwd=restored_cwd,
        )
        self._sessions[session.session_id] = session
        result = {
            "configOptions": self._config_options_for_app(app),
        }
        self._send_available_commands_update(session)
        self._send_session_info_update(session)
        self._stream_loaded_history(session)
        return result

    def _handle_list_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("cursor") not in (None, ""):
            raise JsonRpcError(-32602, "cursor pagination is not supported")
        cwd_filter = params.get("cwd")
        normalized_cwd = None
        if cwd_filter is not None:
            normalized_cwd = str(self._require_absolute_cwd(cwd_filter))
        sessions = []
        for summary in self._session_store.list():
            if normalized_cwd is not None and str(Path(summary.cwd).resolve()) != normalized_cwd:
                continue
            loaded = self._session_store.load(summary.id)
            sessions.append(
                {
                    "sessionId": loaded.id,
                    "cwd": str(Path(loaded.cwd).resolve()),
                    "title": loaded.name,
                    "updatedAt": loaded.updated_at,
                }
            )
        return {"sessions": sessions, "nextCursor": None}

    def _handle_resume_session(self, params: dict[str, Any]) -> dict[str, Any]:
        # Astra restores persisted session state through the same path as load.
        return self._handle_load_session(params)

    def _handle_close_session(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = self._require_string(params.get("sessionId"), "sessionId")
        session = self._sessions.get(session_id)
        if session is None:
            return {}
        with session.lock:
            if session.active_request_id is not None:
                raise JsonRpcError(-32002, "Session is busy processing a prompt")
        self._sessions.pop(session_id, None)
        return {}

    def _handle_set_model(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params.get("sessionId"))
        model_id = self._require_string(params.get("modelId"), "modelId")
        config_options = self._apply_config_option(session, config_id="model", value=model_id)
        self._send_config_options_update(session)
        return {"configOptions": config_options}

    def _handle_set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params.get("sessionId"))
        config_id = self._require_string(params.get("configId"), "configId")
        value = self._require_string(params.get("value"), "value")
        config_options = self._apply_config_option(session, config_id=config_id, value=value)
        self._send_config_options_update(session)
        return {"configOptions": config_options}

    def _apply_config_option(self, session: AcpSession, *, config_id: str, value: str) -> list[dict[str, Any]]:
        with session.lock:
            if session.active_request_id is not None:
                raise JsonRpcError(-32002, "Session is busy processing a prompt")
            if config_id == "model":
                session.app.set_model(value)
            elif config_id == "base_url":
                session.app.set_base_url(value)
            else:
                raise JsonRpcError(-32602, f"Unsupported config option: {config_id}")
            return self._config_options_for_app(session.app)

    def _handle_cancel(self, params: dict[str, Any]) -> None:
        session = self._require_session(params.get("sessionId"))
        with session.lock:
            session.cancel_requested = True
            for tool_call in list(session.active_tool_calls.values()):
                if tool_call.status in {"completed", "failed", "cancelled"}:
                    continue
                tool_call.status = "cancelled"
                self._notify_session_update(
                    session.session_id,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_call.id,
                        "status": "cancelled",
                        "title": tool_call.title,
                    },
                )
        session.app.abort()

    def _handle_prompt_request(self, request_id: str | int | None, params: dict[str, Any]) -> None:
        try:
            session = self._require_session(params.get("sessionId"))
            prompt = params.get("prompt")
            if not isinstance(prompt, list):
                raise JsonRpcError(-32602, "prompt must be an array")
            with session.lock:
                if session.active_request_id is not None:
                    raise JsonRpcError(-32002, "Session is already processing a prompt")
                session.active_request_id = request_id
                session.cancel_requested = False
                session.active_tool_calls.clear()
            prompt_text = self._prompt_blocks_to_text(prompt)
            stop_reason = self._run_prompt_turn(session, prompt_text)
            self._stream.send_response(request_id, {"stopReason": stop_reason})
        except JsonRpcError as exc:
            self._stream.send_error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive JSON-RPC boundary
            self._stream.send_error(request_id, -32000, str(exc))
        finally:
            if "session" in locals():
                with session.lock:
                    session.active_request_id = None
                    session.cancel_requested = False
                    session.active_tool_calls.clear()

    def _run_prompt_turn(self, session: AcpSession, prompt_text: str) -> str:
        text = prompt_text.strip()
        if not text:
            self._emit_agent_text(session.session_id, "(empty prompt)")
            return "end_turn"
        if text.startswith("/"):
            return self._run_command(session, text)

        def on_event(event_type: str, payload: dict[str, object]) -> None:
            self._handle_agent_event(session, event_type, payload)

        self._notify_session_update(
            session.session_id,
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )
        result = session.app.submit_prompt(text, on_event=on_event)
        return self._finalize_prompt_result(session, result)

    def _run_command(self, session: AcpSession, command_text: str) -> str:
        if command_text == "/help":
            body = "\n".join(entry.usage for entry in session.app.help_entries())
            self._emit_agent_text(session.session_id, body)
            return "end_turn"

        if command_text == "/skills":
            skills = session.app.get_skills()
            if not skills:
                self._emit_agent_text(session.session_id, "No skills available.")
                return "end_turn"
            lines = [f"- {entry.name}: {entry.summary}" for entry in skills]
            self._emit_agent_text(session.session_id, "\n".join(lines))
            return "end_turn"

        if command_text == "/templates":
            templates = session.app.get_templates()
            if not templates:
                self._emit_agent_text(session.session_id, "No templates available.")
                return "end_turn"
            self._emit_agent_text(session.session_id, "\n".join(f"- {name}" for name in templates))
            return "end_turn"

        if command_text == "/runtime":
            self._emit_agent_text(
                session.session_id,
                json.dumps(session.app.get_runtime_summary(), ensure_ascii=False, indent=2),
            )
            return "end_turn"

        if command_text == "/reload":
            result = session.app.reload_runtime()
            lines = [result.message]
            lines.extend(f"warning: {warning}" for warning in result.warnings)
            self._emit_agent_text(session.session_id, "\n".join(lines))
            self._send_config_options_update(session)
            self._send_available_commands_update(session)
            return "end_turn"

        if command_text.startswith("/model"):
            _name, _, remainder = command_text.partition(" ")
            if not remainder.strip():
                self._emit_agent_text(session.session_id, session.app.get_model())
                return "end_turn"
            result = session.app.set_model(remainder.strip())
            self._emit_agent_text(session.session_id, result.message)
            self._send_config_options_update(session)
            return "end_turn"

        if command_text.startswith("/base-url"):
            _name, _, remainder = command_text.partition(" ")
            if not remainder.strip():
                self._emit_agent_text(session.session_id, session.app.get_base_url())
                return "end_turn"
            result = session.app.set_base_url(remainder.strip())
            self._emit_agent_text(session.session_id, result.message)
            self._send_config_options_update(session)
            return "end_turn"

        if command_text.startswith("/skill:"):
            remainder = command_text[len("/skill:") :].strip()
            name, _, request_text = remainder.partition(" ")
            if not name:
                self._emit_agent_text(session.session_id, "Usage: /skill:<name> [request]")
                return "end_turn"
            if not request_text.strip():
                result = session.app.arm_skill(name)
                self._emit_agent_text(session.session_id, result.message)
                return "end_turn"

            def on_event(event_type: str, payload: dict[str, object]) -> None:
                self._handle_agent_event(session, event_type, payload)

            result = session.app.run_skill(name, request_text.strip(), on_event=on_event)
            return self._finalize_prompt_result(session, result)

        if command_text.startswith("/template:"):
            remainder = command_text[len("/template:") :].strip()
            name, _, request_text = remainder.partition(" ")
            if not name or not request_text.strip():
                self._emit_agent_text(session.session_id, "Usage: /template:<name> <request>")
                return "end_turn"

            def on_event(event_type: str, payload: dict[str, object]) -> None:
                self._handle_agent_event(session, event_type, payload)

            result = session.app.run_template(name, request_text.strip(), on_event=on_event)
            return self._finalize_prompt_result(session, result)

        self._emit_agent_text(session.session_id, f"Unknown command: {command_text}")
        return "end_turn"

    def _finalize_prompt_result(self, session: AcpSession, result: AgentRunResult) -> str:
        if result.error:
            if session.cancel_requested or result.error == "Request aborted":
                return "cancelled"
            self._emit_agent_text(session.session_id, result.error)
        self._send_session_info_update(session)
        self._send_available_commands_update(session)
        return "cancelled" if session.cancel_requested else "end_turn"

    def _handle_agent_event(self, session: AcpSession, event_type: str, payload: dict[str, object]) -> None:
        if event_type == "text_delta":
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                self._notify_session_update(
                    session.session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": delta},
                    },
                )
            return

        if event_type == "tool_call":
            tool_call_id = str(payload.get("id", ""))
            name = str(payload.get("name", "tool"))
            arguments = self._decode_json_object(payload.get("arguments"))
            title = self._tool_title(name)
            state = ToolCallState(
                id=tool_call_id,
                name=name,
                title=title,
                raw_input=arguments,
                status="pending",
            )
            session.active_tool_calls[tool_call_id] = state
            update = {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
                "title": title,
                "kind": self._tool_kind(name),
                "status": "pending",
                "rawInput": arguments,
            }
            locations = self._tool_locations(name, arguments, session.cwd)
            if locations:
                update["locations"] = locations
            self._notify_session_update(session.session_id, update)
            self._notify_session_update(
                session.session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "status": "in_progress",
                    "title": title,
                },
            )
            state.status = "in_progress"
            return

        if event_type == "tool_result":
            tool_call_id = str(payload.get("id", ""))
            content = payload.get("content")
            rendered = content if isinstance(content, str) else ""
            state = session.active_tool_calls.get(tool_call_id)
            status = "failed" if rendered.startswith("ERROR\n") else "completed"
            if session.cancel_requested and status != "completed":
                status = "cancelled"
            update: dict[str, Any] = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": status,
                "rawOutput": {"text": rendered},
            }
            if state is not None:
                update["title"] = state.title
                update["kind"] = self._tool_kind(state.name)
            if rendered:
                update["content"] = [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": rendered},
                    }
                ]
            self._notify_session_update(session.session_id, update)
            if state is not None:
                state.status = status

    def _stream_loaded_history(self, session: AcpSession) -> None:
        agent = session.app.agent
        if agent is None:
            return
        for message in agent.messages:
            self._stream_message_history(session.session_id, message)

    def _stream_message_history(self, session_id: str, message: Message) -> None:
        if message.role == "user" and message.content:
            self._notify_session_update(
                session_id,
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": message.content},
                },
            )
            return
        if message.role == "assistant" and message.content:
            self._notify_session_update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": message.content},
                },
            )

    def _send_config_options_update(self, session: AcpSession) -> None:
        self._notify_session_update(
            session.session_id,
            {
                "sessionUpdate": "config_option_update",
                "configOptions": self._config_options_for_app(session.app),
            },
        )

    def _send_available_commands_update(self, session: AcpSession) -> None:
        self._notify_session_update(
            session.session_id,
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": self._available_commands_for_app(session.app),
            },
        )

    def _send_session_info_update(self, session: AcpSession) -> None:
        self._notify_session_update(
            session.session_id,
            {
                "sessionUpdate": "session_info_update",
                "title": session.app.current_session_name(),
                "updatedAt": session.app.session_updated_at(),
            },
        )

    def _notify_session_update(self, session_id: str, update: dict[str, Any]) -> None:
        self._stream.send_notification(
            "session/update",
            {
                "sessionId": session_id,
                "update": update,
            },
        )

    def _emit_agent_text(self, session_id: str, text: str) -> None:
        self._notify_session_update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )

    def _config_options_for_app(self, app: AstraApp) -> list[dict[str, Any]]:
        model_options = self._select_options(
            current=app.get_model(),
            defaults=[
                "gpt-5.2",
                "openai/gpt-5",
                "openai/gpt-5-mini",
            ],
        )
        base_url_options = self._select_options(
            current=app.get_base_url(),
            defaults=[
                "https://api.openai.com/v1",
            ],
        )
        return [
            {
                "id": "model",
                "name": "Model",
                "description": "Model used for subsequent prompt turns.",
                "category": "model",
                "type": "select",
                "currentValue": app.get_model(),
                "options": model_options,
            },
            {
                "id": "base_url",
                "name": "Base URL",
                "description": "Provider API base URL used by the current session.",
                "category": "other",
                "type": "select",
                "currentValue": app.get_base_url(),
                "options": base_url_options,
            },
        ]

    def _available_commands_for_app(self, app: AstraApp) -> list[dict[str, Any]]:
        commands = [
            {"name": "/help", "description": "Show built-in command help."},
            {"name": "/reload", "description": "Reload runtime configuration from env and YAML."},
            {
                "name": "/model",
                "description": "Show or set the active model.",
                "input": {"hint": "[name]"},
            },
            {
                "name": "/base-url",
                "description": "Show or set the active base URL.",
                "input": {"hint": "[url]"},
            },
            {"name": "/skills", "description": "List available skills."},
            {"name": "/templates", "description": "List available templates."},
            {"name": "/runtime", "description": "Show runtime summary as JSON."},
        ]
        for entry in app.get_skills():
            commands.append(
                {
                    "name": f"/skill:{entry.name}",
                    "description": f"{entry.summary} Leave the input empty to arm it for the next turn.",
                    "input": {"hint": "[request]"},
                }
            )
        for name in app.get_templates():
            commands.append(
                {
                    "name": f"/template:{name}",
                    "description": "Apply this template to one request.",
                    "input": {"hint": "<request>"},
                }
            )
        return commands

    def _prompt_blocks_to_text(self, prompt: list[Any]) -> str:
        parts: list[str] = []
        for block in prompt:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
                continue
            if block_type == "resource_link":
                parts.append(self._resource_link_to_text(block))
                continue
            if block_type == "resource":
                resource = block.get("resource")
                if isinstance(resource, dict):
                    parts.append(self._resource_block_to_text(resource))
                continue
            if "text" in block and isinstance(block.get("text"), str):
                parts.append(str(block["text"]))
                continue
            if "uri" in block and isinstance(block.get("uri"), str):
                parts.append(self._resource_link_to_text(block))
        return "\n\n".join(part for part in parts if part)

    def _resource_link_to_text(self, resource: Mapping[str, Any]) -> str:
        name = resource.get("name") or resource.get("title") or resource.get("uri") or "resource"
        uri = resource.get("uri")
        description = resource.get("description")
        parts = [f"Resource link: {name}"]
        if isinstance(uri, str):
            parts.append(f"URI: {uri}")
        if isinstance(description, str) and description:
            parts.append(description)
        return "\n".join(parts)

    def _resource_block_to_text(self, resource: Mapping[str, Any]) -> str:
        header = self._resource_link_to_text(resource)
        text = resource.get("text")
        if isinstance(text, str) and text:
            return f"{header}\n\n{text}"
        return header

    def _tool_title(self, name: str) -> str:
        return {
            "read": "Reading files",
            "write": "Writing files",
            "edit": "Editing files",
            "grep": "Searching file contents",
            "find": "Finding files",
            "ls": "Listing workspace files",
            "bash": "Running shell command",
        }.get(name, f"Running {name}")

    def _tool_kind(self, name: str) -> str:
        if name == "read":
            return "read"
        if name in {"write", "edit"}:
            return "edit"
        if name in {"grep", "find", "ls"}:
            return "search"
        if name == "bash":
            return "execute"
        return "other"

    def _tool_locations(self, name: str, raw_input: object, cwd: Path) -> list[dict[str, Any]]:
        if not isinstance(raw_input, dict):
            return []
        raw_path = raw_input.get("path")
        if not isinstance(raw_path, str):
            return []
        try:
            path = Path(raw_path)
            if not path.is_absolute():
                path = cwd / path
            resolved = path.resolve()
        except OSError:
            return []
        return [{"path": str(resolved)}]

    def _decode_json_object(self, value: object) -> object | None:
        if not isinstance(value, str):
            return value
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded

    def _select_options(self, *, current: str, defaults: list[str]) -> list[dict[str, str]]:
        values: list[str] = []
        for value in [current, *defaults]:
            if value not in values:
                values.append(value)
        return [{"value": value, "name": value} for value in values]

    def _require_session(self, session_id: object) -> AcpSession:
        session_key = self._require_string(session_id, "sessionId")
        session = self._sessions.get(session_key)
        if session is None:
            raise JsonRpcError(-32004, f"Unknown session: {session_key}")
        return session

    def _require_absolute_cwd(self, cwd: object) -> Path:
        raw = self._require_string(cwd, "cwd")
        path = Path(raw)
        if not path.is_absolute():
            raise JsonRpcError(-32602, "cwd must be an absolute path")
        return path.resolve()

    def _require_list(self, value: object, field_name: str) -> list[Any]:
        if not isinstance(value, list):
            raise JsonRpcError(-32602, f"{field_name} must be an array")
        return value

    def _require_string(self, value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise JsonRpcError(-32602, f"{field_name} must be a non-empty string")
        return value.strip()


def main() -> None:
    AcpServer().serve_forever()


if __name__ == "__main__":
    main()
