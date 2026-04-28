from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def publish_json_updates(*, project_root: Path, message: str) -> None:
    run_git(["add", "backend/data_final", "frontend/data"], cwd=project_root)
    run_git(["commit", "-m", message], cwd=project_root)
    run_git(["push"], cwd=project_root)
