from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


FailureClass = Literal["syntax", "test", "cli", "env", "timeout", "unknown"]
Decision = Literal["accepted", "reverted", "failed"]


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
        )


@dataclass(slots=True)
class WorkspaceCheckpoint:
    checkpoint_id: str
    tracked_files: dict[str, bytes]


class IterationExecutor:
    def __init__(self, cwd: Path, *, log_path: Path | None = None):
        self.cwd = cwd
        self.log_path = log_path or (cwd / ".astra" / "logs" / "iteration_runs.jsonl")

    def run_once(
        self,
        *,
        session_id: str,
        iterate_fn: Callable[[], str | None],
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
            )
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
            )
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
            )
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
        )
        self._append_record(record)
        return record

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
