from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .config import ReloadResult, ResolvedRuntimeConfig, ToolRuntimeConfig, clone_resolved_runtime_config
from .models import AgentRunResult, Message, ToolCall, ToolContext
from .provider import OpenAICompatibleProvider, ProviderAborted, ProviderRequest
from .runtime import CapabilityRuntime
from .session import SessionStore
from .tools import execute_tool, format_tool_result


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


EventCallback = Callable[[str, dict[str, object]], None]


@dataclass(slots=True)
class SessionPromptState:
    skills: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentConfig:
    model: str
    api_key: str
    base_url: str
    cwd: Path
    system_prompt: str


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        capability_runtime: CapabilityRuntime,
        session_store: SessionStore | None = None,
    ):
        self.config = config
        self.runtime = capability_runtime
        self.provider = OpenAICompatibleProvider(config.base_url)
        self.runtime_config = ResolvedRuntimeConfig(
            model=config.model,
            base_url=config.base_url,
            system_prompt=config.system_prompt,
            tools=ToolRuntimeConfig(),
        )
        self.tools: dict[str, object] = {}
        self.current_system_prompt = ""
        self.session_store = session_store or SessionStore()
        self.session = self.session_store.create(str(config.cwd), config.model, config.system_prompt)
        self._session_prompt_states: dict[str, SessionPromptState] = {self.session.id: SessionPromptState()}
        self.is_streaming = False
        self.pending_tool_calls: set[str] = set()
        self.error: str | None = None

    @property
    def active_skills(self) -> list[str]:
        return list(self._session_prompt_state().skills)

    @property
    def active_templates(self) -> list[str]:
        return list(self._session_prompt_state().templates)

    def load_session(self, session_id: str) -> None:
        self.session = self.session_store.load(session_id)
        self.config.model = self.session.model
        self.config.system_prompt = self.session.system_prompt
        self.config.cwd = Path(self.session.cwd)
        self._ensure_session_prompt_state()
        self._refresh_system_prompt()

    def save_session(self) -> None:
        self.session.model = self.config.model
        self.session.system_prompt = self.config.system_prompt
        self.session.cwd = str(self.config.cwd)
        self.session_store.save(self.session)

    def fork_session(self, name: str | None = None) -> str:
        prior_state = self._session_prompt_state()
        forked = self.session_store.fork(self.session.id, name=name)
        self.session = forked
        self._session_prompt_states[forked.id] = SessionPromptState(
            skills=list(prior_state.skills),
            templates=list(prior_state.templates),
        )
        self._refresh_system_prompt()
        return forked.id

    def abort(self) -> None:
        self.provider.close_active_stream()

    def activate_skill(self, name: str) -> tuple[bool, str]:
        if not self.runtime.has_skill(name):
            return False, f"Unknown skill: {name}"
        prompt_state = self._session_prompt_state()
        if name not in prompt_state.skills:
            prompt_state.skills.append(name)
            self._refresh_system_prompt()
        return True, f"Activated skill: {name}"

    def activate_template(self, name: str) -> tuple[bool, str]:
        if not self.runtime.has_template(name):
            return False, f"Unknown template: {name}"
        prompt_state = self._session_prompt_state()
        if name not in prompt_state.templates:
            prompt_state.templates.append(name)
            self._refresh_system_prompt()
        return True, f"Activated template: {name}"

    def reload_runtime(self, runtime_config: ResolvedRuntimeConfig) -> ReloadResult:
        if self.is_streaming:
            return ReloadResult(
                success=False,
                message="Cannot reload while a response is streaming.",
                applied_model=self.config.model,
                applied_base_url=self.config.base_url,
                enabled_tools=list(self.tools),
            )
        try:
            snapshot = self.runtime.reload(runtime_config)
        except Exception as exc:
            return ReloadResult(
                success=False,
                message=str(exc),
                applied_model=self.config.model,
                applied_base_url=self.config.base_url,
                enabled_tools=list(self.tools),
            )

        self.runtime_config = clone_resolved_runtime_config(runtime_config)
        self.config.model = self.runtime_config.model
        self.config.base_url = self.runtime_config.base_url
        self.config.system_prompt = self.runtime_config.system_prompt
        self.provider = OpenAICompatibleProvider(self.config.base_url)
        self.tools = snapshot.tools
        self._refresh_system_prompt()
        self.save_session()
        return ReloadResult(
            success=True,
            message="Reloaded runtime configuration.",
            applied_model=self.config.model,
            applied_base_url=self.config.base_url,
            enabled_tools=list(self.tools),
            loaded_prompts=list(snapshot.diagnostics.loaded_prompts),
            loaded_skills=list(snapshot.diagnostics.loaded_skills),
            warnings=list(snapshot.diagnostics.warnings),
        )

    def prompt(self, text: str, on_event: EventCallback | None = None) -> AgentRunResult:
        self.session.messages.append(Message(role="user", content=text, created_at=utc_now()))
        return self._run(on_event)

    def continue_from_context(self, on_event: EventCallback | None = None) -> AgentRunResult:
        if not self.session.messages:
            raise RuntimeError("No messages to continue from")
        if self.session.messages[-1].role == "assistant":
            raise RuntimeError("Cannot continue from assistant message")
        return self._run(on_event)

    def _emit(self, on_event: EventCallback | None, event_type: str, payload: dict[str, object]) -> None:
        if on_event is not None:
            on_event(event_type, payload)

    def _ensure_session_prompt_state(self) -> SessionPromptState:
        return self._session_prompt_states.setdefault(self.session.id, SessionPromptState())

    def _session_prompt_state(self) -> SessionPromptState:
        return self._ensure_session_prompt_state()

    def _active_prompt_refs(self) -> list[str]:
        prompt_state = self._session_prompt_state()
        refs = [f"skill:{name}" for name in prompt_state.skills]
        refs.extend(f"template:{name}" for name in prompt_state.templates)
        return refs

    def _refresh_system_prompt(self) -> None:
        self.current_system_prompt = self.runtime.assemble_system_prompt(self.runtime_config, self._active_prompt_refs())

    def _build_provider_messages(self) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        if self.current_system_prompt:
            messages.append({"role": "system", "content": self.current_system_prompt})
        for message in self.session.messages:
            if message.role == "user":
                messages.append({"role": "user", "content": message.content})
            elif message.role == "assistant":
                payload: dict[str, object] = {"role": "assistant", "content": message.content or None}
                if message.tool_calls:
                    payload["tool_calls"] = [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {"name": tool_call.name, "arguments": tool_call.arguments},
                        }
                        for tool_call in message.tool_calls
                    ]
                messages.append(payload)
            elif message.role == "tool_result":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
        return messages

    def _build_provider_tools(self) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.schema,
                },
            }
            for tool in self.tools.values()
        ]

    def _tool_context(self) -> ToolContext:
        return ToolContext(
            cwd=self.config.cwd,
            workspace_root=self.config.cwd,
            timeout_seconds=self.runtime_config.tools.bash_timeout_seconds,
            max_output_bytes=self.runtime_config.tools.bash_max_output_bytes,
            read_max_lines=self.runtime_config.tools.read_max_lines,
        )

    def _run(self, on_event: EventCallback | None = None) -> AgentRunResult:
        assistant_messages: list[Message] = []
        tool_results: list[Message] = []
        usage: dict[str, object] | None = None
        self.error = None
        while True:
            request = ProviderRequest(
                model=self.config.model,
                messages=self._build_provider_messages(),
                tools=self._build_provider_tools(),
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
            assistant_text_parts: list[str] = []
            tool_call_parts: dict[int, dict[str, str]] = {}
            self.is_streaming = True
            try:
                for event in self.provider.stream_chat(request):
                    if event.type == "text_delta":
                        assistant_text_parts.append(event.delta)
                        self._emit(on_event, "text_delta", {"delta": event.delta})
                    elif event.type == "tool_call_delta":
                        index = event.index or 0
                        current = tool_call_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if event.tool_call_id:
                            current["id"] = event.tool_call_id
                        if event.tool_name:
                            current["name"] = event.tool_name
                        if event.tool_arguments_delta:
                            current["arguments"] += event.tool_arguments_delta
                    elif event.type == "usage":
                        usage = event.usage
                    elif event.type == "done":
                        break
            except ProviderAborted:
                self.is_streaming = False
                self.error = "Request aborted"
                return AgentRunResult(assistant_messages=assistant_messages, tool_results=tool_results, usage=usage, error=self.error)
            except Exception as exc:
                self.is_streaming = False
                self.error = str(exc)
                return AgentRunResult(assistant_messages=assistant_messages, tool_results=tool_results, usage=usage, error=self.error)
            finally:
                self.is_streaming = False

            tool_calls = [
                ToolCall(id=payload["id"], name=payload["name"], arguments=payload["arguments"])
                for _, payload in sorted(tool_call_parts.items())
                if payload["name"]
            ]
            assistant_message = Message(
                role="assistant",
                content="".join(assistant_text_parts),
                tool_calls=tool_calls,
                created_at=utc_now(),
            )
            self.session.messages.append(assistant_message)
            assistant_messages.append(assistant_message)
            if assistant_message.content:
                self._emit(on_event, "assistant_message", {"content": assistant_message.content})
            if not tool_calls:
                self.save_session()
                return AgentRunResult(assistant_messages=assistant_messages, tool_results=tool_results, usage=usage, error=None)

            for tool_call in tool_calls:
                self.pending_tool_calls.add(tool_call.id)
                self._emit(
                    on_event,
                    "tool_call",
                    {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments},
                )
                tool = self.tools.get(tool_call.name)
                if tool is None:
                    rendered = f"ERROR\nUnknown tool: {tool_call.name}"
                else:
                    result = execute_tool(tool, tool_call.arguments, self._tool_context())
                    rendered = format_tool_result(result)
                tool_message = Message(
                    role="tool_result",
                    content=rendered,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    created_at=utc_now(),
                )
                self.session.messages.append(tool_message)
                tool_results.append(tool_message)
                self.pending_tool_calls.discard(tool_call.id)
                self._emit(
                    on_event,
                    "tool_result",
                    {"id": tool_call.id, "name": tool_call.name, "content": rendered},
                )
