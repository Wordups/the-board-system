from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.main import run_soccer_pipeline


def test_soccer_pipeline_writes_json_outputs():
    board = run_soccer_pipeline(PROJECT_ROOT)
    assert board["sport"] == "SOCCER"
    assert board["pinned_board"]["market"] == "GS"
    assert set(board.keys()) >= {"sport", "date", "last_updated", "pinned_board", "games"}
    assert (PROJECT_ROOT / "backend" / "data_final" / "soccer.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "soccer.json").exists()
    assert (PROJECT_ROOT / "data" / "soccer.json").exists()
