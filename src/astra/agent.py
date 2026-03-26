from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from .config import ReloadResult, ResolvedRuntimeConfig, clone_resolved_runtime_config
from .models import (
    AgentConversationState,
    AgentEvent,
    AgentRunResult,
    AgentRuntimeState,
    AgentSnapshot,
    CoreCommandResult,
    Message,
    PendingSkillTriggerState,
    SkillCatalogEntry,
    ToolCall,
    ToolContext,
    clone_agent_snapshot,
    clone_messages,
    clone_skill_catalog,
)
from .provider import OpenAICompatibleProvider, ProviderAborted, ProviderRequest
from .runtime import CapabilityRuntime
from .runtime.runtime import PromptInspection, PromptInspectionFragment, SkillSpec
from .tools import execute_tool, format_tool_result


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


EventCallback = Callable[[str, dict[str, object]], None]
EventSubscriber = Callable[[AgentEvent], None]
ProviderFactory = Callable[[str], object]
ToolExecutor = Callable[[object, str, ToolContext], object]


@dataclass(slots=True)
class AgentConfig:
    model: str
    api_key: str
    base_url: str
    cwd: Path
    system_prompt: str


class _CoreEngine:
    def __init__(
        self,
        config: AgentConfig,
        *,
        provider: object | None = None,
        provider_factory: ProviderFactory | None = None,
        tool_executor: ToolExecutor | None = None,
    ):
        self.config = config
        self._provider_factory = provider_factory or (lambda base_url: OpenAICompatibleProvider(base_url))
        self.provider = provider or self._provider_factory(config.base_url)
        self._tool_executor = tool_executor or execute_tool
        self.conversation_state = AgentConversationState()
        self.tools: dict[str, object] = {}
        self.current_system_prompt = ""
        self.is_streaming = False
        self.pending_tool_calls: set[str] = set()
        self.error: str | None = None
        self._read_max_lines = 400
        self._bash_timeout_seconds = 60
        self._bash_max_output_bytes = 32 * 1024
        self._skill_file_aliases: dict[str, Path] = {}

    @property
    def messages(self) -> list[Message]:
        return self.conversation_state.messages

    def snapshot_conversation(self) -> AgentConversationState:
        return AgentConversationState(messages=clone_messages(self.messages))

    def restore_conversation(self, conversation: AgentConversationState) -> None:
        self.conversation_state = AgentConversationState(messages=clone_messages(conversation.messages))

    def replace_messages(self, messages: list[Message]) -> None:
        self.conversation_state.messages = clone_messages(messages)

    def sync_execution_state(
        self,
        runtime_config: ResolvedRuntimeConfig,
        cwd: Path,
        *,
        tools: dict[str, object] | None = None,
        current_system_prompt: str | None = None,
        skill_file_aliases: dict[str, Path] | None = None,
        rebuild_provider: bool,
    ) -> None:
        prior_base_url = self.config.base_url
        self.config.model = runtime_config.model
        self.config.base_url = runtime_config.base_url
        self.config.system_prompt = runtime_config.system_prompt
        self.config.cwd = cwd
        self._read_max_lines = runtime_config.tools.read_max_lines
        self._bash_timeout_seconds = runtime_config.tools.bash_timeout_seconds
        self._bash_max_output_bytes = runtime_config.tools.bash_max_output_bytes
        if tools is not None:
            self.tools = dict(tools)
        if current_system_prompt is not None:
            self.current_system_prompt = current_system_prompt
        if skill_file_aliases is not None:
            self._skill_file_aliases = dict(skill_file_aliases)
        if rebuild_provider or prior_base_url != runtime_config.base_url:
            self.provider = self._provider_factory(runtime_config.base_url)

    def set_model(self, model: str) -> None:
        self.config.model = model

    def set_base_url(self, base_url: str) -> None:
        self.config.base_url = base_url
        self.provider = self._provider_factory(base_url)

    def abort(self) -> None:
        close_active_stream = getattr(self.provider, "close_active_stream", None)
        if callable(close_active_stream):
            close_active_stream()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else monotonic() + timeout
        while self.is_streaming:
            if deadline is not None and monotonic() >= deadline:
                return False
            sleep(0.01)
        return True

    def _build_provider_messages(self) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        if self.current_system_prompt:
            messages.append({"role": "system", "content": self.current_system_prompt})
        for message in self.messages:
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
        cwd = self.config.cwd
        return ToolContext(
            cwd=cwd,
            workspace_root=cwd,
            timeout_seconds=self._bash_timeout_seconds,
            max_output_bytes=self._bash_max_output_bytes,
            read_max_lines=self._read_max_lines,
            skill_file_aliases=dict(self._skill_file_aliases),
        )

    def run(
        self,
        *,
        publish_event: Callable[[str, dict[str, object] | None, EventCallback | None], None],
        on_event: EventCallback | None = None,
        raw_input: str | None = None,
    ) -> AgentRunResult:
        assistant_messages: list[Message] = []
        tool_results: list[Message] = []
        usage: dict[str, object] | None = None
        self.error = None
        publish_event("agent_start", {"raw_input": raw_input}, on_event)
        publish_event("turn_start", {"message_count": len(self.messages)}, on_event)
        try:
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
                publish_event("message_start", {"role": "assistant"}, on_event)
                try:
                    for event in self.provider.stream_chat(request):
                        if event.type == "text_delta":
                            assistant_text_parts.append(event.delta)
                            publish_event("message_update", {"delta": event.delta}, on_event)
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
                    self.error = "Request aborted"
                except Exception as exc:
                    self.error = str(exc)
                finally:
                    self.is_streaming = False

                if self.error is not None:
                    publish_event("error", {"message": self.error}, on_event)
                    publish_event("turn_end", {"success": False, "error": self.error}, on_event)
                    return AgentRunResult(
                        assistant_messages=assistant_messages,
                        tool_results=tool_results,
                        usage=usage,
                        error=self.error,
                    )

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
                self.messages.append(assistant_message)
                assistant_messages.append(assistant_message)
                publish_event(
                    "message_end",
                    {
                        "role": "assistant",
                        "content": assistant_message.content,
                        "tool_calls": [
                            {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments}
                            for tool_call in tool_calls
                        ],
                    },
                    on_event,
                )
                if not tool_calls:
                    publish_event("turn_end", {"success": True}, on_event)
                    return AgentRunResult(
                        assistant_messages=assistant_messages,
                        tool_results=tool_results,
                        usage=usage,
                        error=None,
                    )

                for tool_call in tool_calls:
                    self.pending_tool_calls.add(tool_call.id)
                    publish_event(
                        "tool_execution_start",
                        {"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments},
                        on_event,
                    )
                    tool = self.tools.get(tool_call.name)
                    is_error = False
                    if tool is None:
                        rendered = f"ERROR\nUnknown tool: {tool_call.name}"
                        is_error = True
                    else:
                        result = self._tool_executor(tool, tool_call.arguments, self._tool_context())
                        rendered = format_tool_result(result)
                        is_error = bool(getattr(result, "is_error", False))
                    tool_message = Message(
                        role="tool_result",
                        content=rendered,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        created_at=utc_now(),
                    )
                    self.messages.append(tool_message)
                    tool_results.append(tool_message)
                    self.pending_tool_calls.discard(tool_call.id)
                    publish_event(
                        "tool_execution_end",
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "content": rendered,
                            "is_error": is_error,
                        },
                        on_event,
                    )
        finally:
            self.is_streaming = False
            publish_event("agent_end", {"error": self.error}, on_event)


class Agent:
    _EXTENSION_COMMAND_USAGES = ["/skill:<name> [request]", "/template:<name> <request>"]

    def __init__(
        self,
        config: AgentConfig,
        capability_runtime: CapabilityRuntime,
        *,
        provider: object | None = None,
        provider_factory: ProviderFactory | None = None,
        tool_executor: ToolExecutor | None = None,
        initial_snapshot: AgentSnapshot | None = None,
    ):
        self.config = config
        self.runtime = capability_runtime
        self._engine = _CoreEngine(
            config,
            provider=provider,
            provider_factory=provider_factory,
            tool_executor=tool_executor,
        )
        self.runtime_state = AgentRuntimeState(
            cwd=str(config.cwd),
            runtime_config=ResolvedRuntimeConfig(
                model=config.model,
                base_url=config.base_url,
                system_prompt=config.system_prompt,
            ),
        )
        self._subscribers: list[EventSubscriber] = []
        if initial_snapshot is not None:
            self.restore(initial_snapshot)

    @property
    def conversation_state(self) -> AgentConversationState:
        return self._engine.conversation_state

    @property
    def provider(self) -> object:
        return self._engine.provider

    @provider.setter
    def provider(self, value: object) -> None:
        self._engine.provider = value

    @property
    def tools(self) -> dict[str, object]:
        return self._engine.tools

    @tools.setter
    def tools(self, value: dict[str, object]) -> None:
        self._engine.tools = dict(value)

    @property
    def current_system_prompt(self) -> str:
        return self._engine.current_system_prompt

    @current_system_prompt.setter
    def current_system_prompt(self, value: str) -> None:
        self._engine.current_system_prompt = value

    @property
    def is_streaming(self) -> bool:
        return self._engine.is_streaming

    @is_streaming.setter
    def is_streaming(self, value: bool) -> None:
        self._engine.is_streaming = value

    @property
    def pending_tool_calls(self) -> set[str]:
        return self._engine.pending_tool_calls

    @property
    def error(self) -> str | None:
        return self._engine.error

    @error.setter
    def error(self, value: str | None) -> None:
        self._engine.error = value

    @property
    def messages(self) -> list[Message]:
        return self._engine.messages

    @property
    def runtime_config(self) -> ResolvedRuntimeConfig:
        return self.runtime_state.runtime_config

    @property
    def pending_skill_name(self) -> str | None:
        trigger = self.runtime_state.pending_skill_trigger
        return trigger.name if trigger is not None else None

    def available_skill_names(self) -> list[str]:
        return [entry.name for entry in self.runtime_state.skill_catalog_snapshot if not entry.history_only]

    def available_skills(self) -> list[SkillCatalogEntry]:
        return clone_skill_catalog(
            [entry for entry in self.runtime_state.skill_catalog_snapshot if not entry.history_only]
        )

    def history_only_skill_names(self) -> list[str]:
        return [entry.name for entry in self.runtime_state.skill_catalog_snapshot if entry.history_only]

    def subscribe(self, callback: EventSubscriber) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def abort(self) -> None:
        self._engine.abort()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        return self._engine.wait_for_idle(timeout)

    def snapshot(self) -> AgentSnapshot:
        return AgentSnapshot(
            conversation=self._engine.snapshot_conversation(),
            runtime=AgentRuntimeState(
                cwd=self.runtime_state.cwd,
                runtime_config=clone_resolved_runtime_config(self.runtime_config),
                skill_catalog_snapshot=clone_skill_catalog(self.runtime_state.skill_catalog_snapshot),
                pending_skill_trigger=(
                    PendingSkillTriggerState(
                        name=self.runtime_state.pending_skill_trigger.name,
                        raw_command=self.runtime_state.pending_skill_trigger.raw_command,
                    )
                    if self.runtime_state.pending_skill_trigger is not None
                    else None
                ),
            ),
        )

    def restore(self, snapshot: AgentSnapshot) -> None:
        restored = clone_agent_snapshot(snapshot)
        self._engine.restore_conversation(restored.conversation)
        self.runtime_state = restored.runtime
        self.config.cwd = Path(self.runtime_state.cwd)
        self.runtime.cwd = self.config.cwd
        self._engine.sync_execution_state(
            self.runtime_config,
            self.config.cwd,
            tools=self.tools,
            current_system_prompt="",
            skill_file_aliases=self.runtime.snapshot().skill_file_aliases,
            rebuild_provider=False,
        )
        self._refresh_system_prompt()
        self._publish("state_changed", {"reason": "restore"})

    def set_model(self, model: str) -> None:
        self.runtime_state.runtime_config.model = model
        self._engine.set_model(model)
        self._publish("state_changed", {"reason": "model", "model": model})

    def set_base_url(self, base_url: str) -> None:
        self.runtime_state.runtime_config.base_url = base_url
        self._engine.set_base_url(base_url)
        self._publish("state_changed", {"reason": "base_url", "base_url": base_url})

    def set_system_prompt(self, system_prompt: str) -> ReloadResult:
        updated_runtime = clone_resolved_runtime_config(self.runtime_config)
        updated_runtime.system_prompt = system_prompt
        return self.apply_runtime_config(updated_runtime)

    def set_tools(self, enabled_tools: list[str]) -> ReloadResult:
        updated_runtime = clone_resolved_runtime_config(self.runtime_config)
        updated_runtime.tools.enabled_tools = list(enabled_tools)
        return self.apply_runtime_config(updated_runtime)

    def replace_messages(self, messages: list[Message]) -> None:
        self._engine.replace_messages(messages)
        self._publish("state_changed", {"reason": "messages"})

    def apply_runtime_config(self, runtime_config: ResolvedRuntimeConfig) -> ReloadResult:
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

        self.runtime_state.runtime_config = clone_resolved_runtime_config(runtime_config)
        self.config.cwd = Path(self.runtime_state.cwd)
        self.runtime.cwd = self.config.cwd
        self._engine.sync_execution_state(
            self.runtime_config,
            self.config.cwd,
            tools=snapshot.tools,
            current_system_prompt="",
            skill_file_aliases=snapshot.skill_file_aliases,
            rebuild_provider=True,
        )
        self._merge_skill_catalog_snapshot(snapshot.skills)
        self._refresh_system_prompt()
        self._publish(
            "state_changed",
            {
                "reason": "runtime",
                "model": self.config.model,
                "base_url": self.config.base_url,
                "tools": list(self.tools),
            },
        )
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

    def reload_runtime(self, runtime_config: ResolvedRuntimeConfig) -> ReloadResult:
        return self.apply_runtime_config(runtime_config)

    def inspect_runtime(self) -> dict[str, object]:
        snapshot = self.runtime.snapshot()
        return {
            "model": self.config.model,
            "base_url": self.config.base_url,
            "tools": list(self.tools),
            "prompts": {
                "order": list(self.runtime_config.prompts.order),
                "available": self.runtime.list_prompt_keys(),
                "loaded": list(snapshot.diagnostics.loaded_prompts),
            },
            "skills": {
                "available": self.available_skill_names(),
                "history_only": self.history_only_skill_names(),
                "pending": self.pending_skill_name,
                "loaded": list(snapshot.diagnostics.loaded_skills),
                "entries": [
                    {
                        "name": entry.name,
                        "summary": entry.summary,
                        "source": entry.source,
                        "source_label": entry.source_label,
                        "shadowed_sources": list(entry.shadowed_sources),
                        "history_only": entry.history_only,
                    }
                    for entry in self.available_skills()
                ],
                "conflicts": [
                    {
                        "name": conflict.name,
                        "winner_source": conflict.winner_source,
                        "winner_source_label": conflict.winner_source_label,
                        "shadowed_sources": list(conflict.shadowed_sources),
                        "shadowed_source_labels": list(conflict.shadowed_source_labels),
                    }
                    for conflict in snapshot.diagnostics.skill_conflicts
                ],
            },
            "templates": {
                "available": self.runtime.list_template_names(),
            },
            "tool_defaults": {
                "read_max_lines": self.runtime_config.tools.read_max_lines,
                "bash_timeout_seconds": self.runtime_config.tools.bash_timeout_seconds,
                "bash_max_output_bytes": self.runtime_config.tools.bash_max_output_bytes,
            },
            "warnings": self.runtime.warnings(),
        }

    def prompt(
        self,
        text: str,
        *,
        metadata: dict[str, object] | None = None,
        raw_input: str | None = None,
        on_event: EventCallback | None = None,
    ) -> AgentRunResult:
        success, effective_text, effective_metadata = self.consume_pending_skill_prompt(text)
        if not success:
            return AgentRunResult(assistant_messages=[], tool_results=[], error=effective_text)

        return self._submit_user_message(
            effective_text,
            metadata=effective_metadata,
            extra_metadata=metadata,
            raw_input=raw_input or text,
            on_event=on_event,
        )

    def continue_from_context(self, on_event: EventCallback | None = None) -> AgentRunResult:
        if not self.messages:
            raise RuntimeError("No messages to continue from")
        if self.messages[-1].role == "assistant":
            raise RuntimeError("Cannot continue from assistant message")
        return self._engine.run(publish_event=self._publish, on_event=on_event, raw_input=None)

    def try_handle_extension_command(
        self,
        raw_input: str,
        *,
        on_event: EventCallback | None = None,
    ) -> CoreCommandResult | None:
        if raw_input.startswith("/skill:"):
            remainder = raw_input[len("/skill:") :].strip()
            if not remainder:
                return None
            name, _, request_text = remainder.partition(" ")
            if not name:
                return None
            request_text = request_text.strip()
            if request_text:
                try:
                    result = self.run_skill(name, request_text, raw_input, on_event=on_event)
                except ValueError as exc:
                    return CoreCommandResult(message=str(exc), error=str(exc), persist_state=False)
                return CoreCommandResult(run_result=result, error=result.error, persist_state=True)
            success, message = self.arm_skill(name, raw_input)
            return CoreCommandResult(message=message, error=None if success else message, persist_state=False)

        if raw_input.startswith("/template:"):
            name = raw_input[len("/template:") :].strip()
            if not name:
                usage = "Usage: /template:<name> <request>"
                return CoreCommandResult(message=usage, error=usage, persist_state=False)
            name, _, request_text = name.partition(" ")
            request_text = request_text.strip()
            if not name or not request_text:
                usage = "Usage: /template:<name> <request>"
                return CoreCommandResult(message=usage, error=usage, persist_state=False)
            try:
                result = self.run_template(name, request_text, raw_input, on_event=on_event)
            except ValueError as exc:
                return CoreCommandResult(message=str(exc), error=str(exc), persist_state=False)
            return CoreCommandResult(run_result=result, error=result.error, persist_state=True)

        return None

    def extension_command_usages(self) -> list[str]:
        return list(self._EXTENSION_COMMAND_USAGES)

    def arm_skill(self, name: str, raw_command: str) -> tuple[bool, str]:
        skill, error = self._resolve_triggerable_skill(name)
        if skill is None:
            return False, error
        self.runtime_state.pending_skill_trigger = PendingSkillTriggerState(name=skill.name, raw_command=raw_command)
        self._publish("state_changed", {"reason": "pending_skill", "name": skill.name})
        return True, f"Next message will use skill: {skill.name}"

    def clear_pending_skill(self) -> None:
        self.runtime_state.pending_skill_trigger = None
        self._publish("state_changed", {"reason": "pending_skill", "name": None})

    def run_skill(
        self,
        name: str,
        request_text: str,
        raw_command: str,
        *,
        on_event: EventCallback | None = None,
    ) -> AgentRunResult:
        success, rewritten, metadata = self.build_skill_prompt(name, request_text, raw_command)
        if not success:
            raise ValueError(rewritten)
        self.clear_pending_skill()
        return self.prompt(rewritten, metadata=metadata, raw_input=raw_command, on_event=on_event)

    def build_skill_prompt(self, name: str, user_text: str, raw_command: str) -> tuple[bool, str, dict[str, object] | None]:
        skill, error = self._resolve_triggerable_skill(name)
        if skill is None:
            return False, error, None
        rewritten = self._rewrite_skill_request(self._skill_entry_from_spec(skill), user_text)
        metadata = {
            "raw_user_input": raw_command,
            "skill_trigger": {
                "name": skill.name,
                "source": skill.source,
                "files": list(skill.files),
            },
        }
        return True, rewritten, metadata

    def consume_pending_skill_prompt(self, user_text: str) -> tuple[bool, str, dict[str, object] | None]:
        trigger = self.runtime_state.pending_skill_trigger
        if trigger is None:
            return True, user_text, None
        self.runtime_state.pending_skill_trigger = None
        return self.build_skill_prompt(trigger.name, user_text, trigger.raw_command)

    def run_template(
        self,
        name: str,
        request_text: str,
        raw_command: str,
        *,
        on_event: EventCallback | None = None,
    ) -> AgentRunResult:
        success, rewritten, metadata = self.build_template_prompt(name, request_text, raw_command)
        if not success:
            raise ValueError(rewritten)
        return self._submit_user_message(
            rewritten,
            metadata=metadata,
            raw_input=raw_command,
            on_event=on_event,
        )

    def build_template_prompt(
        self,
        name: str,
        user_text: str,
        raw_command: str,
    ) -> tuple[bool, str, dict[str, object] | None]:
        key = self.runtime.normalize_prompt_ref(f"template:{name}")
        fragment = self.runtime.snapshot().prompt_fragments.get(key)
        if fragment is None:
            return False, f"Unknown template: {name}", None
        rewritten = self._rewrite_template_request(name, fragment.text.strip(), user_text)
        metadata = {
            "raw_user_input": raw_command,
            "template_trigger": {
                "name": name,
                "source": fragment.source,
                "key": key,
            },
        }
        return True, rewritten, metadata

    def inspect_prompt(self) -> PromptInspection:
        default_inspection = self.runtime.inspect_prompt(self.runtime_config)
        prompt_parts: list[str] = []
        fragments: list[PromptInspectionFragment] = []
        seen: set[str] = set()

        for fragment in default_inspection.fragments:
            prompt_fragment = self.runtime.snapshot().prompt_fragments.get(fragment.key)
            if prompt_fragment is None:
                continue
            text = prompt_fragment.text.strip()
            if not text or fragment.key in seen:
                continue
            seen.add(fragment.key)
            prompt_parts.append(text)
            fragments.append(fragment)

        catalog_text = self._build_skill_catalog_text().strip()
        if catalog_text:
            catalog_key = "session:skills-catalog"
            prompt_parts.append(catalog_text)
            fragments.append(
                PromptInspectionFragment(
                    key=catalog_key,
                    source="coding-agent",
                    text_length=len(catalog_text),
                )
            )
            seen.add(catalog_key)

        return PromptInspection(assembled="\n\n".join(prompt_parts), fragments=fragments)

    def prompt_fragment_text(self, key: str) -> str:
        if key == "session:skills-catalog":
            return self._build_skill_catalog_text()
        prompt_fragment = self.runtime.snapshot().prompt_fragments.get(key)
        if prompt_fragment is None:
            return ""
        return prompt_fragment.text.strip()

    def _publish(
        self,
        event_type: str,
        payload: dict[str, object] | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        event_payload = dict(payload or {})
        event = AgentEvent(type=event_type, payload=event_payload)
        for subscriber in list(self._subscribers):
            subscriber(event)
        if on_event is None:
            return
        on_event(event.type, dict(event_payload))
        if event_type == "message_update":
            delta = event_payload.get("delta")
            if isinstance(delta, str):
                on_event("text_delta", {"delta": delta})
        elif event_type == "tool_execution_start":
            on_event(
                "tool_call",
                {
                    "id": event_payload.get("id"),
                    "name": event_payload.get("name"),
                    "arguments": event_payload.get("arguments"),
                },
            )
        elif event_type == "tool_execution_end":
            on_event(
                "tool_result",
                {
                    "id": event_payload.get("id"),
                    "name": event_payload.get("name"),
                    "content": event_payload.get("content"),
                },
            )

    def _refresh_system_prompt(self) -> None:
        self.current_system_prompt = self.inspect_prompt().assembled

    def _submit_user_message(
        self,
        text: str,
        *,
        metadata: dict[str, object] | None = None,
        extra_metadata: dict[str, object] | None = None,
        raw_input: str,
        on_event: EventCallback | None = None,
    ) -> AgentRunResult:
        merged_metadata = dict(metadata or {})
        if extra_metadata:
            merged_metadata.update(extra_metadata)
        self.messages.append(
            Message(
                role="user",
                content=text,
                created_at=utc_now(),
                metadata=merged_metadata,
            )
        )
        return self._engine.run(publish_event=self._publish, on_event=on_event, raw_input=raw_input)

    def _build_skill_catalog_text(self) -> str:
        if "read" not in self.tools:
            return ""
        lines = [
            "Skill catalog for this session:",
            "Detailed skill instructions are not preloaded into this system prompt.",
            "When a task matches a skill, use the read tool to open that skill's files in the listed order before answering.",
            "A /skill:<name> request applies only to that turn and does not switch the session into a permanent skill mode.",
        ]
        available_entries = [entry for entry in self.runtime_state.skill_catalog_snapshot if not entry.history_only]
        if not available_entries:
            lines.append("No skills are currently available.")
            return "\n".join(lines)

        lines.append("Available skills:")
        for entry in available_entries:
            files = ", ".join(entry.files) if entry.files else "(no files listed)"
            when_to_use = entry.when_to_use or "Read when the task matches this skill or when the user explicitly requests it."
            lines.append(f"- {entry.name}: {entry.summary}")
            lines.append(f"  Use when: {when_to_use}")
            if entry.source_label:
                lines.append(f"  Source: {entry.source_label}")
            if entry.shadowed_sources:
                lines.append(f"  Shadowed definitions: {len(entry.shadowed_sources)}")
            lines.append(f"  Read in order: {files}")
        return "\n".join(lines)

    def _merge_skill_catalog_snapshot(self, discovered_skills: dict[str, SkillSpec]) -> None:
        prior_entries = list(self.runtime_state.skill_catalog_snapshot)
        merged: list[SkillCatalogEntry] = []
        seen: set[str] = set()

        for entry in prior_entries:
            if entry.name in seen:
                continue
            seen.add(entry.name)
            discovered = discovered_skills.get(entry.name)
            if discovered is None:
                merged.append(
                    SkillCatalogEntry(
                        name=entry.name,
                        summary=entry.summary,
                        when_to_use=entry.when_to_use,
                        files=list(entry.files),
                        source=entry.source,
                        source_label=entry.source_label,
                        shadowed_sources=list(entry.shadowed_sources),
                        history_only=True,
                    )
                )
                continue
            merged.append(self._skill_entry_from_spec(discovered))

        for name in sorted(discovered_skills):
            if name in seen:
                continue
            merged.append(self._skill_entry_from_spec(discovered_skills[name]))

        self.runtime_state.skill_catalog_snapshot = merged

    def _skill_entry_from_spec(self, skill: SkillSpec) -> SkillCatalogEntry:
        return SkillCatalogEntry(
            name=skill.name,
            summary=skill.summary,
            when_to_use=skill.when_to_use,
            files=list(skill.files),
            source=skill.source,
            source_label=skill.source_label,
            shadowed_sources=list(skill.shadowed_sources),
            history_only=False,
        )

    def _resolve_triggerable_skill(self, name: str) -> tuple[SkillSpec | None, str]:
        skill = self.runtime.get_skill(name)
        if skill is None:
            if any(entry.name == name and entry.history_only for entry in self.runtime_state.skill_catalog_snapshot):
                return None, f"Skill is no longer available: {name}"
            return None, f"Unknown skill: {name}"
        if "read" not in self.tools:
            return None, "Cannot use /skill:... because the read tool is disabled."
        return skill, ""

    def _rewrite_skill_request(self, entry: SkillCatalogEntry, user_text: str) -> str:
        files = "\n".join(f"- {path}" for path in entry.files) or "- (no files listed)"
        return "\n".join(
            [
                f"Please use the skill '{entry.name}' for this turn only.",
                "Read these skill files in order before answering:",
                files,
                "This is not a permanent mode switch.",
                "Original user request:",
                user_text,
            ]
        )

    def _rewrite_template_request(self, name: str, template_text: str, user_text: str) -> str:
        return "\n".join(
            [
                "Please follow the template instructions below for this turn only.",
                "",
                f"Template: {name}",
                "Template instructions:",
                template_text,
                "",
                "Original user request:",
                user_text,
            ]
        )
