from __future__ import annotations

import builtins
import io
import sys
from pathlib import Path

import pytest

from astra import cli
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


def test_runtime_json_prompt_command(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    monkeypatch.setattr(builtins, "input", InputFeeder(["/runtime json prompt", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "Session " in out
    assert '"prompt"' in out
    assert '"fragment_count"' in out


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
