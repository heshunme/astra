from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from astra.acp import AcpServer
from astra.app import AstraApp, AstraAppOptions
from astra.config import CapabilitiesConfig, PromptRuntimeConfig, ReloadResult, ResolvedRuntimeConfig, RuntimeConfig, ToolRuntimeConfig
from astra.models import AgentConversationState, AgentRunResult, AgentRuntimeState, AgentSnapshot, Message
from astra.runtime.runtime import PromptInspection, PromptInspectionFragment
from astra.session import SessionStore


pytestmark = pytest.mark.unit


class FakeConfigManager:
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self._config = config or RuntimeConfig()

    def load(self, cwd: Path) -> RuntimeConfig:
        return self._config


class FakeCapabilityRuntime:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.template_names = ["repo-rules"]

    def list_template_names(self) -> list[str]:
        return list(self.template_names)


class FakeSkill:
    def __init__(self, name: str, summary: str):
        self.name = name
        self.summary = summary


class EventfulFakeAgent:
    def __init__(self, config, runtime) -> None:
        self.config = config
        self.runtime = runtime
        self.runtime_state = AgentRuntimeState(cwd=str(config.cwd), runtime_config=_runtime_config(config))
        self.messages: list[Message] = []
        self.is_streaming = False
        self.abort_requested = False
        self.block_on_prompt = False
        self.prompt_started = threading.Event()
        self.release_prompt = threading.Event()
        self.snapshot_value = AgentSnapshot(
            conversation=AgentConversationState(messages=[]),
            runtime=AgentRuntimeState(cwd=str(config.cwd), runtime_config=_runtime_config(config)),
        )

    @property
    def runtime_config(self):
        return self.runtime_state.runtime_config

    def subscribe(self, _callback):
        return lambda: None

    def abort(self) -> None:
        self.abort_requested = True
        self.is_streaming = False
        self.release_prompt.set()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        return not self.is_streaming

    def apply_runtime_config(self, runtime_config):
        self.runtime_state.runtime_config = runtime_config
        self.config.model = runtime_config.model
        self.config.base_url = runtime_config.base_url
        self.config.system_prompt = runtime_config.system_prompt
        self.snapshot_value.runtime.runtime_config = runtime_config
        self.snapshot_value.runtime.cwd = self.runtime_state.cwd
        return ReloadResult(
            success=True,
            message="Reloaded runtime configuration.",
            applied_model=runtime_config.model,
            applied_base_url=runtime_config.base_url,
            enabled_tools=list(runtime_config.tools.enabled_tools),
        )

    def set_model(self, model: str) -> None:
        self.config.model = model
        self.runtime_state.runtime_config.model = model

    def set_base_url(self, base_url: str) -> None:
        self.config.base_url = base_url
        self.runtime_state.runtime_config.base_url = base_url

    def inspect_runtime(self) -> dict[str, object]:
        return {
            "model": self.config.model,
            "base_url": self.config.base_url,
            "tools": ["read", "write", "bash"],
            "prompts": {"order": ["builtin:base"], "available": ["builtin:base"], "loaded": []},
            "skills": {"available": ["review"], "history_only": [], "pending": None, "loaded": [], "entries": [], "conflicts": []},
            "templates": {"available": self.runtime.list_template_names()},
            "tool_defaults": {"read_max_lines": 400, "bash_timeout_seconds": 60, "bash_max_output_bytes": 32768},
            "warnings": [],
        }

    def inspect_prompt(self) -> PromptInspection:
        return PromptInspection(
            assembled="assembled prompt",
            fragments=[PromptInspectionFragment(key="builtin:base", source="builtin", text_length=16)],
        )

    def prompt_fragment_text(self, key: str) -> str:
        return "assembled prompt" if key == "builtin:base" else ""

    def prompt(self, text: str, *, raw_input: str | None = None, metadata=None, on_event=None) -> AgentRunResult:
        self.messages.append(Message(role="user", content=text, metadata=metadata or {}))
        self.snapshot_value.conversation.messages = list(self.messages)
        self.is_streaming = True
        self.prompt_started.set()
        if on_event is not None:
            on_event("text_delta", {"delta": "Working..."})
            on_event("tool_call", {"id": "tool-1", "name": "read", "arguments": '{"path":"src/demo.py"}'})
        if self.block_on_prompt:
            self.release_prompt.wait(timeout=2)
        self.is_streaming = False
        if self.abort_requested:
            return AgentRunResult(assistant_messages=[], tool_results=[], error="Request aborted")
        if on_event is not None:
            on_event("tool_result", {"id": "tool-1", "name": "read", "content": "OK\n1: print('demo')"})
            on_event("text_delta", {"delta": "Done."})
        assistant = Message(role="assistant", content="Working...Done.")
        self.messages.append(assistant)
        self.snapshot_value.conversation.messages = list(self.messages)
        return AgentRunResult(assistant_messages=[assistant], tool_results=[], error=None)

    def arm_skill(self, name: str, raw_command: str):
        return True, f"Next message will use skill: {name}"

    def run_skill(self, name: str, request_text: str, raw_command: str, *, on_event=None):
        return self.prompt(f"skill:{name}:{request_text}", raw_input=raw_command, on_event=on_event)

    def run_template(self, name: str, request_text: str, raw_command: str, *, on_event=None):
        return self.prompt(f"template:{name}:{request_text}", raw_input=raw_command, on_event=on_event)

    def available_skills(self) -> list[object]:
        return [FakeSkill("review", "Review code for issues.")]

    def snapshot(self) -> AgentSnapshot:
        self.snapshot_value.runtime.cwd = self.runtime_state.cwd
        self.snapshot_value.runtime.runtime_config = self.runtime_state.runtime_config
        self.snapshot_value.conversation.messages = list(self.messages)
        return self.snapshot_value

    def restore(self, snapshot: AgentSnapshot) -> None:
        self.snapshot_value = snapshot
        self.runtime_state = snapshot.runtime
        self.messages = list(snapshot.conversation.messages)
        self.config.model = snapshot.runtime.runtime_config.model
        self.config.base_url = snapshot.runtime.runtime_config.base_url
        self.config.system_prompt = snapshot.runtime.runtime_config.system_prompt


def _runtime_config(config) -> ResolvedRuntimeConfig:
    return ResolvedRuntimeConfig(
        model=config.model,
        base_url=config.base_url,
        system_prompt=config.system_prompt,
        tools=ToolRuntimeConfig(
            enabled_tools=["read", "write", "bash"],
            read_max_lines=400,
            bash_timeout_seconds=60,
            bash_max_output_bytes=32768,
        ),
        prompts=PromptRuntimeConfig(order=["builtin:base"]),
        capabilities=CapabilitiesConfig(),
    )


def _app_factory(store_factory, config_manager_factory):
    def factory(options: AstraAppOptions) -> AstraApp:
        return AstraApp(
            options,
            agent_factory=EventfulFakeAgent,
            runtime_factory=FakeCapabilityRuntime,
            session_store_factory=store_factory,
            config_manager_factory=config_manager_factory,
            env_provider=lambda: {"OPENAI_API_KEY": "test-key"},
        )

    return factory


def _drain_output(stream: io.StringIO, consumed: int) -> tuple[list[dict[str, object]], int]:
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines[consumed:]], len(lines)


def _initialize(server: AcpServer) -> None:
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
    )


def _create_session(server: AcpServer, cwd: Path) -> str:
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {"cwd": str(cwd), "mcpServers": []},
        }
    )
    for session_id in server._sessions:  # noqa: SLF001 - test-only inspection
        return session_id
    raise AssertionError("session was not created")


def test_initialize_and_new_session_advertise_capabilities(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    _initialize(server)
    _create_session(server, workspace)

    messages, _ = _drain_output(output, 0)
    init_response = next(message for message in messages if message.get("id") == 0)
    new_response = next(message for message in messages if message.get("id") == 1)
    commands_update = next(
        message
        for message in messages
        if message.get("method") == "session/update"
        and message["params"]["update"]["sessionUpdate"] == "available_commands_update"
    )

    assert init_response["result"]["protocolVersion"] == 1
    assert init_response["result"]["agentCapabilities"]["loadSession"] is True
    assert init_response["result"]["agentCapabilities"]["sessionCapabilities"] == {"list": {}}
    assert new_response["result"]["sessionId"]
    assert any(option["id"] == "model" for option in new_response["result"]["configOptions"])
    assert commands_update["method"] == "session/update"
    assert commands_update["params"]["update"]["sessionUpdate"] == "available_commands_update"
    command_names = [entry["name"] for entry in commands_update["params"]["update"]["availableCommands"]]
    assert "help" in command_names
    assert "reload" in command_names
    assert "model" in command_names
    assert "base-url" in command_names
    assert "skills" in command_names
    assert "templates" in command_names
    assert "runtime" in command_names
    assert "skill:review" in command_names
    assert "template:repo-rules" in command_names
    assert all(not name.startswith("/") for name in command_names)


def test_prompt_streams_updates_and_returns_stop_reason(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    _initialize(server)
    session_id = _create_session(server, workspace)
    consumed = len([line for line in output.getvalue().splitlines() if line.strip()])

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Inspect the repo"}],
            },
        }
    )
    server._threads[-1].join(timeout=2)  # noqa: SLF001 - test-only inspection

    messages, _ = _drain_output(output, consumed)
    updates = [message for message in messages if message.get("method") == "session/update"]
    response = next(message for message in messages if message.get("id") == 2)

    assert response["result"]["stopReason"] == "end_turn"
    assert any(update["params"]["update"]["sessionUpdate"] == "agent_message_chunk" for update in updates)
    assert any(update["params"]["update"]["sessionUpdate"] == "tool_call" for update in updates)
    assert any(
        update["params"]["update"]["sessionUpdate"] == "tool_call_update"
        and update["params"]["update"]["status"] == "completed"
        for update in updates
    )
    assert not any(update["params"]["update"]["sessionUpdate"] == "user_message_chunk" for update in updates)


def test_cancel_turn_returns_cancelled_stop_reason(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    _initialize(server)
    session_id = _create_session(server, workspace)
    session = server._sessions[session_id]  # noqa: SLF001 - test-only inspection
    assert session.app.agent is not None
    session.app.agent.block_on_prompt = True
    consumed = len([line for line in output.getvalue().splitlines() if line.strip()])

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Long running task"}],
            },
        }
    )
    assert session.app.agent.prompt_started.wait(timeout=1)

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": session_id},
        }
    )
    server._threads[-1].join(timeout=2)  # noqa: SLF001 - test-only inspection

    messages, _ = _drain_output(output, consumed)
    updates = [message for message in messages if message.get("method") == "session/update"]
    response = next(message for message in messages if message.get("id") == 3)

    assert response["result"]["stopReason"] == "cancelled"
    assert any(
        update["params"]["update"]["sessionUpdate"] == "tool_call_update"
        and update["params"]["update"]["status"] == "cancelled"
        for update in updates
    )


def test_list_load_and_set_config_option_use_persisted_sessions(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    _initialize(server)
    session_id = _create_session(server, workspace)
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Persist this session"}],
            },
        }
    )
    server._threads[-1].join(timeout=2)  # noqa: SLF001 - test-only inspection
    consumed = len([line for line in output.getvalue().splitlines() if line.strip()])

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "session/list",
            "params": {"cwd": str(workspace)},
        }
    )
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "session/load",
            "params": {"sessionId": session_id, "cwd": str(workspace), "mcpServers": []},
        }
    )
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/set_config_option",
            "params": {"sessionId": session_id, "configId": "model", "value": "openai/gpt-5"},
        }
    )

    messages, _ = _drain_output(output, consumed)
    list_response = next(message for message in messages if message.get("id") == 5)
    load_response = next(message for message in messages if message.get("id") == 6)
    set_config_response = next(message for message in messages if message.get("id") == 7)

    assert list_response["result"]["sessions"][0]["sessionId"] == session_id
    assert "configOptions" in load_response["result"]
    assert any(option["currentValue"] == "openai/gpt-5" for option in set_config_response["result"]["configOptions"])


def test_session_load_rejects_cross_workspace_restore_and_does_not_register_session(tmp_path: Path) -> None:
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    workspace_a = (tmp_path / "workspace-a").resolve()
    workspace_b = (tmp_path / "workspace-b").resolve()
    workspace_a.mkdir()
    workspace_b.mkdir()

    first_output = io.StringIO()
    first_server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=first_output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    _initialize(first_server)
    session_id = _create_session(first_server, workspace_b)
    first_server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Persist in workspace B"}],
            },
        }
    )
    first_server._threads[-1].join(timeout=2)  # noqa: SLF001 - test-only inspection

    second_output = io.StringIO()
    second_server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=second_output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    _initialize(second_server)
    consumed = len([line for line in second_output.getvalue().splitlines() if line.strip()])

    second_server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/load",
            "params": {"sessionId": session_id, "cwd": str(workspace_a), "mcpServers": []},
        }
    )

    messages, _ = _drain_output(second_output, consumed)
    error_response = next(message for message in messages if message.get("id") == 9)

    assert "error" in error_response
    assert "requested=" in error_response["error"]["message"]
    assert "restored=" in error_response["error"]["message"]
    assert second_server._sessions == {}  # noqa: SLF001 - test-only inspection
    assert not any(message.get("method") == "session/update" for message in messages)


def test_set_config_option_updates_model_and_base_url_when_session_is_idle(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    _initialize(server)
    session_id = _create_session(server, workspace)
    consumed = len([line for line in output.getvalue().splitlines() if line.strip()])

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "session/set_config_option",
            "params": {"sessionId": session_id, "configId": "model", "value": "openai/gpt-5"},
        }
    )
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "session/set_config_option",
            "params": {"sessionId": session_id, "configId": "base_url", "value": "http://gateway/v1"},
        }
    )

    messages, _ = _drain_output(output, consumed)
    model_response = next(message for message in messages if message.get("id") == 10)
    base_url_response = next(message for message in messages if message.get("id") == 11)

    assert any(option["currentValue"] == "openai/gpt-5" for option in model_response["result"]["configOptions"])
    assert any(option["currentValue"] == "http://gateway/v1" for option in base_url_response["result"]["configOptions"])


def test_set_model_alias_updates_model_and_emits_config_update(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    _initialize(server)
    session_id = _create_session(server, workspace)
    consumed = len([line for line in output.getvalue().splitlines() if line.strip()])

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "session/set_model",
            "params": {"sessionId": session_id, "modelId": "openai/gpt-5-mini"},
        }
    )

    messages, _ = _drain_output(output, consumed)
    response = next(message for message in messages if message.get("id") == 14)
    update = next(
        message
        for message in messages
        if message.get("method") == "session/update"
        and message["params"]["update"]["sessionUpdate"] == "config_option_update"
    )

    assert any(option["currentValue"] == "openai/gpt-5-mini" for option in response["result"]["configOptions"])
    assert any(
        option["currentValue"] == "openai/gpt-5-mini"
        for option in update["params"]["update"]["configOptions"]
    )


def test_resume_and_close_session_aliases_work_for_active_sessions(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    _initialize(server)
    session_id = _create_session(server, workspace)
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Persist this session"}],
            },
        }
    )
    server._threads[-1].join(timeout=2)  # noqa: SLF001 - test-only inspection
    consumed = len([line for line in output.getvalue().splitlines() if line.strip()])

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 16,
            "method": "session/resume",
            "params": {"sessionId": session_id, "cwd": str(workspace), "mcpServers": []},
        }
    )
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 17,
            "method": "session/close",
            "params": {"sessionId": session_id},
        }
    )

    messages, _ = _drain_output(output, consumed)
    resume_response = next(message for message in messages if message.get("id") == 16)
    close_response = next(message for message in messages if message.get("id") == 17)

    assert "configOptions" in resume_response["result"]
    assert close_response["result"] == {}
    assert session_id not in server._sessions  # noqa: SLF001 - test-only inspection


def test_set_config_option_is_rejected_once_prompt_turn_has_claimed_the_session(tmp_path: Path) -> None:
    output = io.StringIO()
    store_factory = lambda: SessionStore(base_dir=tmp_path / "sessions")
    config_manager_factory = lambda: FakeConfigManager()
    server = AcpServer(
        input_stream=io.StringIO(),
        output_stream=output,
        app_factory=_app_factory(store_factory, config_manager_factory),
        session_store_factory=store_factory,
    )
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    _initialize(server)
    session_id = _create_session(server, workspace)
    session = server._sessions[session_id]  # noqa: SLF001 - test-only inspection
    assert session.app.agent is not None
    entered_turn = threading.Event()
    release_turn = threading.Event()
    original_run_prompt_turn = server._run_prompt_turn

    def wrapped_run_prompt_turn(session_obj, prompt_text):
        entered_turn.set()
        release_turn.wait(timeout=2)
        return original_run_prompt_turn(session_obj, prompt_text)

    server._run_prompt_turn = wrapped_run_prompt_turn  # type: ignore[assignment]
    consumed = len([line for line in output.getvalue().splitlines() if line.strip()])

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "Hold the turn open"}],
            },
        }
    )
    assert entered_turn.wait(timeout=1)

    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "session/set_config_option",
            "params": {"sessionId": session_id, "configId": "model", "value": "openai/gpt-5"},
        }
    )
    release_turn.set()
    server._threads[-1].join(timeout=2)  # noqa: SLF001 - test-only inspection

    messages, _ = _drain_output(output, consumed)
    busy_response = next(message for message in messages if message.get("id") == 13)
    prompt_response = next(message for message in messages if message.get("id") == 12)

    assert busy_response["error"]["code"] == -32002
    assert prompt_response["result"]["stopReason"] == "end_turn"
    assert session.app.get_model() == "gpt-5.2"
