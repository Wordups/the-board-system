from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.main import run_mlb_pipeline


def test_mlb_pipeline_writes_json_outputs():
    board = run_mlb_pipeline(PROJECT_ROOT)
    assert board["sport"] == "MLB"
    assert board["pinned_board"]["players"]
    assert board["games"][0]["top_signals"]
    assert (PROJECT_ROOT / "backend" / "data_processed" / "mlb_processed.json").exists()
    assert (PROJECT_ROOT / "backend" / "data_final" / "mlb.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "mlb.json").exists()
