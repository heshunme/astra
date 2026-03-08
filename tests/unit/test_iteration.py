from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from astra.iteration import GateSpec, IterationExecutor, IterationRunRecord


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


def test_iteration_auto_accepts_after_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace)
    _ensure_local_python(workspace)
    executor = IterationExecutor(workspace)

    monkeypatch.setattr(
        executor,
        "_default_gates",
        lambda _python: [GateSpec(name="compileall", command=[sys.executable, "-c", "print('ok')"])],
    )

    seen_steps: list[int] = []

    def attempt(step: int) -> str | None:
        seen_steps.append(step)
        if step == 1:
            return "step 1 failed"
        return None

    record = executor.run_auto(session_id="s-5", iterate_fn=attempt, objective="stabilize loop")

    assert seen_steps == [1, 2]
    assert record.final_decision == "accepted"
    assert record.loop_final_decision == "accepted"
    assert record.loop_stop_reason == "accepted"
    assert record.loop_step == 2
    assert record.loop_id is not None
    assert record.objective == "stabilize loop"


def test_iteration_auto_stops_on_max_reverts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace)
    _ensure_local_python(workspace)
    executor = IterationExecutor(workspace)

    monkeypatch.setattr(
        executor,
        "_default_gates",
        lambda _python: [GateSpec(name="compileall", command=[sys.executable, "-c", "print('ok')"])],
    )

    def attempt(_step: int) -> str | None:
        return "always fail"

    record = executor.run_auto(session_id="s-6", iterate_fn=attempt, max_steps=5, max_reverts=2)

    assert record.final_decision == "reverted"
    assert record.loop_final_decision == "failed"
    assert record.loop_stop_reason == "max_reverts"
    assert record.loop_step == 2


def test_iteration_auto_fails_fast_on_env_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _init_repo(workspace)
    # Intentionally skip local python setup to trigger env failure.
    executor = IterationExecutor(workspace)

    called = False

    def attempt(_step: int) -> str | None:
        nonlocal called
        called = True
        return None

    record = executor.run_auto(session_id="s-7", iterate_fn=attempt)

    assert called is False
    assert record.final_decision == "failed"
    assert record.failure_class == "env"
    assert record.loop_final_decision == "failed"
    assert record.loop_stop_reason == "env_failure"
    assert record.loop_step == 1


def test_load_benchmark_tasks_skips_invalid_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    tasks_file = workspace / ".astra" / "benchmarks" / "tasks.yaml"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(
        """
tasks:
  - id: ok-task
    objective: Keep it green
  - id: ""
    objective: missing id
  - id: bad-tags
    objective: wrong tags
    tags: invalid
  - id: ok-task
    objective: duplicate id
""".strip(),
        encoding="utf-8",
    )

    executor = IterationExecutor(workspace)
    tasks, warnings = executor.load_benchmark_tasks(task_path=tasks_file)

    assert [task.id for task in tasks] == ["ok-task"]
    assert len(warnings) == 3
    assert any("tasks[2].id" in warning for warning in warnings)
    assert any("tasks[3].tags" in warning for warning in warnings)
    assert any("duplicated" in warning for warning in warnings)


def test_run_benchmark_aggregates_results_and_persists_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    tasks_file = workspace / ".astra" / "benchmarks" / "tasks.yaml"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(
        """
tasks:
  - id: t-pass
    objective: pass objective
    max_steps: 2
  - id: t-fail
    objective: fail objective
    max_steps: 1
""".strip(),
        encoding="utf-8",
    )

    executor = IterationExecutor(workspace)
    seen: list[str] = []

    def fake_run_auto(
        self, *, session_id: str, iterate_fn, objective=None, max_steps=3, max_reverts=2, max_total_seconds=900
    ) -> IterationRunRecord:  # type: ignore[no-untyped-def]
        if objective == "pass objective":
            status: str = "accepted"
            failure_class = None
            loop_stop_reason: str = "accepted"
            score = 1.0
        else:
            status = "reverted"
            failure_class = "test"
            loop_stop_reason = "max_reverts"
            score = 0.25
        run_id = f"run-{len(seen) + 1}"
        seen.append(run_id)
        return IterationRunRecord(
            run_id=run_id,
            session_id=session_id,
            checkpoint_id=f"ckpt-{len(seen)}",
            final_decision=status,  # type: ignore[arg-type]
            score=score,
            failure_class=failure_class,  # type: ignore[arg-type]
            duration_seconds=0.5,
            objective=objective,
            loop_id=f"loop-{len(seen)}",
            loop_step=1,
            loop_final_decision="accepted" if status == "accepted" else "failed",
            loop_stop_reason=loop_stop_reason,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(IterationExecutor, "run_auto", fake_run_auto)
    record = executor.run_benchmark(
        session_id="s-bench",
        iterate_fn=lambda _task, _step: None,
        task_path=tasks_file,
    )

    assert record.total_tasks == 2
    assert record.passed_tasks == 1
    assert record.accept_rate == 0.5
    assert record.avg_score == pytest.approx(0.625)
    assert record.failure_breakdown == {"test": 1}
    assert len(record.task_results) == 2
    assert executor.last_benchmark_record() is not None
