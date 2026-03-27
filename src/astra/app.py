from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .agent import Agent, AgentConfig, EventSubscriber
from .config import (
    ConfigError,
    ConfigManager,
    DotenvError,
    ReloadResult,
    ResolvedRuntimeConfig,
    RuntimeConfig,
    clone_resolved_runtime_config,
    merged_env,
    resolve_runtime_config,
)
from .models import AgentRunResult, Session, SessionSummary
from .runtime import CapabilityRuntime
from .session import (
    SessionStore,
    agent_snapshot_from_dict,
    agent_snapshot_to_dict,
    apply_agent_snapshot_to_session,
    session_to_agent_snapshot,
)


AgentFactory = Callable[[AgentConfig, CapabilityRuntime], Agent]
RuntimeFactory = Callable[[Path], CapabilityRuntime]
SessionStoreFactory = Callable[[], SessionStore]
ConfigManagerFactory = Callable[[], ConfigManager]
EnvProvider = Callable[[], Mapping[str, str]]


@dataclass(slots=True)
class AstraAppOptions:
    cwd: str | Path
    session_id: str | None = None
    new_session: bool = False
    model_override: str | None = None
    base_url_override: str | None = None
    system_prompt_override: str | None = None


@dataclass(slots=True)
class AppSessionState:
    session: Session
    materialized: bool = False
    needs_auto_save: bool = False
    runtime_snapshot_dirty: bool = False


@dataclass(slots=True)
class CommandHelpEntry:
    usage: str
    summary: str


@dataclass(slots=True)
class PromptFragmentSummary:
    key: str
    source: str
    text_length: int


@dataclass(slots=True)
class RuntimePromptSummary:
    assembled: str
    char_length: int
    fragment_count: int
    fragments: list[PromptFragmentSummary] = field(default_factory=list)


@dataclass(slots=True)
class ResumeCandidate:
    id: str
    name: str | None
    cwd: str
    updated_at: str
    parent_session_id: str | None


@dataclass(slots=True)
class AppActionResult:
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    persisted: bool = False


class AstraApp:
    _BUILTIN_HELP = [
        CommandHelpEntry("/help", "Show help"),
        CommandHelpEntry("/reload | /reload code", "Reload runtime or code"),
        CommandHelpEntry("/model [name]", "Show or set model"),
        CommandHelpEntry("/base-url [url]", "Show or set base URL"),
        CommandHelpEntry("/tools", "Show enabled tools and defaults"),
        CommandHelpEntry("/skills", "List available skills"),
        CommandHelpEntry("/templates", "List available templates"),
        CommandHelpEntry(
            "/runtime | /runtime warnings | /runtime json | /runtime prompt | /runtime json prompt",
            "Show capability runtime state",
        ),
        CommandHelpEntry("/sessions", "List saved sessions"),
        CommandHelpEntry("/resume", "Resume a saved session by number"),
        CommandHelpEntry("/switch <session-id-prefix>", "Switch sessions"),
        CommandHelpEntry("/fork [name]", "Fork the current session"),
        CommandHelpEntry("/rename <name>", "Rename the current session"),
        CommandHelpEntry("/save", "Save the current session"),
        CommandHelpEntry("/exit", "Exit the CLI"),
        CommandHelpEntry("/skill:<name> [request]", "Apply a discovered skill for one turn"),
        CommandHelpEntry("/template:<name> <request>", "Apply a discovered template for one turn"),
    ]

    def __init__(
        self,
        options: AstraAppOptions,
        *,
        agent_factory: AgentFactory | None = None,
        runtime_factory: RuntimeFactory | None = None,
        session_store_factory: SessionStoreFactory | None = None,
        config_manager_factory: ConfigManagerFactory | None = None,
        env_provider: EnvProvider | None = None,
    ):
        self.options = options
        self.cwd = Path(options.cwd).resolve()
        self._agent_factory = agent_factory or (lambda cfg, runtime: Agent(cfg, runtime))
        self._runtime_factory = runtime_factory or CapabilityRuntime
        self._session_store_factory = session_store_factory or SessionStore
        self._config_manager_factory = config_manager_factory or ConfigManager
        self._env_provider = env_provider or (lambda: dict(os.environ))

        self.store = self._session_store_factory()
        self.config_manager = self._config_manager_factory()
        self.runtime_env: dict[str, str] = {}
        self.api_key: str | None = None
        self.capability_runtime: CapabilityRuntime | None = None
        self.agent: Agent | None = None
        self.session_state: AppSessionState | None = None

    def startup(self) -> AppActionResult:
        runtime_config, warnings = self._load_runtime_config(self.cwd)
        self.capability_runtime = self._runtime_factory(self.cwd)
        self.agent = self._agent_factory(
            AgentConfig(
                model=runtime_config.model,
                api_key=self.api_key,
                base_url=runtime_config.base_url,
                runtime_env=dict(self.runtime_env),
                cwd=self.cwd,
                system_prompt=runtime_config.system_prompt,
            ),
            self.capability_runtime,
        )
        self.session_state = AppSessionState(
            session=self.store.create(cwd=str(self.cwd), model=runtime_config.model, system_prompt=runtime_config.system_prompt),
            materialized=False,
        )

        startup_reload = self.agent.apply_runtime_config(runtime_config)
        if startup_reload.success:
            warnings.extend(startup_reload.warnings)
        else:
            warnings.append(startup_reload.message)

        if self.options.session_id and not self.options.new_session:
            try:
                loaded_session = self.store.load_by_prefix(self.options.session_id)
            except ValueError as exc:
                message = str(exc)
                return AppActionResult(message=message, warnings=warnings, error=message)
            restored = self._restore_session(loaded_session)
            if restored.error:
                return AppActionResult(message=restored.message, warnings=warnings + restored.warnings, error=restored.error)
            warnings.extend(restored.warnings)

        return AppActionResult(message="Started application.", warnings=warnings)

    def current_session_id(self) -> str | None:
        session_state = self._require_session_state()
        if not session_state.materialized:
            return None
        return session_state.session.id

    def session_handle_id(self) -> str:
        return self._require_session_state().session.id

    def current_session_name(self) -> str | None:
        return self._require_session_state().session.name

    def has_materialized_session(self) -> bool:
        return self._require_session_state().materialized

    def current_cwd(self) -> Path:
        return Path(self._require_agent().runtime_state.cwd)

    def session_cwd(self) -> str:
        return self._require_session_state().session.cwd

    def session_updated_at(self) -> str:
        return self._require_session_state().session.updated_at

    @property
    def is_streaming(self) -> bool:
        return self._require_agent().is_streaming

    def help_entries(self) -> list[CommandHelpEntry]:
        return list(self._BUILTIN_HELP)

    def subscribe(self, callback: EventSubscriber) -> Callable[[], None]:
        return self._require_agent().subscribe(callback)

    def abort(self) -> None:
        self._require_agent().abort()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        return self._require_agent().wait_for_idle(timeout)

    def get_model(self) -> str:
        return self._require_agent().config.model

    def set_model(self, model: str) -> AppActionResult:
        agent = self._require_agent()
        agent.set_model(model)
        persisted = self._persist_agent_state() if self._require_session_state().materialized else False
        return AppActionResult(message=f"Model set to {agent.config.model}", persisted=persisted)

    def get_base_url(self) -> str:
        return self._require_agent().config.base_url

    def set_base_url(self, base_url: str) -> AppActionResult:
        agent = self._require_agent()
        agent.set_base_url(base_url)
        persisted = self._persist_agent_state() if self._require_session_state().materialized else False
        return AppActionResult(message=f"Base URL set to {agent.config.base_url}", persisted=persisted)

    def get_tools_summary(self) -> dict[str, object]:
        summary = self.get_runtime_summary()
        return {
            "tools": summary["tools"],
            "tool_defaults": summary["tool_defaults"],
        }

    def get_skills(self) -> list[object]:
        return self._require_agent().available_skills()

    def get_templates(self) -> list[str]:
        return self._require_agent().runtime.list_template_names()

    def get_runtime_summary(self) -> dict[str, object]:
        return self._require_agent().inspect_runtime()

    def get_runtime_prompt_summary(self) -> RuntimePromptSummary:
        inspection = self._require_agent().inspect_prompt()
        fragments = [
            PromptFragmentSummary(
                key=fragment.key,
                source=fragment.source,
                text_length=fragment.text_length,
            )
            for fragment in inspection.fragments
        ]
        return RuntimePromptSummary(
            assembled=inspection.assembled,
            char_length=len(inspection.assembled),
            fragment_count=len(fragments),
            fragments=fragments,
        )

    def prompt_fragment_text(self, key: str) -> str:
        return self._require_agent().prompt_fragment_text(key)

    def reload_runtime(self) -> ReloadResult:
        target_cwd = self.current_cwd()
        runtime_config, warnings = self._load_runtime_config(target_cwd)
        agent = self._require_agent()
        self.cwd = target_cwd
        self._apply_runtime_env_to_agent(agent)
        result = agent.apply_runtime_config(runtime_config)
        combined_warnings = list(warnings)
        combined_warnings.extend(result.warnings)
        if result.success and self._require_session_state().materialized:
            self._require_session_state().runtime_snapshot_dirty = True
        return ReloadResult(
            success=result.success,
            message=result.message,
            applied_model=result.applied_model,
            applied_base_url=result.applied_base_url,
            enabled_tools=list(result.enabled_tools),
            loaded_prompts=list(result.loaded_prompts),
            loaded_skills=list(result.loaded_skills),
            warnings=combined_warnings,
        )

    def reload_code(self) -> AppActionResult:
        agent = self._require_agent()
        if agent.is_streaming:
            message = "Cannot reload while a response is streaming."
            return AppActionResult(message=message, error=message)

        snapshot_dict = agent_snapshot_to_dict(agent.snapshot())
        try:
            config_module = importlib.reload(importlib.import_module("astra.config"))
            importlib.reload(importlib.import_module("astra.tools"))
            importlib.reload(importlib.import_module("astra.provider"))
            importlib.reload(importlib.import_module("astra.runtime.builtin_capabilities"))
            runtime_module = importlib.reload(importlib.import_module("astra.runtime.runtime"))
            session_module = importlib.reload(importlib.import_module("astra.session"))
            agent_module = importlib.reload(importlib.import_module("astra.agent"))
        except Exception as exc:
            message = f"Code reload failed: {exc}"
            return AppActionResult(message=message, error=message)

        self.config_manager = config_module.ConfigManager()
        runtime_after_code_reload = config_module.ResolvedRuntimeConfig(
            model=agent.config.model,
            base_url=agent.config.base_url,
            system_prompt=agent.config.system_prompt,
            tools=config_module.ToolRuntimeConfig(
                enabled_tools=list(agent.runtime_config.tools.enabled_tools),
                read_max_lines=agent.runtime_config.tools.read_max_lines,
                bash_timeout_seconds=agent.runtime_config.tools.bash_timeout_seconds,
                bash_max_output_bytes=agent.runtime_config.tools.bash_max_output_bytes,
            ),
            prompts=config_module.PromptRuntimeConfig(order=list(agent.runtime_config.prompts.order)),
            capabilities=config_module.CapabilitiesConfig(
                prompts=config_module.PromptCapabilityConfig(paths=list(agent.runtime_config.capabilities.prompts.paths)),
                skills=config_module.SkillCapabilityConfig(paths=list(agent.runtime_config.capabilities.skills.paths)),
            ),
        )
        restored_snapshot = session_module.agent_snapshot_from_dict(snapshot_dict, runtime_after_code_reload)
        restored_cwd = Path(restored_snapshot.runtime.cwd or self.cwd)
        self.capability_runtime = runtime_module.CapabilityRuntime(restored_cwd)
        self.agent = agent_module.Agent(
            agent_module.AgentConfig(
                model=restored_snapshot.runtime.runtime_config.model,
                api_key=self.api_key,
                base_url=restored_snapshot.runtime.runtime_config.base_url,
                runtime_env=dict(self.runtime_env),
                cwd=restored_cwd,
                system_prompt=restored_snapshot.runtime.runtime_config.system_prompt,
            ),
            capability_runtime=self.capability_runtime,
            initial_snapshot=restored_snapshot,
        )
        runtime_result = self.reload_runtime()
        return AppActionResult(
            message="Code modules reloaded.",
            warnings=list(runtime_result.warnings),
            error=None if runtime_result.success else runtime_result.message,
        )

    def submit_prompt(
        self,
        text: str,
        *,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> AgentRunResult:
        agent = self._require_agent()
        self._set_default_session_name(text)
        result = agent.prompt(text, raw_input=text, on_event=on_event)
        self._persist_agent_state(create_if_needed=True)
        return result

    def arm_skill(self, name: str) -> AppActionResult:
        success, message = self._require_agent().arm_skill(name, f"/skill:{name}")
        persisted = self._persist_agent_state() if self._require_session_state().materialized else False
        return AppActionResult(message=message, error=None if success else message, persisted=persisted)

    def run_skill(
        self,
        name: str,
        request_text: str,
        *,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> AgentRunResult:
        result = self._require_agent().run_skill(
            name,
            request_text,
            f"/skill:{name} {request_text}",
            on_event=on_event,
        )
        self._persist_agent_state(create_if_needed=True)
        return result

    def run_template(
        self,
        name: str,
        request_text: str,
        *,
        on_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> AgentRunResult:
        result = self._require_agent().run_template(
            name,
            request_text,
            f"/template:{name} {request_text}",
            on_event=on_event,
        )
        self._persist_agent_state(create_if_needed=True)
        return result

    def list_sessions(self) -> list[SessionSummary]:
        return self.store.list()

    def list_resume_candidates(self) -> list[ResumeCandidate]:
        normalized_cwd = self._normalize_cwd(self.current_cwd())
        sessions = [session for session in self.store.list() if self._normalize_cwd(session.cwd) == normalized_cwd]
        return [
            ResumeCandidate(
                id=session.id,
                name=session.name,
                cwd=session.cwd,
                updated_at=session.updated_at,
                parent_session_id=session.parent_session_id,
            )
            for session in sessions
        ]

    def switch_session(self, session_id: str) -> AppActionResult:
        try:
            loaded_session = self.store.load_by_prefix(session_id)
        except ValueError as exc:
            message = str(exc)
            return AppActionResult(message=message, error=message)
        result = self._restore_session(loaded_session)
        if result.error:
            return result
        return AppActionResult(message=f"Switched to {self._require_session_state().session.id}")

    def resume_session(self, session_id: str) -> AppActionResult:
        try:
            loaded_session = self.store.load_by_prefix(session_id)
        except ValueError as exc:
            message = str(exc)
            return AppActionResult(message=message, error=message)
        result = self._restore_session(loaded_session)
        if result.error:
            return result
        session = self._require_session_state().session
        resumed_name = session.name or "(unnamed)"
        return AppActionResult(message=f"Resumed {resumed_name} ({session.id})")

    def fork_session(self, name: str | None = None) -> AppActionResult:
        session_state = self._require_session_state()
        if not session_state.materialized:
            message = "No saved session to fork."
            return AppActionResult(message=message, error=None)
        forked = self.store.fork(session_state.session.id, name=name)
        session_state.session = forked
        session_state.materialized = True
        session_state.runtime_snapshot_dirty = False
        return AppActionResult(message=f"Forked to {forked.id}")

    def rename_session(self, name: str) -> AppActionResult:
        session_state = self._require_session_state()
        if not session_state.materialized:
            return AppActionResult(message="No saved session to rename.")
        session_state.session.name = name
        persisted = self._persist_session_metadata()
        return AppActionResult(message=f"Renamed to {session_state.session.name}", persisted=persisted)

    def save_session(self) -> AppActionResult:
        session_state = self._require_session_state()
        if not session_state.materialized:
            return AppActionResult(message="No session to save.")
        self._persist_agent_state()
        return AppActionResult(message=f"Saved {session_state.session.id}", persisted=True)

    def autosave_session(self) -> bool:
        session_state = self._require_session_state()
        if not session_state.materialized or not session_state.needs_auto_save:
            return False
        return self._persist_agent_state()

    def _load_runtime_config(self, cwd: Path) -> tuple[ResolvedRuntimeConfig, list[str]]:
        warnings: list[str] = []
        resolved_cwd = cwd.resolve()
        try:
            env_map = merged_env(resolved_cwd, env=self._env_provider())
        except DotenvError as exc:
            warnings.append(str(exc))
            env_map = dict(self._env_provider())
        self.runtime_env = dict(env_map)
        self.api_key = self.runtime_env.get("OPENAI_API_KEY")
        try:
            raw_config = self.config_manager.load(resolved_cwd)
        except ConfigError as exc:
            warnings.append(str(exc))
            raw_config = RuntimeConfig()
        resolved = resolve_runtime_config(
            raw_config,
            self.options.model_override,
            self.options.base_url_override,
            self.options.system_prompt_override,
            env=self.runtime_env,
        )
        return resolved, warnings

    def _apply_runtime_env_to_agent(self, agent: Agent) -> None:
        agent.config.api_key = self.api_key
        agent.config.runtime_env = dict(self.runtime_env)

    def _persist_agent_state(self, create_if_needed: bool = False) -> bool:
        session_state = self._require_session_state()
        agent = self._require_agent()
        if not session_state.materialized and not create_if_needed:
            return False
        if not session_state.materialized and not agent.messages:
            return False
        apply_agent_snapshot_to_session(session_state.session, agent.snapshot())
        self.store.save(session_state.session)
        session_state.materialized = True
        session_state.needs_auto_save = False
        session_state.runtime_snapshot_dirty = False
        return True

    def _persist_session_metadata(self) -> bool:
        session_state = self._require_session_state()
        if not session_state.materialized:
            return False
        self.store.save(session_state.session)
        return True

    def _restore_session(self, session: Session) -> AppActionResult:
        agent = self._require_agent()
        session_state = self._require_session_state()
        snapshot_cwd = session.cwd
        if session.agent_snapshot is not None and session.agent_snapshot.runtime.cwd:
            snapshot_cwd = session.agent_snapshot.runtime.cwd
        restored_cwd = Path(snapshot_cwd).resolve()
        restored_runtime_config, warnings = self._load_runtime_config(restored_cwd)
        snapshot = session_to_agent_snapshot(session, restored_runtime_config)
        snapshot.runtime.cwd = str(restored_cwd)
        agent.restore(snapshot)
        self.cwd = restored_cwd
        self.capability_runtime = agent.runtime
        self._apply_runtime_env_to_agent(agent)
        resumed_runtime = clone_resolved_runtime_config(snapshot.runtime.runtime_config)
        result = agent.apply_runtime_config(resumed_runtime)
        combined_warnings = list(warnings)
        combined_warnings.extend(result.warnings)
        if not result.success:
            message = f"Failed to restore session: {result.message}"
            return AppActionResult(message=message, warnings=combined_warnings, error=message)
        session_state.session = session
        session_state.materialized = True
        session_state.needs_auto_save = False
        session_state.runtime_snapshot_dirty = False
        return AppActionResult(message=f"Restored {session.id}", warnings=combined_warnings)

    def _set_default_session_name(self, text: str) -> None:
        session_state = self._require_session_state()
        if session_state.materialized:
            return
        if (session_state.session.name or "").strip():
            return
        normalized = text.strip()
        if normalized:
            session_state.session.name = normalized

    def _normalize_cwd(self, raw_path: str | Path) -> str:
        try:
            return str(Path(raw_path).resolve())
        except OSError:
            return str(raw_path)

    def _require_agent(self) -> Agent:
        if self.agent is None:
            raise RuntimeError("Application has not been started")
        return self.agent

    def _require_session_state(self) -> AppSessionState:
        if self.session_state is None:
            raise RuntimeError("Application has not been started")
        return self.session_state
