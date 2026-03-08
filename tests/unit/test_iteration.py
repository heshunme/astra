from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from astra.iteration import GateSpec, IterationExecutor


pytestmark = pytest.mark.unit


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], cwd=workspace)
    _run(["git", "config", "user.email", "astra-tests@example.com"], cwd=workspace)
    _run(["git", "config", "user.name", "Astra Tests"], cwd=workspace)
    (workspace / ".gitignore").write_text(".venv\n.astra/logs/\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run(["git", "add", ".gitignore", "tracked.txt"], cwd=workspace)
    _run(["git", "commit", "-m", "init"], cwd=workspace)


def _ensure_local_python(workspace: Path) -> None:
    venv_bin = workspace / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    python_link = venv_bin / "python"
    if python_link.exists():
        return
    os.symlink(sys.executable, python_link)


def test_iteration_reverts_on_attempt_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace)
    _ensure_local_python(workspace)
    executor = IterationExecutor(workspace)

    monkeypatch.setattr(
        executor,
        "_default_gates",
        lambda _python: [GateSpec(name="compileall", command=[sys.executable, "-c", "print('ok')"])],
    )

    def attempt() -> str | None:
        (workspace / "tracked.txt").write_text("mutated\n", encoding="utf-8")
        (workspace / "new_file.txt").write_text("temp\n", encoding="utf-8")
        return "attempt failed"

    record = executor.run_once(session_id="s-1", iterate_fn=attempt)

    assert record.final_decision == "reverted"
    assert record.failure_class == "unknown"
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert not (workspace / "new_file.txt").exists()
    assert executor.last_record() is not None


def test_iteration_reverts_when_first_gate_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace)
    _ensure_local_python(workspace)
    executor = IterationExecutor(workspace)

    monkeypatch.setattr(
        executor,
        "_default_gates",
        lambda _python: [
            GateSpec(name="compileall", command=[sys.executable, "-c", "import sys; sys.exit(1)"]),
            GateSpec(name="unit_tests", command=[sys.executable, "-c", "print('should not run')"]),
        ],
    )

    def attempt() -> str | None:
        (workspace / "tracked.txt").write_text("mutated\n", encoding="utf-8")
        return None

    record = executor.run_once(session_id="s-2", iterate_fn=attempt)

    assert record.final_decision == "reverted"
    assert record.failure_class == "syntax"
    assert len(record.gate_results) == 1
    assert record.gate_results[0].name == "compileall"
    assert record.gate_results[0].status == "failed"
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "base\n"


def test_iteration_fails_on_dirty_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace)
    _ensure_local_python(workspace)
    (workspace / "scratch.txt").write_text("dirty\n", encoding="utf-8")

    executor = IterationExecutor(workspace)
    record = executor.run_once(session_id="s-3", iterate_fn=lambda: None)

    assert record.final_decision == "failed"
    assert record.failure_class == "env"
    assert "dirty" in (record.error or "")


def test_iteration_ignores_astra_log_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace)
    _ensure_local_python(workspace)
    log_file = workspace / ".astra" / "logs" / "iteration_runs.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("{}\n", encoding="utf-8")

    executor = IterationExecutor(workspace)
    monkeypatch.setattr(
        executor,
        "_default_gates",
        lambda _python: [GateSpec(name="compileall", command=[sys.executable, "-c", "print('ok')"])],
    )

    record = executor.run_once(session_id="s-4", iterate_fn=lambda: None)

    assert record.final_decision == "accepted"
