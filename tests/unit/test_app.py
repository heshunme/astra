from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from astra.app import AstraApp, AstraAppOptions
from astra.config import ConfigError, ReloadResult, ResolvedRuntimeConfig, RuntimeConfig, ToolRuntimeConfig, PromptRuntimeConfig, CapabilitiesConfig
from astra.models import AgentConversationState, AgentRunResult, AgentRuntimeState, AgentSnapshot, Message, Session, SessionSummary
from astra.runtime.runtime import PromptInspection, PromptInspectionFragment


pytestmark = pytest.mark.unit


@dataclass(slots=True)
class _StoredSessionRecord:
    session: Session


class FakeSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self.saved_ids: list[str] = []
        self.created: list[Session] = []
        self.forked_from: list[str] = []

    def create(self, cwd: str, model: str, system_prompt: str, name: str | None = None) -> Session:
        session_id = f"session-{len(self.created) + 1}"
        session = Session(
            id=session_id,
            name=name,
            cwd=cwd,
            created_at="created",
            updated_at="updated",
            model=model,
            system_prompt=system_prompt,
            messages=[],
        )
        self.created.append(session)
        return session

    def load(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def save(self, session: Session) -> None:
        session.updated_at = f"saved-{len(self.saved_ids) + 1}"
        self._sessions[session.id] = session
        self.saved_ids.append(session.id)

    def list(self) -> list[SessionSummary]:
        return [
            SessionSummary(
                id=session.id,
                name=session.name,
                cwd=session.cwd,
                updated_at=session.updated_at,
                parent_session_id=session.parent_session_id,
            )
            for session in self._sessions.values()
        ]

    def fork(self, session_id: str, name: str | None = None) -> Session:
        source = self._sessions[session_id]
        forked = Session(
            id=f"fork-{len(self.forked_from) + 1}",
            name=name,
            cwd=source.cwd,
            created_at="created",
            updated_at="updated",
            model=source.model,
            system_prompt=source.system_prompt,
            messages=list(source.messages),
            skill_catalog_snapshot=list(source.skill_catalog_snapshot),
            parent_session_id=source.id,
            agent_snapshot=source.agent_snapshot,
        )
        self._sessions[forked.id] = forked
        self.forked_from.append(source.id)
        return forked


class FakeConfigManager:
    def __init__(self, config: RuntimeConfig | None = None, error: Exception | None = None) -> None:
        self._config = config or RuntimeConfig()
        self._error = error
        self.load_calls: list[Path] = []

    def load(self, cwd: Path) -> RuntimeConfig:
        self.load_calls.append(cwd)
        if self._error is not None:
            raise self._error
        return self._config


class FakeCapabilityRuntime:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.template_names: list[str] = []

    def list_template_names(self) -> list[str]:
        return list(self.template_names)


class FakeAgent:
    def __init__(self, config, runtime) -> None:
        self.config = config
        self.runtime = runtime
        self.runtime_state = AgentRuntimeState(cwd=str(config.cwd), runtime_config=_runtime_config(config))
        self.messages: list[Message] = []
        self.apply_calls: list[object] = []
        self.restore_calls: list[AgentSnapshot] = []
        self.prompt_calls: list[tuple[str, str | None]] = []
        self.arm_skill_calls: list[tuple[str, str]] = []
        self.run_skill_calls: list[tuple[str, str, str]] = []
        self.run_template_calls: list[tuple[str, str, str]] = []
        self.is_streaming = False
        self.available_skills_value: list[object] = []
        self.inspect_runtime_value: dict[str, object] = {
            "model": config.model,
            "base_url": config.base_url,
            "tools": ["read", "write"],
            "prompts": {"order": ["builtin:base"], "available": ["builtin:base"], "loaded": []},
            "skills": {"available": [], "history_only": [], "pending": None, "loaded": [], "entries": [], "conflicts": []},
            "templates": {"available": []},
            "tool_defaults": {"read_max_lines": 400, "bash_timeout_seconds": 60, "bash_max_output_bytes": 32768},
            "warnings": [],
        }
        self.inspect_prompt_value = PromptInspection(
            assembled="assembled prompt",
            fragments=[PromptInspectionFragment(key="builtin:base", source="builtin", text_length=16)],
        )
        self.prompt_fragment_map = {"builtin:base": "assembled prompt"}
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
        self.is_streaming = False

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        return not self.is_streaming

    def apply_runtime_config(self, runtime_config):
        self.apply_calls.append(runtime_config)
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
            warnings=["runtime warning"],
        )

    def set_model(self, model: str) -> None:
        self.config.model = model
        self.runtime_state.runtime_config.model = model

    def set_base_url(self, base_url: str) -> None:
        self.config.base_url = base_url
        self.runtime_state.runtime_config.base_url = base_url

    def inspect_runtime(self) -> dict[str, object]:
        payload = dict(self.inspect_runtime_value)
        payload["model"] = self.config.model
        payload["base_url"] = self.config.base_url
        return payload

    def inspect_prompt(self) -> PromptInspection:
        return self.inspect_prompt_value

    def prompt_fragment_text(self, key: str) -> str:
        return self.prompt_fragment_map.get(key, "")

    def prompt(self, text: str, *, raw_input: str | None = None, metadata=None, on_event=None) -> AgentRunResult:
        self.prompt_calls.append((text, raw_input))
        self.messages.append(Message(role="user", content=text, metadata=metadata or {}))
        self.snapshot_value.conversation.messages = list(self.messages)
        return AgentRunResult(assistant_messages=[], tool_results=[], error=None)

    def arm_skill(self, name: str, raw_command: str):
        self.arm_skill_calls.append((name, raw_command))
        return True, f"Next message will use skill: {name}"

    def run_skill(self, name: str, request_text: str, raw_command: str):
        self.run_skill_calls.append((name, request_text, raw_command))
        self.messages.append(Message(role="user", content=f"skill:{name}:{request_text}"))
        self.snapshot_value.conversation.messages = list(self.messages)
        return AgentRunResult(assistant_messages=[], tool_results=[], error=None)

    def run_template(self, name: str, request_text: str, raw_command: str):
        self.run_template_calls.append((name, request_text, raw_command))
        self.messages.append(Message(role="user", content=f"template:{name}:{request_text}"))
        self.snapshot_value.conversation.messages = list(self.messages)
        return AgentRunResult(assistant_messages=[], tool_results=[], error=None)

    def available_skills(self) -> list[object]:
        return list(self.available_skills_value)

    def snapshot(self) -> AgentSnapshot:
        self.snapshot_value.conversation.messages = list(self.messages)
        self.snapshot_value.runtime.cwd = self.runtime_state.cwd
        self.snapshot_value.runtime.runtime_config = self.runtime_state.runtime_config
        return self.snapshot_value

    def restore(self, snapshot: AgentSnapshot) -> None:
        self.restore_calls.append(snapshot)
        self.snapshot_value = snapshot
        self.runtime_state = snapshot.runtime
        self.messages = list(snapshot.conversation.messages)
        self.config.model = snapshot.runtime.runtime_config.model
        self.config.base_url = snapshot.runtime.runtime_config.base_url
        self.config.system_prompt = snapshot.runtime.runtime_config.system_prompt


def _runtime_config(config) -> object:
    return ResolvedRuntimeConfig(
        model=config.model,
        base_url=config.base_url,
        system_prompt=config.system_prompt,
        tools=ToolRuntimeConfig(enabled_tools=["read", "write"], read_max_lines=400, bash_timeout_seconds=60, bash_max_output_bytes=32768),
        prompts=PromptRuntimeConfig(order=["builtin:base"]),
        capabilities=CapabilitiesConfig(),
    )


def _make_app(
    tmp_path: Path,
    *,
    config: RuntimeConfig | None = None,
    config_error: Exception | None = None,
    env: dict[str, str] | None = None,
    session_id: str | None = None,
    new_session: bool = False,
) -> tuple[AstraApp, FakeSessionStore]:
    store = FakeSessionStore()
    config_manager = FakeConfigManager(config=config, error=config_error)
    app = AstraApp(
        AstraAppOptions(cwd=tmp_path / "workspace", session_id=session_id, new_session=new_session),
        agent_factory=FakeAgent,
        runtime_factory=FakeCapabilityRuntime,
        session_store_factory=lambda: store,
        config_manager_factory=lambda: config_manager,
        env_provider=lambda: dict(env or {"OPENAI_API_KEY": "test-key"}),
    )
    (tmp_path / "workspace").mkdir()
    return app, store


def test_app_startup_applies_runtime_and_collects_warnings(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path, config_error=ConfigError("broken config"))

    result = app.startup()

    assert result.message == "Started application."
    assert "broken config" in result.warnings
    assert "runtime warning" in result.warnings
    assert app.current_session_id() is None
    assert store.created[0].cwd == str((tmp_path / "workspace").resolve())
    assert len(app.agent.apply_calls) == 1


def test_app_startup_requires_api_key(tmp_path: Path) -> None:
    app, _store = _make_app(tmp_path, env={"PATH": "/usr/bin"})

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        app.startup()


def test_submit_prompt_materializes_session_and_sets_default_name(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()

    result = app.submit_prompt("hello")

    assert result.error is None
    assert app.has_materialized_session()
    assert app.current_session_id() == "session-1"
    assert app.current_session_name() == "hello"
    assert store.saved_ids == ["session-1"]


def test_arm_skill_does_not_materialize_unsaved_session(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()

    result = app.arm_skill("review")

    assert result.message == "Next message will use skill: review"
    assert result.persisted is False
    assert app.current_session_id() is None
    assert store.saved_ids == []
    assert app.agent.arm_skill_calls == [("review", "/skill:review")]


def test_arm_skill_persists_when_session_already_materialized(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()
    app.submit_prompt("hello")
    store.saved_ids.clear()

    result = app.arm_skill("review")

    assert result.persisted is True
    assert store.saved_ids == ["session-1"]


def test_set_model_and_base_url_do_not_materialize_unsaved_session(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()

    model_result = app.set_model("custom-model")
    base_url_result = app.set_base_url("http://gateway/v1")

    assert model_result.persisted is False
    assert base_url_result.persisted is False
    assert store.saved_ids == []
    assert app.get_model() == "custom-model"
    assert app.get_base_url() == "http://gateway/v1"


def test_run_skill_and_template_materialize_session(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()

    app.run_skill("review", "Check file")
    app.run_template("repo-rules", "Review file")

    assert store.saved_ids == ["session-1", "session-1"]
    assert app.agent.run_skill_calls == [("review", "Check file", "/skill:review Check file")]
    assert app.agent.run_template_calls == [("repo-rules", "Review file", "/template:repo-rules Review file")]


def test_reload_runtime_merges_config_and_runtime_warnings_and_persists_saved_session(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path, config_error=ConfigError("broken config"))
    app.startup()
    app.submit_prompt("hello")
    store.saved_ids.clear()

    result = app.reload_runtime()

    assert result.success is True
    assert "broken config" in result.warnings
    assert "runtime warning" in result.warnings
    assert store.saved_ids == ["session-1"]


def test_switch_and_resume_restore_saved_session(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()
    session = store.create(cwd=str((tmp_path / "workspace").resolve()), model="saved-model", system_prompt="saved-prompt", name="saved")
    session.agent_snapshot = AgentSnapshot(
        conversation=AgentConversationState(messages=[Message(role="user", content="saved")]),
        runtime=AgentRuntimeState(cwd=session.cwd, runtime_config=_runtime_config(SimpleNamespace(model="saved-model", base_url="http://saved/v1", system_prompt="saved-prompt"))),
    )
    store.save(session)

    switch_result = app.switch_session(session.id)
    resume_result = app.resume_session(session.id)

    assert switch_result.message == f"Switched to {session.id}"
    assert resume_result.message == f"Resumed saved ({session.id})"
    assert len(app.agent.restore_calls) == 2
    assert len(app.agent.apply_calls) == 3


def test_fork_rename_and_save_require_materialized_session(tmp_path: Path) -> None:
    app, _store = _make_app(tmp_path)
    app.startup()

    assert app.fork_session().message == "No saved session to fork."
    assert app.rename_session("demo").message == "No saved session to rename."
    assert app.save_session().message == "No session to save."


def test_fork_rename_and_save_work_for_materialized_session(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()
    app.submit_prompt("hello")
    store.saved_ids.clear()

    rename_result = app.rename_session("demo")
    save_result = app.save_session()
    fork_result = app.fork_session("child")

    assert rename_result.persisted is True
    assert save_result.persisted is True
    assert fork_result.message == "Forked to fork-1"
    assert store.forked_from == ["session-1"]
    assert app.current_session_id() == "fork-1"


def test_list_resume_candidates_filters_by_current_cwd(tmp_path: Path) -> None:
    app, store = _make_app(tmp_path)
    app.startup()
    current_cwd = str((tmp_path / "workspace").resolve())
    other_cwd = str((tmp_path / "other").resolve())
    session_one = store.create(cwd=current_cwd, model="m1", system_prompt="s1", name="one")
    session_two = store.create(cwd=other_cwd, model="m2", system_prompt="s2", name="two")
    store.save(session_one)
    store.save(session_two)

    candidates = app.list_resume_candidates()

    assert [candidate.name for candidate in candidates] == ["one"]


def test_help_and_runtime_prompt_accessors_use_typed_api(tmp_path: Path) -> None:
    app, _store = _make_app(tmp_path)
    app.startup()

    help_entries = app.help_entries()
    prompt_summary = app.get_runtime_prompt_summary()

    assert any(entry.usage == "/skill:<name> [request]" for entry in help_entries)
    assert any(entry.usage == "/template:<name> <request>" for entry in help_entries)
    assert prompt_summary.assembled == "assembled prompt"
    assert app.prompt_fragment_text("builtin:base") == "assembled prompt"


def test_reload_code_rejects_streaming_agent(tmp_path: Path) -> None:
    app, _store = _make_app(tmp_path)
    app.startup()
    app.agent.is_streaming = True

    result = app.reload_code()

    assert result.error == "Cannot reload while a response is streaming."
