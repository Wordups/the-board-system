from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.outputs.json_writer import write_json
from app.outputs.mlb_hr_tracking import build_mlb_hr_tracking_payload
from app.paths import build_paths


def list_daily_mlb_board_commits(repo_root: Path, *, days: int = 21) -> list[tuple[str, str]]:
    command = [
        "git",
        "log",
        f"--since={days} days ago",
        "--pretty=format:%H|%ad",
        "--date=short",
        "--",
        "frontend/data/mlb.json",
    ]
    result = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    daily: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip() or "|" not in line:
            continue
        commit, commit_date = line.split("|", 1)
        if commit_date not in daily:
            daily[commit_date] = commit
    return sorted(((day, commit) for day, commit in daily.items()), key=lambda item: item[0])


def load_board_from_commit(repo_root: Path, commit: str) -> dict | None:
    result = subprocess.run(
        ["git", "show", f"{commit}:frontend/data/mlb.json"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def main() -> None:
    repo_root = PROJECT_ROOT.parent
    paths = build_paths(repo_root)
    history_dir = paths.data_final / "history" / "mlb_hr_tracking"
    history_dir.mkdir(parents=True, exist_ok=True)

    latest_payload: dict | None = None
    for _, commit in list_daily_mlb_board_commits(repo_root):
        board = load_board_from_commit(repo_root, commit)
        if not board or board.get("sport") != "MLB":
            continue
        payload = build_mlb_hr_tracking_payload(board=board)
        board_date = str(payload.get("date") or "").strip()
        if not board_date:
            continue
        write_json(history_dir / f"{board_date}.json", payload)
        latest_payload = payload

    if latest_payload:
        write_json(paths.data_final / "mlb_hr_tracking_latest.json", latest_payload)


if __name__ == "__main__":
    main()
