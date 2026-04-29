from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.main import run_tennis_pipeline


def test_tennis_pipeline_writes_json_outputs():
    board = run_tennis_pipeline(PROJECT_ROOT)
    assert board["sport"] == "TENNIS"
    assert board["pinned_board"]["market"] == "ML"
    assert board["games"]
    assert set(board["games"][0]["markets"].keys()) == {"ML", "O/U", "Sets"}
    assert (PROJECT_ROOT / "backend" / "data_final" / "tennis.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "tennis.json").exists()
    assert (PROJECT_ROOT / "data" / "tennis.json").exists()
