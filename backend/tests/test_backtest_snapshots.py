from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from backtest.outcomes_mlb import resolve_moneyline, resolve_prop
from backtest.snapshots import (
    extract_picks,
    parse_line_threshold,
    select_daily_snapshots,
)


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class TestLineParsing:
    def test_common_board_lines(self):
        assert parse_line_threshold("HR 1+") == 1
        assert parse_line_threshold("2+ Hits") == 2
        assert parse_line_threshold("8+ K") == 8
        assert parse_line_threshold("15+ PTS") == 15
        assert parse_line_threshold("2+ 3PM") == 2

    def test_unparseable(self):
        assert parse_line_threshold("Moneyline") is None
        assert parse_line_threshold("") is None
        assert parse_line_threshold(None) is None


class TestSnapshotSelection:
    def test_prefers_last_snapshot_before_cutoff(self):
        commits = [
            {"hash": "a", "ts": utc("2026-07-10T12:00:00")},  # 8:00 ET
            {"hash": "b", "ts": utc("2026-07-10T15:30:00")},  # 11:30 ET
            {"hash": "c", "ts": utc("2026-07-10T20:00:00")},  # 16:00 ET (post-cutoff)
        ]
        selected = select_daily_snapshots(commits, cutoff_et_hour=12)
        assert len(selected) == 1
        assert selected[0]["hash"] == "b" and selected[0]["pregame"] is True

    def test_falls_back_to_earliest_when_no_pregame_snapshot(self):
        commits = [
            {"hash": "x", "ts": utc("2026-07-10T20:00:00")},
            {"hash": "y", "ts": utc("2026-07-10T23:00:00")},
        ]
        selected = select_daily_snapshots(commits, cutoff_et_hour=12)
        assert selected[0]["hash"] == "x" and selected[0]["pregame"] is False

    def test_groups_by_eastern_date_not_utc(self):
        # 02:00 UTC is 22:00 ET the previous day
        commits = [{"hash": "n", "ts": utc("2026-07-11T02:00:00")}]
        selected = select_daily_snapshots(commits, cutoff_et_hour=12)
        assert selected[0]["date"] == "2026-07-10"

    def test_date_range_filter(self):
        commits = [
            {"hash": "a", "ts": utc("2026-07-09T12:00:00")},
            {"hash": "b", "ts": utc("2026-07-10T12:00:00")},
        ]
        selected = select_daily_snapshots(
            commits, cutoff_et_hour=12, start="2026-07-10", end="2026-07-10"
        )
        assert [s["hash"] for s in selected] == ["b"]


BOARD = {
    "date": "2026-07-10",
    "games": [
        {
            "game_id": "pit-cle-2026-07-10",
            "markets": {
                "HR": [
                    {
                        "player_id": "664040",
                        "player_name": "Slugger",
                        "team": "PIT",
                        "opponent": "CLE",
                        "line": "HR 1+",
                        "sim_prob_pct": 34.8,
                    },
                    {  # no sim prob -> skipped
                        "player_id": "1",
                        "player_name": "No Sim",
                        "team": "PIT",
                        "opponent": "CLE",
                        "line": "HR 1+",
                    },
                ],
                "ML": [
                    {
                        "player_id": "114-moneyline",
                        "player_name": "Cleveland",
                        "team": "CLE",
                        "opponent": "PIT",
                        "line": "Moneyline",
                        "sim_prob_pct": 55.5,
                        "kalshi": {"ticker": "T", "implied_prob": 0.505},
                        "decision": "BET",
                    }
                ],
            },
        }
    ],
}


class TestExtractPicks:
    def test_extracts_props_and_ml_with_metadata(self):
        picks = extract_picks(BOARD, sport="mlb", date="2026-07-10")
        assert len(picks) == 2
        prop, ml = picks
        assert prop["market"] == "HR" and prop["threshold"] == 1
        assert prop["model_prob"] == 0.348
        assert ml["market"] == "ML" and ml["threshold"] is None
        assert ml["recorded_decision"] == "BET"
        assert ml["recorded_implied_prob"] == 0.505

    def test_duplicate_rows_deduped(self):
        board = {
            "date": "2026-07-10",
            "games": [
                BOARD["games"][0],
                BOARD["games"][0],  # same game twice
            ],
        }
        assert len(extract_picks(board, sport="mlb", date="2026-07-10")) == 2


RESULTS = {
    "games": [
        {
            "gamePk": 1,
            "away": "PIT",
            "home": "CLE",
            "away_score": 2,
            "home_score": 5,
            "players": {
                "664040": {"pa": 4, "h": 2, "d": 1, "t": 0, "hr": 1, "rbi": 2, "k": None, "outs": None},
                "777777": {"pa": None, "h": None, "d": None, "t": None, "hr": None, "rbi": None, "k": 7, "outs": 18},
            },
        }
    ]
}


def prop(pid: str, market: str, threshold: int) -> dict:
    return {
        "game_id": "pit-cle-2026-07-10",
        "market": market,
        "player_id": pid,
        "team": "PIT",
        "opponent": "CLE",
        "threshold": threshold,
    }


class TestMlbResolution:
    def test_hr_hit_and_miss(self):
        assert resolve_prop(prop("664040", "HR", 1), RESULTS) == 1
        assert resolve_prop(prop("664040", "HR", 2), RESULTS) == 0

    def test_total_bases_formula(self):
        # 2 hits: 1 double + 1 HR -> TB = 2 + 1 + 0 + 3 = 6
        assert resolve_prop(prop("664040", "TB", 6), RESULTS) == 1
        assert resolve_prop(prop("664040", "TB", 7), RESULTS) == 0

    def test_pitcher_strikeouts(self):
        assert resolve_prop(prop("777777", "K", 7), RESULTS) == 1
        assert resolve_prop(prop("777777", "K", 8), RESULTS) == 0

    def test_absent_player_is_void(self):
        assert resolve_prop(prop("999999", "HR", 1), RESULTS) is None

    def test_batter_market_on_non_batter_is_void(self):
        assert resolve_prop(prop("777777", "Hits", 1), RESULTS) is None

    def test_moneyline_sides(self):
        base = {"game_id": "pit-cle-2026-07-10", "market": "ML", "opponent": "PIT"}
        assert resolve_moneyline({**base, "team": "CLE"}, RESULTS) == 1
        assert resolve_moneyline({**base, "team": "PIT", "opponent": "CLE"}, RESULTS) == 0

    def test_moneyline_unmatched_game_is_void(self):
        pick = {"game_id": "nyy-bos-2026-07-10", "market": "ML", "team": "NYY", "opponent": "BOS"}
        assert resolve_moneyline(pick, RESULTS) is None
