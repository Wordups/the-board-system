from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.mlb_board_builder import apply_hr_board_sliding_scale
from app.main import run_mlb_pipeline


def test_mlb_pipeline_writes_json_outputs():
    board = run_mlb_pipeline(PROJECT_ROOT)
    assert board["sport"] == "MLB"
    assert board["pinned_board"]["players"]
    assert board["games"][0]["top_signals"]
    assert (PROJECT_ROOT / "backend" / "data_processed" / "mlb_processed.json").exists()
    assert (PROJECT_ROOT / "backend" / "data_final" / "mlb.json").exists()
    assert (PROJECT_ROOT / "frontend" / "data" / "mlb.json").exists()
    assert (PROJECT_ROOT / "data" / "mlb.json").exists()


def test_hr_board_sliding_scale_decays_live_games():
    pregame = apply_hr_board_sliding_scale(
        base_score=30.0,
        previous_score=28.0,
        status={"phase": "pregame", "minutes_to_start": 45, "is_lineup_window": True, "probable_pitchers_confirmed": True},
    )
    live = apply_hr_board_sliding_scale(
        base_score=30.0,
        previous_score=28.0,
        status={"phase": "live", "current_inning": 6},
    )
    final = apply_hr_board_sliding_scale(
        base_score=30.0,
        previous_score=28.0,
        status={"phase": "final"},
    )
    assert pregame > live
    assert final == 0.0
