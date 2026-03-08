from __future__ import annotations

import builtins
import io
import json
import sys
from pathlib import Path

import pytest

from astra import cli
from astra.iteration import BenchmarkRunRecord, BenchmarkTaskResult, IterationRunRecord
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


def test_iterate_status_and_runtime_json_include_iteration(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    monkeypatch.setattr(builtins, "input", InputFeeder(["/iterate status", "/runtime json", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "No iteration runs" in out
    assert '"iteration"' in out
    assert '"last_run_id": null' in out


def test_iterate_auto_command_uses_bounded_loop(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    def fake_run_auto(self, *, session_id: str, iterate_fn, objective=None, **_kwargs):  # type: ignore[no-untyped-def]
        return IterationRunRecord(
            run_id="run-auto",
            session_id=session_id,
            checkpoint_id="ckpt-auto",
            final_decision="accepted",
            score=1.0,
            objective=objective,
            loop_id="loop-123",
            loop_step=1,
            loop_final_decision="accepted",
            loop_stop_reason="accepted",
        )

    monkeypatch.setattr(cli.IterationExecutor, "run_auto", fake_run_auto)
    monkeypatch.setattr(builtins, "input", InputFeeder(["/iterate auto tighten checks", "/runtime json", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "Iteration status" in out
    assert "loop_final_decision=accepted" in out
    assert '"last_loop_id": "loop-123"' in out
    assert '"last_loop_decision": "accepted"' in out


def test_runtime_json_includes_loop_fields_from_persisted_record(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    log_file = cwd / ".astra" / "logs" / "iteration_runs.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "session_id": "s-1",
                "checkpoint_id": "ckpt-1",
                "final_decision": "reverted",
                "score": 0.33,
                "changed_files": [],
                "gate_results": [],
                "failure_class": "test",
                "duration_seconds": 0.2,
                "objective": "ship fix",
                "loop_id": "loop-9",
                "loop_step": 3,
                "loop_final_decision": "failed",
                "loop_stop_reason": "max_steps",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(builtins, "input", InputFeeder(["/iterate status", "/runtime json", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "loop_id=loop-9" in out
    assert "loop_stop_reason=max_steps" in out
    assert '"last_loop_id": "loop-9"' in out
    assert '"last_loop_step": 3' in out
    assert '"last_loop_stop_reason": "max_steps"' in out


def test_iterate_benchmark_command_updates_runtime_json(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    def fake_run_benchmark(self, *, session_id: str, iterate_fn, task_path=None):  # type: ignore[no-untyped-def]
        return BenchmarkRunRecord(
            benchmark_run_id="bench-1",
            session_id=session_id,
            task_source=".astra/benchmarks/tasks.yaml",
            total_tasks=1,
            passed_tasks=1,
            accept_rate=1.0,
            avg_score=1.0,
            avg_duration_seconds=0.2,
            duration_seconds=0.2,
            task_results=[
                BenchmarkTaskResult(
                    task_id="t1",
                    objective="tighten checks",
                    status="passed",
                    run_id="run-1",
                    final_decision="accepted",
                    score=1.0,
                    duration_seconds=0.2,
                    loop_stop_reason="accepted",
                )
            ],
            failure_breakdown={},
            warnings=[],
        )

    monkeypatch.setattr(cli.IterationExecutor, "run_benchmark", fake_run_benchmark)
    monkeypatch.setattr(
        builtins,
        "input",
        InputFeeder(["/iterate benchmark", "/runtime json", "/exit"]),
    )
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert "Benchmark status" in out
    assert "accept_rate=1.000" in out
    assert '"benchmark"' in out
    assert '"last_run_id": "bench-1"' in out
    assert '"last_accept_rate": 1.0' in out


def test_runtime_json_includes_benchmark_fields_from_persisted_record(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    benchmark_log_file = cwd / ".astra" / "logs" / "iteration_benchmarks.jsonl"
    benchmark_log_file.parent.mkdir(parents=True, exist_ok=True)
    benchmark_log_file.write_text(
        json.dumps(
            {
                "benchmark_run_id": "bench-77",
                "session_id": "s-1",
                "task_source": ".astra/benchmarks/tasks.yaml",
                "total_tasks": 2,
                "passed_tasks": 1,
                "accept_rate": 0.5,
                "avg_score": 0.6,
                "avg_duration_seconds": 1.2,
                "duration_seconds": 2.4,
                "failure_breakdown": {"test": 1},
                "warnings": ["tasks[2] skipped"],
                "task_results": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(builtins, "input", InputFeeder(["/runtime json", "/exit"]))
    cli.main(["--cwd", str(cwd)])

    out = capsys.readouterr().out
    assert '"benchmark"' in out
    assert '"last_run_id": "bench-77"' in out
    assert '"last_total_tasks": 2' in out
    assert '"last_accept_rate": 0.5' in out
    assert '"last_failure_breakdown": {' in out


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
