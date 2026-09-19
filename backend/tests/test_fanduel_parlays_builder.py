import json
from types import SimpleNamespace

import pytest

from app.builders.nfl_parlays_builder import FANDUEL_SOURCE, _load_fanduel_snapshot, build_nfl_parlays_board


def _snapshot():
    return {
        "source": FANDUEL_SOURCE,
        "date": "2026-09-20",
        "parlays": [{
            "game_id": "1",
            "matchup": "CAR @ ATL",
            "book": FANDUEL_SOURCE,
            "displayed_odds": 1000,
            "odds_status": "pending movement confirmation",
            "legs": [{
                "player_name": "Player",
                "market": "REC",
                "line": "2+ Receptions",
                "selection": "Player 2+ Receptions",
                "book": FANDUEL_SOURCE,
            }],
        }],
    }


def test_rejects_non_fanduel_leg(tmp_path):
    payload = _snapshot()
    payload["parlays"][0]["legs"][0]["book"] = "Other Book"
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Every NFL leg"):
        _load_fanduel_snapshot(path)


def test_suppresses_unconfirmed_price(tmp_path):
    source_dir = tmp_path / "data_sources"
    source_dir.mkdir()
    (source_dir / "fanduel_nfl_parlays.json").write_text(json.dumps(_snapshot()), encoding="utf-8")
    board = build_nfl_parlays_board(config=None, paths=SimpleNamespace(backend_root=tmp_path))
    assert board["source"] == FANDUEL_SOURCE
    assert board["parlays"][0]["fanduel"]["odds"] is None
