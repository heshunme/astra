from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .config import ReloadResult, ResolvedRuntimeConfig, ToolRuntimeConfig, clone_resolved_runtime_config
from .models import AgentRunResult, Message, Session, SkillCatalogEntry, ToolCall, ToolContext
from .provider import OpenAICompatibleProvider, ProviderAborted, ProviderRequest
from .runtime import CapabilityRuntime
from .runtime.runtime import PromptInspection, PromptInspectionFragment, SkillSpec
from .session import SessionStore
from .tools import execute_tool, format_tool_result


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


EventCallback = Callable[[str, dict[str, object]], None]


@dataclass(slots=True)
class PendingSkillTrigger:
    name: str
    raw_command: str


@dataclass(slots=True)
class SessionRuntimeState:
    templates: list[str] = field(default_factory=list)
    pending_skill_trigger: PendingSkillTrigger | None = None


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
        self._session_materialized = False
        self._session_runtime_states: dict[str, SessionRuntimeState] = {self.session.id: SessionRuntimeState()}
        self.is_streaming = False
        self.pending_tool_calls: set[str] = set()
        self.error: str | None = None

    @property
    def has_session(self) -> bool:
        return self._session_materialized

    @property
    def current_session_id(self) -> str | None:
        if not self._session_materialized:
            return None
        return self.session.id

    @property
    def current_session_label(self) -> str:
        if not self._session_materialized:
            return "(new)"
        return self.session.id

    @property
    def active_templates(self) -> list[str]:
        return list(self._session_runtime_state().templates)

    @property
    def pending_skill_name(self) -> str | None:
        trigger = self._session_runtime_state().pending_skill_trigger
        return trigger.name if trigger is not None else None

    def available_skill_names(self) -> list[str]:
        return [entry.name for entry in self.session.skill_catalog_snapshot if not entry.history_only]

    def history_only_skill_names(self) -> list[str]:
        return [entry.name for entry in self.session.skill_catalog_snapshot if entry.history_only]

    def load_session(self, session_id: str) -> None:
        self.session = self.session_store.load(session_id)
        self._session_materialized = True
        self.config.model = self.session.model
        self.config.system_prompt = self.session.system_prompt
        self.config.cwd = Path(self.session.cwd)
        self._ensure_session_runtime_state()
        self._refresh_system_prompt()

    def save_session(self) -> None:
        if not self._session_materialized:
            return
        self.session.model = self.config.model
        self.session.system_prompt = self.config.system_prompt
        self.session.cwd = str(self.config.cwd)
        self.session_store.save(self.session)

    def fork_session(self, name: str | None = None) -> str:
        if not self._session_materialized:
            raise RuntimeError("No saved session to fork")
        prior_state = self._session_runtime_state()
        forked = self.session_store.fork(self.session.id, name=name)
        self.session = forked
        self._session_materialized = True
        self._session_runtime_states[forked.id] = SessionRuntimeState(
            templates=list(prior_state.templates),
        )
        self._refresh_system_prompt()
        return forked.id

    def abort(self) -> None:
        self.provider.close_active_stream()

    def arm_skill(self, name: str, raw_command: str) -> tuple[bool, str]:
        skill, error = self._resolve_triggerable_skill(name)
        if skill is None:
            return False, error
        self._session_runtime_state().pending_skill_trigger = PendingSkillTrigger(name=skill.name, raw_command=raw_command)
        return True, f"Next message will use skill: {skill.name}"

    def clear_pending_skill(self) -> None:
        self._session_runtime_state().pending_skill_trigger = None

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
        state = self._session_runtime_state()
        trigger = state.pending_skill_trigger
        if trigger is None:
            return True, user_text, None
        state.pending_skill_trigger = None
        return self.build_skill_prompt(trigger.name, user_text, trigger.raw_command)

    def activate_template(self, name: str) -> tuple[bool, str]:
        if not self.runtime.has_template(name):
            return False, f"Unknown template: {name}"
        prompt_state = self._session_runtime_state()
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
        self._merge_skill_catalog_snapshot(snapshot.skills)
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

    def prompt(
        self,
        text: str,
        *,
        metadata: dict[str, object] | None = None,
        on_event: EventCallback | None = None,
    ) -> AgentRunResult:
        self._materialize_session()
        raw_name_source = dict(metadata or {}).get("raw_user_input")
        self._set_default_session_name(raw_name_source if isinstance(raw_name_source, str) else text)
        self.session.messages.append(
            Message(role="user", content=text, created_at=utc_now(), metadata=dict(metadata or {}))
        )
        self.save_session()
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

    def _ensure_session_runtime_state(self) -> SessionRuntimeState:
        return self._session_runtime_states.setdefault(self.session.id, SessionRuntimeState())

    def _session_runtime_state(self) -> SessionRuntimeState:
        return self._ensure_session_runtime_state()

    def _materialize_session(self) -> Session:
        self._session_materialized = True
        return self.session

    def _set_default_session_name(self, text: str) -> None:
        if self.session.messages:
            return
        if (self.session.name or "").strip():
            return
        normalized = text.strip()
        if normalized:
            self.session.name = normalized

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
                    source=f"session:{self.session.id}",
                    text_length=len(catalog_text),
                )
            )
            seen.add(catalog_key)

        for template_name in self.active_templates:
            key = self.runtime.normalize_prompt_ref(f"template:{template_name}")
            if key in seen:
                continue
            prompt_fragment = self.runtime.snapshot().prompt_fragments.get(key)
            if prompt_fragment is None:
                continue
            text = prompt_fragment.text.strip()
            if not text:
                continue
            seen.add(key)
            prompt_parts.append(text)
            fragments.append(
                PromptInspectionFragment(
                    key=key,
                    source=prompt_fragment.source,
                    text_length=len(text),
                )
            )

        return PromptInspection(assembled="\n\n".join(prompt_parts), fragments=fragments)

    def prompt_fragment_text(self, key: str) -> str:
        if key == "session:skills-catalog":
            return self._build_skill_catalog_text()
        prompt_fragment = self.runtime.snapshot().prompt_fragments.get(key)
        if prompt_fragment is None:
            return ""
        return prompt_fragment.text.strip()

    def _refresh_system_prompt(self) -> None:
        self.current_system_prompt = self.inspect_prompt().assembled

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

    def _build_skill_catalog_text(self) -> str:
        if "read" not in self.tools:
            return ""
        lines = [
            "Skill catalog for this session:",
            "Detailed skill instructions are not preloaded into this system prompt.",
            "When a task matches a skill, use the read tool to open that skill's files in the listed order before answering.",
            "A /skill:<name> request applies only to that turn and does not switch the session into a permanent skill mode.",
        ]
        available_entries = [entry for entry in self.session.skill_catalog_snapshot if not entry.history_only]
        if not available_entries:
            lines.append("No skills are currently available.")
            return "\n".join(lines)

        lines.append("Available skills:")
        for entry in available_entries:
            files = ", ".join(entry.files) if entry.files else "(no files listed)"
            when_to_use = entry.when_to_use or "Read when the task matches this skill or when the user explicitly requests it."
            lines.append(f"- {entry.name}: {entry.summary}")
            lines.append(f"  Use when: {when_to_use}")
            lines.append(f"  Read in order: {files}")
        return "\n".join(lines)

    def _merge_skill_catalog_snapshot(self, discovered_skills: dict[str, SkillSpec]) -> None:
        prior_entries = list(self.session.skill_catalog_snapshot)
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
                        history_only=True,
                    )
                )
                continue
            merged.append(self._skill_entry_from_spec(discovered))

        for name in sorted(discovered_skills):
            if name in seen:
                continue
            merged.append(self._skill_entry_from_spec(discovered_skills[name]))

        self.session.skill_catalog_snapshot = merged

    def _skill_entry_from_spec(self, skill: SkillSpec) -> SkillCatalogEntry:
        return SkillCatalogEntry(
            name=skill.name,
            summary=skill.summary,
            when_to_use=skill.when_to_use,
            files=list(skill.files),
            source=skill.source,
            history_only=False,
        )

    def _resolve_triggerable_skill(self, name: str) -> tuple[SkillSpec | None, str]:
        skill = self.runtime.get_skill(name)
        if skill is None:
            if any(entry.name == name and entry.history_only for entry in self.session.skill_catalog_snapshot):
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
