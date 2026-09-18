"""Generate NFL same-game parlays as a separate system."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.builders.nfl_parlays_builder import build_nfl_parlays_board
from app.config import build_config
from app.paths import build_paths


def generate_nfl_parlays(project_root: Path) -> None:
    config = build_config(project_root)
    paths = build_paths(project_root)

    # Build parlay board (independent from main board)
    parlays_board = build_nfl_parlays_board(config=config, paths=paths)

    # Save to separate data files
    frontend_parlay_path = project_root / "frontend" / "data" / "nfl_parlays.json"
    frontend_parlay_path.parent.mkdir(parents=True, exist_ok=True)
    with open(frontend_parlay_path, "w") as f:
        json.dump(parlays_board, f, indent=2)

    # Root GitHub Pages entrypoint fetches data/nfl_parlays.json.
    site_parlay_path = project_root / "data" / "nfl_parlays.json"
    site_parlay_path.parent.mkdir(parents=True, exist_ok=True)
    with open(site_parlay_path, "w") as f:
        json.dump(parlays_board, f, indent=2)

    backend_parlay_path = project_root / "backend" / "data_final" / "nfl_parlays.json"
    backend_parlay_path.parent.mkdir(parents=True, exist_ok=True)
    with open(backend_parlay_path, "w") as f:
        json.dump(parlays_board, f, indent=2)

    print(f"Generated {len(parlays_board['parlays'])} parlay tickets")
    print(f"  Site:     {site_parlay_path}")
    print(f"  Frontend: {frontend_parlay_path}")
    print(f"  Backend:  {backend_parlay_path}")


if __name__ == "__main__":
    generate_nfl_parlays(Path(__file__).resolve().parents[1].parent)
