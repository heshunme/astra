from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping, cast

import yaml


FailureClass = Literal["syntax", "test", "cli", "env", "timeout", "unknown"]
Decision = Literal["accepted", "reverted", "failed"]
LoopDecision = Literal["accepted", "failed"]
LoopStopReason = Literal["accepted", "max_steps", "max_reverts", "max_total_seconds", "env_failure"]
BenchmarkTaskStatus = Literal["passed", "failed"]

DEFAULT_BENCHMARK_TASK_PATH = ".astra/benchmarks/tasks.yaml"


@dataclass(slots=True)
class GateSpec:
    name: str
    command: list[str]
    timeout_seconds: int = 300


@dataclass(slots=True)
class GateResult:
    name: str
    status: Literal["passed", "failed", "error", "timeout"]
    duration_seconds: float
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass(slots=True)
class IterationRunRecord:
    run_id: str
    session_id: str
    checkpoint_id: str
    final_decision: Decision
    score: float
    changed_files: list[str] = field(default_factory=list)
    gate_results: list[GateResult] = field(default_factory=list)
    failure_class: FailureClass | None = None
    error: str | None = None
    duration_seconds: float = 0.0
    objective: str | None = None
    loop_id: str | None = None
    loop_step: int | None = None
    loop_final_decision: LoopDecision | None = None
    loop_stop_reason: LoopStopReason | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "checkpoint_id": self.checkpoint_id,
            "final_decision": self.final_decision,
            "score": self.score,
            "changed_files": list(self.changed_files),
            "gate_results": [
                {
                    "name": gate.name,
                    "status": gate.status,
                    "duration_seconds": gate.duration_seconds,
                    "exit_code": gate.exit_code,
                    "stdout_tail": gate.stdout_tail,
                    "stderr_tail": gate.stderr_tail,
                }
                for gate in self.gate_results
            ],
            "failure_class": self.failure_class,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "objective": self.objective,
            "loop_id": self.loop_id,
            "loop_step": self.loop_step,
            "loop_final_decision": self.loop_final_decision,
            "loop_stop_reason": self.loop_stop_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> IterationRunRecord:
        gate_results: list[GateResult] = []
        for item in raw.get("gate_results", []):
            if not isinstance(item, dict):
                continue
            gate_results.append(
                GateResult(
                    name=str(item.get("name", "")),
                    status=str(item.get("status", "error")),  # type: ignore[arg-type]
                    duration_seconds=float(item.get("duration_seconds", 0.0)),
                    exit_code=item.get("exit_code") if isinstance(item.get("exit_code"), int) else None,
                    stdout_tail=str(item.get("stdout_tail", "")),
                    stderr_tail=str(item.get("stderr_tail", "")),
                )
            )
        return cls(
            run_id=str(raw.get("run_id", "")),
            session_id=str(raw.get("session_id", "")),
            checkpoint_id=str(raw.get("checkpoint_id", "")),
            final_decision=str(raw.get("final_decision", "failed")),  # type: ignore[arg-type]
            score=float(raw.get("score", 0.0)),
            changed_files=[str(item) for item in raw.get("changed_files", []) if isinstance(item, str)],
            gate_results=gate_results,
            failure_class=raw.get("failure_class") if isinstance(raw.get("failure_class"), str) else None,
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            duration_seconds=float(raw.get("duration_seconds", 0.0)),
            objective=raw.get("objective") if isinstance(raw.get("objective"), str) else None,
            loop_id=raw.get("loop_id") if isinstance(raw.get("loop_id"), str) else None,
            loop_step=raw.get("loop_step") if isinstance(raw.get("loop_step"), int) else None,
            loop_final_decision=(
                raw.get("loop_final_decision") if isinstance(raw.get("loop_final_decision"), str) else None
            ),
            loop_stop_reason=raw.get("loop_stop_reason") if isinstance(raw.get("loop_stop_reason"), str) else None,
        )


@dataclass(slots=True)
class BenchmarkTask:
    id: str
    objective: str
    max_steps: int = 3
    max_reverts: int = 2
    max_total_seconds: int = 900
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "objective": self.objective,
            "max_steps": self.max_steps,
            "max_reverts": self.max_reverts,
            "max_total_seconds": self.max_total_seconds,
            "tags": list(self.tags),
        }


@dataclass(slots=True)
class BenchmarkTaskResult:
    task_id: str
    objective: str
    status: BenchmarkTaskStatus
    run_id: str
    final_decision: Decision
    score: float
    duration_seconds: float
    failure_class: FailureClass | None = None
    error: str | None = None
    loop_id: str | None = None
    loop_step: int | None = None
    loop_stop_reason: LoopStopReason | None = None
    changed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "status": self.status,
            "run_id": self.run_id,
            "final_decision": self.final_decision,
            "score": self.score,
            "duration_seconds": self.duration_seconds,
            "failure_class": self.failure_class,
            "error": self.error,
            "loop_id": self.loop_id,
            "loop_step": self.loop_step,
            "loop_stop_reason": self.loop_stop_reason,
            "changed_files": list(self.changed_files),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> BenchmarkTaskResult:
        return cls(
            task_id=str(raw.get("task_id", "")),
            objective=str(raw.get("objective", "")),
            status=str(raw.get("status", "failed")),  # type: ignore[arg-type]
            run_id=str(raw.get("run_id", "")),
            final_decision=str(raw.get("final_decision", "failed")),  # type: ignore[arg-type]
            score=float(raw.get("score", 0.0)),
            duration_seconds=float(raw.get("duration_seconds", 0.0)),
            failure_class=raw.get("failure_class") if isinstance(raw.get("failure_class"), str) else None,
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            loop_id=raw.get("loop_id") if isinstance(raw.get("loop_id"), str) else None,
            loop_step=raw.get("loop_step") if isinstance(raw.get("loop_step"), int) else None,
            loop_stop_reason=raw.get("loop_stop_reason") if isinstance(raw.get("loop_stop_reason"), str) else None,
            changed_files=[str(item) for item in raw.get("changed_files", []) if isinstance(item, str)],
        )


@dataclass(slots=True)
class BenchmarkRunRecord:
    benchmark_run_id: str
    session_id: str
    task_source: str
    total_tasks: int
    passed_tasks: int
    accept_rate: float
    avg_score: float
    avg_duration_seconds: float
    duration_seconds: float
    task_results: list[BenchmarkTaskResult] = field(default_factory=list)
    failure_breakdown: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "benchmark_run_id": self.benchmark_run_id,
            "session_id": self.session_id,
            "task_source": self.task_source,
            "total_tasks": self.total_tasks,
            "passed_tasks": self.passed_tasks,
            "accept_rate": self.accept_rate,
            "avg_score": self.avg_score,
            "avg_duration_seconds": self.avg_duration_seconds,
            "duration_seconds": self.duration_seconds,
            "failure_breakdown": dict(self.failure_breakdown),
            "warnings": list(self.warnings),
            "task_results": [result.to_dict() for result in self.task_results],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> BenchmarkRunRecord:
        task_results: list[BenchmarkTaskResult] = []
        for item in raw.get("task_results", []):
            if not isinstance(item, dict):
                continue
            task_results.append(BenchmarkTaskResult.from_dict(cast(dict[str, object], item)))
        failure_breakdown_raw = raw.get("failure_breakdown")
        failure_breakdown: dict[str, int] = {}
        if isinstance(failure_breakdown_raw, dict):
            for key, value in failure_breakdown_raw.items():
                if isinstance(key, str) and isinstance(value, int):
                    failure_breakdown[key] = value
        warnings = [str(item) for item in raw.get("warnings", []) if isinstance(item, str)]
        return cls(
            benchmark_run_id=str(raw.get("benchmark_run_id", "")),
            session_id=str(raw.get("session_id", "")),
            task_source=str(raw.get("task_source", "")),
            total_tasks=int(raw.get("total_tasks", 0)),
            passed_tasks=int(raw.get("passed_tasks", 0)),
            accept_rate=float(raw.get("accept_rate", 0.0)),
            avg_score=float(raw.get("avg_score", 0.0)),
            avg_duration_seconds=float(raw.get("avg_duration_seconds", 0.0)),
            duration_seconds=float(raw.get("duration_seconds", 0.0)),
            task_results=task_results,
            failure_breakdown=failure_breakdown,
            warnings=warnings,
        )


@dataclass(slots=True)
class WorkspaceCheckpoint:
    checkpoint_id: str
    tracked_files: dict[str, bytes]


class IterationExecutor:
    AUTO_MAX_STEPS = 3
    AUTO_MAX_REVERTS = 2
    AUTO_MAX_TOTAL_SECONDS = 900

    def __init__(
        self,
        cwd: Path,
        *,
        log_path: Path | None = None,
        benchmark_log_path: Path | None = None,
    ):
        self.cwd = cwd
        self.log_path = log_path or (cwd / ".astra" / "logs" / "iteration_runs.jsonl")
        self.benchmark_log_path = benchmark_log_path or (cwd / ".astra" / "logs" / "iteration_benchmarks.jsonl")

    def run_once(
        self,
        *,
        session_id: str,
        iterate_fn: Callable[[], str | None],
        objective: str | None = None,
        loop_id: str | None = None,
        loop_step: int | None = None,
        loop_final_decision: LoopDecision | None = None,
        loop_stop_reason: LoopStopReason | None = None,
        append_record: bool = True,
    ) -> IterationRunRecord:
        run_id = uuid.uuid4().hex
        checkpoint_id = f"ckpt-{run_id[:8]}"
        start = time.monotonic()
        gate_results: list[GateResult] = []
        failure_class: FailureClass | None = None
        error: str | None = None
        final_decision: Decision = "failed"
        score = 0.0
        changed_files: list[str] = []

        if not self._is_git_workspace():
            record = IterationRunRecord(
                run_id=run_id,
                session_id=session_id,
                checkpoint_id=checkpoint_id,
                final_decision="failed",
                score=0.0,
                failure_class="env",
                error="Iteration requires a git workspace.",
                objective=objective,
                loop_id=loop_id,
                loop_step=loop_step,
                loop_final_decision=loop_final_decision,
                loop_stop_reason=loop_stop_reason,
            )
            if append_record:
                self._append_record(record)
            return record

        python_path = self._resolve_python_executable()
        if python_path is None:
            record = IterationRunRecord(
                run_id=run_id,
                session_id=session_id,
                checkpoint_id=checkpoint_id,
                final_decision="failed",
                score=0.0,
                failure_class="env",
                error="Iteration requires .venv python (.venv/Scripts/python.exe or .venv/bin/python).",
                objective=objective,
                loop_id=loop_id,
                loop_step=loop_step,
                loop_final_decision=loop_final_decision,
                loop_stop_reason=loop_stop_reason,
            )
            if append_record:
                self._append_record(record)
            return record

        dirty_files = self._dirty_files()
        if dirty_files:
            record = IterationRunRecord(
                run_id=run_id,
                session_id=session_id,
                checkpoint_id=checkpoint_id,
                final_decision="failed",
                score=0.0,
                failure_class="env",
                error=f"Working tree is dirty ({', '.join(dirty_files[:5])}). Commit or stash changes before /iterate once.",
                objective=objective,
                loop_id=loop_id,
                loop_step=loop_step,
                loop_final_decision=loop_final_decision,
                loop_stop_reason=loop_stop_reason,
            )
            if append_record:
                self._append_record(record)
            return record

        checkpoint = self._create_checkpoint(checkpoint_id)
        gates = self._default_gates(python_path)
        try:
            iteration_error = iterate_fn()
            changed_files = self._changed_files()
            if iteration_error:
                failure_class = "unknown"
                error = iteration_error
                self._restore_checkpoint(checkpoint)
                final_decision = "reverted"
            else:
                gate_results = self._run_gates(gates)
                score = self._score_gates(gate_results)
                failed_gate = next((result for result in gate_results if result.status != "passed"), None)
                if failed_gate is not None:
                    failure_class = self._classify_gate_failure(failed_gate)
                    self._restore_checkpoint(checkpoint)
                    final_decision = "reverted"
                else:
                    final_decision = "accepted"
        except Exception as exc:
            changed_files = changed_files or self._changed_files()
            self._restore_checkpoint(checkpoint)
            final_decision = "reverted"
            failure_class = "unknown"
            error = str(exc)

        duration_seconds = time.monotonic() - start
        record = IterationRunRecord(
            run_id=run_id,
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            final_decision=final_decision,
            score=score,
            changed_files=changed_files,
            gate_results=gate_results,
            failure_class=failure_class,
            error=error,
            duration_seconds=duration_seconds,
            objective=objective,
            loop_id=loop_id,
            loop_step=loop_step,
            loop_final_decision=loop_final_decision,
            loop_stop_reason=loop_stop_reason,
        )
        if append_record:
            self._append_record(record)
        return record

    def run_auto(
        self,
        *,
        session_id: str,
        iterate_fn: Callable[[int], str | None],
        objective: str | None = None,
        max_steps: int = AUTO_MAX_STEPS,
        max_reverts: int = AUTO_MAX_REVERTS,
        max_total_seconds: int = AUTO_MAX_TOTAL_SECONDS,
    ) -> IterationRunRecord:
        loop_id = f"loop-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()
        max_steps = max(1, max_steps)
        max_reverts = max(1, max_reverts)
        max_total_seconds = max(1, max_total_seconds)
        revert_count = 0

        for step in range(1, max_steps + 1):
            if time.monotonic() - start >= max_total_seconds:
                timeout_record = IterationRunRecord(
                    run_id=uuid.uuid4().hex,
                    session_id=session_id,
                    checkpoint_id=f"ckpt-{uuid.uuid4().hex[:8]}",
                    final_decision="failed",
                    score=0.0,
                    failure_class="timeout",
                    error="Iteration loop time budget exceeded before executing this step.",
                    objective=objective,
                    loop_id=loop_id,
                    loop_step=step,
                    loop_final_decision="failed",
                    loop_stop_reason="max_total_seconds",
                )
                self._append_record(timeout_record)
                return timeout_record

            record = self.run_once(
                session_id=session_id,
                iterate_fn=lambda current_step=step: iterate_fn(current_step),
                objective=objective,
                loop_id=loop_id,
                loop_step=step,
                append_record=False,
            )

            if record.failure_class == "env":
                record.loop_final_decision = "failed"
                record.loop_stop_reason = "env_failure"
                self._append_record(record)
                return record

            if record.final_decision == "accepted":
                record.loop_final_decision = "accepted"
                record.loop_stop_reason = "accepted"
                self._append_record(record)
                return record

            if record.final_decision == "reverted":
                revert_count += 1

            elapsed = time.monotonic() - start
            if revert_count >= max_reverts:
                record.loop_final_decision = "failed"
                record.loop_stop_reason = "max_reverts"
                self._append_record(record)
                return record
            if elapsed >= max_total_seconds:
                record.loop_final_decision = "failed"
                record.loop_stop_reason = "max_total_seconds"
                self._append_record(record)
                return record
            if step >= max_steps:
                record.loop_final_decision = "failed"
                record.loop_stop_reason = "max_steps"
                self._append_record(record)
                return record

            self._append_record(record)

        fallback = IterationRunRecord(
            run_id=uuid.uuid4().hex,
            session_id=session_id,
            checkpoint_id=f"ckpt-{uuid.uuid4().hex[:8]}",
            final_decision="failed",
            score=0.0,
            failure_class="unknown",
            error="Iteration loop exited unexpectedly.",
            objective=objective,
            loop_id=loop_id,
            loop_step=max_steps,
            loop_final_decision="failed",
            loop_stop_reason="max_steps",
        )
        self._append_record(fallback)
        return fallback

    def run_benchmark(
        self,
        *,
        session_id: str,
        iterate_fn: Callable[[BenchmarkTask, int], str | None],
        task_path: Path | None = None,
    ) -> BenchmarkRunRecord:
        benchmark_run_id = f"bench-{uuid.uuid4().hex[:12]}"
        start = time.monotonic()
        resolved_task_path = self._resolve_task_path(task_path)
        tasks, warnings = self.load_benchmark_tasks(task_path=resolved_task_path)
        task_results: list[BenchmarkTaskResult] = []
        failure_breakdown: dict[str, int] = {}

        for task in tasks:
            run_record = self.run_auto(
                session_id=session_id,
                iterate_fn=lambda step, current_task=task: iterate_fn(current_task, step),
                objective=task.objective,
                max_steps=task.max_steps,
                max_reverts=task.max_reverts,
                max_total_seconds=task.max_total_seconds,
            )
            task_status: BenchmarkTaskStatus = "passed" if run_record.loop_final_decision == "accepted" else "failed"
            if task_status == "failed":
                failure_key = run_record.failure_class or "unknown"
                failure_breakdown[failure_key] = failure_breakdown.get(failure_key, 0) + 1
            task_results.append(
                BenchmarkTaskResult(
                    task_id=task.id,
                    objective=task.objective,
                    status=task_status,
                    run_id=run_record.run_id,
                    final_decision=run_record.final_decision,
                    score=run_record.score,
                    duration_seconds=run_record.duration_seconds,
                    failure_class=run_record.failure_class,
                    error=run_record.error,
                    loop_id=run_record.loop_id,
                    loop_step=run_record.loop_step,
                    loop_stop_reason=run_record.loop_stop_reason,
                    changed_files=list(run_record.changed_files),
                )
            )

        total_tasks = len(task_results)
        passed_tasks = sum(1 for result in task_results if result.status == "passed")
        accept_rate = (passed_tasks / total_tasks) if total_tasks else 0.0
        avg_score = (sum(result.score for result in task_results) / total_tasks) if total_tasks else 0.0
        avg_duration_seconds = (
            sum(result.duration_seconds for result in task_results) / total_tasks if total_tasks else 0.0
        )
        if total_tasks == 0:
            warnings = [*warnings, f"No valid benchmark tasks loaded from {resolved_task_path}."]

        benchmark_record = BenchmarkRunRecord(
            benchmark_run_id=benchmark_run_id,
            session_id=session_id,
            task_source=self._display_path(resolved_task_path),
            total_tasks=total_tasks,
            passed_tasks=passed_tasks,
            accept_rate=accept_rate,
            avg_score=avg_score,
            avg_duration_seconds=avg_duration_seconds,
            duration_seconds=time.monotonic() - start,
            task_results=task_results,
            failure_breakdown=failure_breakdown,
            warnings=warnings,
        )
        self._append_benchmark_record(benchmark_record)
        return benchmark_record

    def load_benchmark_tasks(self, *, task_path: Path | None = None) -> tuple[list[BenchmarkTask], list[str]]:
        resolved_task_path = self._resolve_task_path(task_path)
        warnings: list[str] = []
        if not resolved_task_path.exists():
            warnings.append(f"Benchmark task file does not exist: {resolved_task_path}")
            return [], warnings

        try:
            loaded = yaml.safe_load(resolved_task_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            warnings.append(f"Failed to load benchmark task file {resolved_task_path}: {exc}")
            return [], warnings

        task_items: list[object]
        if loaded is None:
            warnings.append(f"Benchmark task file is empty: {resolved_task_path}")
            return [], warnings
        if isinstance(loaded, list):
            task_items = list(loaded)
        elif isinstance(loaded, dict):
            raw_items = loaded.get("tasks")
            if not isinstance(raw_items, list):
                warnings.append(
                    f"Benchmark task file must contain a top-level list or tasks list: {resolved_task_path}"
                )
                return [], warnings
            task_items = raw_items
        else:
            warnings.append(f"Unsupported benchmark task format in {resolved_task_path}")
            return [], warnings

        tasks: list[BenchmarkTask] = []
        seen_ids: set[str] = set()
        for index, raw_task in enumerate(task_items, start=1):
            if not isinstance(raw_task, dict):
                warnings.append(f"tasks[{index}] must be a mapping; skipped.")
                continue
            try:
                task = self._parse_benchmark_task(cast(dict[str, object], raw_task), index)
            except ValueError as exc:
                warnings.append(str(exc))
                continue
            if task.id in seen_ids:
                warnings.append(f"tasks[{index}].id duplicated ({task.id}); skipped.")
                continue
            seen_ids.add(task.id)
            tasks.append(task)
        return tasks, warnings

    def last_record(self) -> IterationRunRecord | None:
        if not self.log_path.exists():
            return None
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        for raw_line in reversed(lines):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            return IterationRunRecord.from_dict(payload)
        return None

    def last_benchmark_record(self) -> BenchmarkRunRecord | None:
        if not self.benchmark_log_path.exists():
            return None
        lines = self.benchmark_log_path.read_text(encoding="utf-8").splitlines()
        for raw_line in reversed(lines):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            return BenchmarkRunRecord.from_dict(payload)
        return None

    def _default_gates(self, python_path: Path) -> list[GateSpec]:
        return [
            GateSpec(name="compileall", command=[str(python_path), "-m", "compileall", "src"]),
            GateSpec(name="unit_tests", command=[str(python_path), "-m", "pytest", "-q", "tests/unit"]),
            GateSpec(name="cli_help", command=[str(python_path), "-m", "astra", "--help"]),
        ]

    def _run_gates(self, gates: list[GateSpec]) -> list[GateResult]:
        results: list[GateResult] = []
        for gate in gates:
            start = time.monotonic()
            try:
                completed = subprocess.run(
                    gate.command,
                    cwd=self.cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=gate.timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                results.append(
                    GateResult(
                        name=gate.name,
                        status="timeout",
                        duration_seconds=time.monotonic() - start,
                        exit_code=None,
                        stdout_tail=self._tail_text(exc.stdout),
                        stderr_tail=self._tail_text(exc.stderr),
                    )
                )
                break
            except Exception as exc:
                results.append(
                    GateResult(
                        name=gate.name,
                        status="error",
                        duration_seconds=time.monotonic() - start,
                        exit_code=None,
                        stderr_tail=self._tail_text(str(exc)),
                    )
                )
                break

            status: Literal["passed", "failed"] = "passed" if completed.returncode == 0 else "failed"
            results.append(
                GateResult(
                    name=gate.name,
                    status=status,
                    duration_seconds=time.monotonic() - start,
                    exit_code=completed.returncode,
                    stdout_tail=self._tail_text(completed.stdout),
                    stderr_tail=self._tail_text(completed.stderr),
                )
            )
            if status != "passed":
                break
        return results

    def _create_checkpoint(self, checkpoint_id: str) -> WorkspaceCheckpoint:
        tracked_files: dict[str, bytes] = {}
        for relative_path in self._git_tracked_files():
            source = self.cwd / relative_path
            if source.exists() and source.is_file():
                tracked_files[relative_path] = source.read_bytes()
        return WorkspaceCheckpoint(checkpoint_id=checkpoint_id, tracked_files=tracked_files)

    def _restore_checkpoint(self, checkpoint: WorkspaceCheckpoint) -> None:
        current_tracked = set(self._git_tracked_files())
        snapshot_tracked = set(checkpoint.tracked_files)

        for relative_path in current_tracked - snapshot_tracked:
            path = self.cwd / relative_path
            if path.exists():
                path.unlink()

        for relative_path, content in checkpoint.tracked_files.items():
            path = self.cwd / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        for relative_path in self._git_untracked_files():
            path = self.cwd / relative_path
            if path.is_dir():
                continue
            if path.exists():
                path.unlink()

    def _append_record(self, record: IterationRunRecord) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _append_benchmark_record(self, record: BenchmarkRunRecord) -> None:
        self.benchmark_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.benchmark_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _changed_files(self) -> list[str]:
        status = self._git_output(["status", "--porcelain"])
        changed: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if line.startswith("?? ") and self._is_ignorable_untracked(path):
                continue
            changed.append(path)
        return changed

    def _dirty_files(self) -> list[str]:
        status = self._git_output(["status", "--porcelain"])
        dirty_files: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if line.startswith("?? ") and self._is_ignorable_untracked(path):
                continue
            dirty_files.append(path)
        return dirty_files

    def _classify_gate_failure(self, gate: GateResult) -> FailureClass:
        if gate.status == "timeout":
            return "timeout"
        if gate.name == "compileall":
            return "syntax"
        if gate.name == "unit_tests":
            return "test"
        if gate.name == "cli_help":
            return "cli"
        if gate.status == "error":
            return "env"
        return "unknown"

    def _score_gates(self, gate_results: list[GateResult]) -> float:
        if not gate_results:
            return 0.0
        passed = sum(1 for gate in gate_results if gate.status == "passed")
        return passed / len(self._default_gates(self._resolve_python_executable() or Path("python")))

    def _is_git_workspace(self) -> bool:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.cwd), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except Exception:
            return False
        return completed.returncode == 0 and completed.stdout.strip() == "true"

    def _git_output(self, args: list[str]) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
        return completed.stdout

    def _git_tracked_files(self) -> list[str]:
        output = self._git_output(["ls-files", "-z"])
        return [item for item in output.split("\0") if item]

    def _git_untracked_files(self) -> list[str]:
        output = self._git_output(["ls-files", "--others", "--exclude-standard", "-z"])
        return [item for item in output.split("\0") if item]

    def _resolve_python_executable(self) -> Path | None:
        windows_python = self.cwd / ".venv" / "Scripts" / "python.exe"
        if windows_python.exists():
            return windows_python
        posix_python = self.cwd / ".venv" / "bin" / "python"
        if posix_python.exists():
            return posix_python
        return None

    def _is_ignorable_untracked(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        return normalized.startswith(".astra/logs/") or normalized.startswith(".venv/")

    def _tail_text(self, text: str | bytes | None, *, max_chars: int = 2000) -> str:
        if text is None:
            return ""
        if isinstance(text, bytes):
            normalized = text.decode("utf-8", errors="replace")
        else:
            normalized = text
        if len(normalized) <= max_chars:
            return normalized
        return normalized[-max_chars:]

    def _resolve_task_path(self, task_path: Path | None) -> Path:
        if task_path is None:
            return self.cwd / DEFAULT_BENCHMARK_TASK_PATH
        if task_path.is_absolute():
            return task_path
        return (self.cwd / task_path).resolve()

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.cwd))
        except ValueError:
            return str(path)

    def _parse_benchmark_task(self, raw: Mapping[str, object], index: int) -> BenchmarkTask:
        task_id = self._required_non_empty_string(raw.get("id"), f"tasks[{index}].id")
        objective = self._required_non_empty_string(raw.get("objective"), f"tasks[{index}].objective")
        max_steps = self._positive_int(raw.get("max_steps", self.AUTO_MAX_STEPS), f"tasks[{index}].max_steps")
        max_reverts = self._positive_int(raw.get("max_reverts", self.AUTO_MAX_REVERTS), f"tasks[{index}].max_reverts")
        max_total_seconds = self._positive_int(
            raw.get("max_total_seconds", self.AUTO_MAX_TOTAL_SECONDS),
            f"tasks[{index}].max_total_seconds",
        )
        tags_raw = raw.get("tags", [])
        if not isinstance(tags_raw, list) or any(not isinstance(item, str) for item in tags_raw):
            raise ValueError(f"tasks[{index}].tags must be a list of strings.")
        return BenchmarkTask(
            id=task_id,
            objective=objective,
            max_steps=max_steps,
            max_reverts=max_reverts,
            max_total_seconds=max_total_seconds,
            tags=[str(tag) for tag in tags_raw],
        )

    def _required_non_empty_string(self, value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string.")
        return value.strip()

    def _positive_int(self, value: object, label: str) -> int:
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer.")
        return value
