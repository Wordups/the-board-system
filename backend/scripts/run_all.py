from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import run_mlb_pipeline, run_nba_pipeline, run_soccer_pipeline, run_tennis_pipeline, run_wnba_pipeline
from app.outputs.picks_snapshot import write_picks_snapshot
from app.paths import build_paths


def main() -> None:
    repo_root = PROJECT_ROOT.parent
    mlb = run_mlb_pipeline(repo_root)
    nba = run_nba_pipeline(repo_root)
    wnba = run_wnba_pipeline(repo_root)
    soccer = run_soccer_pipeline(repo_root)
    tennis = run_tennis_pipeline(repo_root)
    write_picks_snapshot(
        boards={
            "mlb": mlb,
            "nba": nba,
            "wnba": wnba,
            "soccer": soccer,
            "tennis": tennis,
        },
        paths=build_paths(repo_root),
    )


if __name__ == "__main__":
    main()
