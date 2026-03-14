from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_cli_smoke_runtime_prompt_json_subprocess(tmp_path: Path) -> None:
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "test-key"
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")

    process = subprocess.run(
        [sys.executable, "-m", "astra", "--cwd", str(cwd)],
        input="/runtime json prompt\n/exit\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )

    assert process.returncode == 0, process.stderr
    assert "Session (new)" in process.stdout
    assert '"prompt"' in process.stdout
    assert '"fragment_count"' in process.stdout
    assert list((tmp_path / ".astra-python" / "sessions").glob("*.json")) == []
