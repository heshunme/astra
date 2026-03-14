from __future__ import annotations

import builtins
import io
import sys
import time
import json
from pathlib import Path

import pytest

from astra import cli
from astra.models import ProviderEvent
from astra.provider import OpenAICompatibleProvider
from astra.session import SessionStore


pytestmark = pytest.mark.integration


class InputFeeder:
    def __init__(self, lines: list[str]):
        self._lines = iter(lines)

    def __call__(self, _prompt: str = "") -> str:
        try:
            return next(self._lines)
        except StopIteration as exc:
            raise EOFError from exc


class FallbackStdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


def _saved_session_files(home: Path) -> list[Path]:
    return list((home / ".astra-python" / "sessions").glob("*.json"))


def test_runtime_json_prompt_command(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    monkeypatch.setattr(builtins, "input", InputFeeder(["/runtime json prompt", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "Session (new)" in out
    assert '"prompt"' in out
    assert '"fragment_count"' in out
    assert _saved_session_files(tmp_path) == []


def test_model_and_base_url_commands(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    monkeypatch.setattr(
        builtins,
        "input",
        InputFeeder(["/model custom-model", "/base-url http://gateway/v1", "/tools", "/exit"]),
    )
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "Model set to custom-model" in out
    assert "Base URL set to http://gateway/v1" in out
    assert "Tools summary" in out
    assert "tools=read, write, edit, ls, find, grep, bash" in out
    assert "read.max_lines=400" in out
    assert "bash.timeout_seconds=60" in out
    assert "bash.max_output_bytes=32768" in out
    assert "model=custom-model" not in out
    assert "base_url=http://gateway/v1" not in out
    assert _saved_session_files(tmp_path) == []


def test_switch_command(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    store = SessionStore()
    first = store.create(cwd=str(cwd), model="m-1", system_prompt="s-1")
    second = store.create(cwd=str(cwd), model="m-2", system_prompt="s-2")
    store.save(first)
    store.save(second)

    monkeypatch.setattr(builtins, "input", InputFeeder([f"/switch {second.id}", "/exit"]))
    cli.main(["--cwd", str(cwd), "--session", first.id])

    out = capsys.readouterr().out
    assert f"Switched to {second.id}" in out


def test_resume_command(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    store = SessionStore()
    first = store.create(cwd=str(cwd), model="m-1", system_prompt="s-1", name="alpha")
    second = store.create(cwd=str(cwd), model="m-2", system_prompt="s-2", name="beta")
    store.save(first)
    time.sleep(0.01)
    store.save(second)

    monkeypatch.setattr(builtins, "input", InputFeeder(["/resume", "1", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "Sessions" in out
    assert "1. beta" in out
    assert "2. alpha" in out
    assert f"Resumed beta ({second.id})" in out
    assert "Runtime config" in out
    assert "model=m-2" in out
    assert "base_url=https://api.openai.com/v1" in out
    assert "tools=read, write, edit, ls, find, grep, bash" in out
    assert "s-2" not in out


def test_resume_command_rejects_invalid_selection(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    store = SessionStore()
    session = store.create(cwd=str(cwd), model="m-1", system_prompt="s-1", name="alpha")
    store.save(session)

    monkeypatch.setattr(builtins, "input", InputFeeder(["/resume", "nope", "/sessions", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "1. alpha" in out
    assert "Invalid session number." in out
    assert "* current session" not in out


def test_resume_command_with_no_sessions(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    monkeypatch.setattr(builtins, "input", InputFeeder(["/resume", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "No sessions" in out
    assert _saved_session_files(tmp_path) == []


def test_resume_command_filters_sessions_to_current_cwd(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "workspace"
    other_cwd = tmp_path / "other-workspace"
    cwd.mkdir()
    other_cwd.mkdir()

    store = SessionStore()
    matching = store.create(cwd=str(cwd), model="m-1", system_prompt="s-1", name="alpha")
    other = store.create(cwd=str(other_cwd), model="m-2", system_prompt="s-2", name="beta")
    store.save(matching)
    time.sleep(0.01)
    store.save(other)

    monkeypatch.setattr(builtins, "input", InputFeeder(["/resume", "1", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "1. alpha" in out
    assert "beta" not in out
    assert f"Resumed alpha ({matching.id})" in out
    assert f"({other.id})" not in out


def test_save_rename_and_fork_require_saved_session(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    monkeypatch.setattr(builtins, "input", InputFeeder(["/save", "/rename demo", "/fork", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "No session to save." in out
    assert "No saved session to rename." in out
    assert "No saved session to fork." in out
    assert _saved_session_files(tmp_path) == []


def test_plain_message_creates_session(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    def fake_stream_chat(self, _request):
        yield ProviderEvent(type="text_delta", delta="ok")
        yield ProviderEvent(type="done")

    monkeypatch.setattr(OpenAICompatibleProvider, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(builtins, "input", InputFeeder(["hello", "/exit"]))

    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "ok" in out
    saved_sessions = _saved_session_files(tmp_path)
    assert len(saved_sessions) == 1
    data = json.loads(saved_sessions[0].read_text(encoding="utf-8"))
    assert data["name"] == "hello"


def test_sessions_command_shows_first_prompt_as_default_name(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    def fake_stream_chat(self, _request):
        yield ProviderEvent(type="text_delta", delta="ok")
        yield ProviderEvent(type="done")

    monkeypatch.setattr(OpenAICompatibleProvider, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(builtins, "input", InputFeeder(["hello", "/sessions", "/exit"]))

    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "Sessions" in out
    assert "hello" in out
    assert "(unnamed)" not in out


def test_cli_recovers_from_unicode_decode_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    class BrokenInputOnce:
        _called = False

        def __call__(self, _prompt: str = "") -> str:
            if not self._called:
                self._called = True
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            raise EOFError

    monkeypatch.setattr(builtins, "input", BrokenInputOnce())
    monkeypatch.setattr(sys, "stdin", FallbackStdin(b"/exit\n"))

    cli.main(["--cwd", str(cwd)])

    captured = capsys.readouterr()
    assert "Warning: stdin contains non-UTF-8 bytes" in captured.err
    assert _saved_session_files(tmp_path) == []
