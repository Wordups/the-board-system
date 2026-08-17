"""NFL salary-categories tests — contract loading, tiering, and the historical cut.

Offline: contract fixtures are written to a temp directory, and the "history"
is synthetic game logs. No network, no dependence on the committed seed file.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.builders.nfl_salary_board import build_nfl_salary_board, median, percentile
from app.collectors.nfl_collector import player_key
from app.collectors.nfl_contracts import load_contracts, salary_tier, to_money


@dataclass
class StubPaths:
    data_raw: Path
    data_static: Path


def write_contracts(directory: Path, players: list[dict], *, meta: dict | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "nfl_contracts.json").write_text(
        json.dumps({"meta": meta or {"source": "test"}, "players": players}),
        encoding="utf-8",
    )


def make_history_profile(*, name: str, position: str, team: str, ppg: float, games: int = 34) -> dict:
    logs = [
        {
            "event_id": f"{name}-{index}",
            "season": 2026 - (index // 17),
            "game_date": f"2026-09-{(index % 28) + 1:02d}T17:00:00+00:00",
            "opponent": "SF",
            "fantasy_ppr": ppg,
            "TD": 0.0,
        }
        for index in range(games)
    ]
    return {
        "player_id": name,
        "player_name": name,
        "player_key": player_key(name),
        "team": team,
        "position": position,
        "logs": logs,
        "seasons": sorted({log["season"] for log in logs}, reverse=True),
    }


# ------------------------------------------------------------------ contracts


def test_salary_tier_bands():
    assert salary_tier(60_000_000) == "SUPERMAX"
    assert salary_tier(40_000_000) == "SUPERMAX"
    assert salary_tier(39_999_999) == "ELITE"
    assert salary_tier(25_000_000) == "ELITE"
    assert salary_tier(15_000_000) == "HIGH"
    assert salary_tier(7_000_000) == "MID"
    assert salary_tier(2_500_000) == "LOW"
    assert salary_tier(1_100_000) == "ROOKIE_MIN"


def test_money_parser_accepts_the_shapes_exports_actually_use():
    assert to_money(45_000_000) == 45_000_000
    assert to_money("45,000,000") == 45_000_000
    assert to_money("$45M") == 45_000_000
    assert to_money("45") == 45_000_000, "a bare small number is read as millions"
    assert to_money("") is None
    assert to_money("not a number") is None


def test_data_raw_override_wins_over_the_tracked_default(tmp_path):
    paths = StubPaths(data_raw=tmp_path / "raw", data_static=tmp_path / "static")
    write_contracts(paths.data_static, [{"player_name": "Tracked Player", "apy": 10_000_000}])
    write_contracts(paths.data_raw, [{"player_name": "Private Player", "apy": 20_000_000}])

    contracts = load_contracts(paths)

    assert set(contracts["players"]) == {player_key("Private Player")}
    assert contracts["meta"]["loaded"] is True


def test_missing_contracts_file_degrades_with_a_reason(tmp_path):
    paths = StubPaths(data_raw=tmp_path / "raw", data_static=tmp_path / "static")

    contracts = load_contracts(paths)

    assert contracts["players"] == {}
    assert contracts["meta"]["loaded"] is False
    assert "no contracts file" in contracts["meta"]["reason"]


def test_malformed_contracts_file_does_not_raise(tmp_path):
    paths = StubPaths(data_raw=tmp_path / "raw", data_static=tmp_path / "static")
    paths.data_static.mkdir(parents=True)
    (paths.data_static / "nfl_contracts.json").write_text("{not json", encoding="utf-8")

    contracts = load_contracts(paths)

    assert contracts["players"] == {}
    assert contracts["meta"]["loaded"] is False


def test_rows_without_a_name_or_salary_are_skipped(tmp_path):
    paths = StubPaths(data_raw=tmp_path / "raw", data_static=tmp_path / "static")
    write_contracts(
        paths.data_static,
        [
            {"player_name": "Good Row", "apy": 12_000_000},
            {"player_name": "", "apy": 9_000_000},
            {"player_name": "No Salary"},
        ],
    )

    contracts = load_contracts(paths)

    assert set(contracts["players"]) == {player_key("Good Row")}
    assert contracts["meta"]["player_count"] == 1


# ---------------------------------------------------------------- the board


def build_board(*, candidates=None):
    contracts = {
        "meta": {"loaded": True, "source": "test", "player_count": 4},
        "players": {
            player_key("Paid Star"): {
                "player_name": "Paid Star", "player_key": player_key("Paid Star"), "team": "KC",
                "position": "WR", "apy": 30_000_000.0, "total_value": None, "guaranteed": None,
                "years": 4, "signed": 2025, "salary_tier": "ELITE", "estimated": False,
            },
            player_key("Paid Bust"): {
                "player_name": "Paid Bust", "player_key": player_key("Paid Bust"), "team": "KC",
                "position": "WR", "apy": 30_000_000.0, "total_value": None, "guaranteed": None,
                "years": 4, "signed": 2025, "salary_tier": "ELITE", "estimated": True,
            },
            player_key("Cheap Producer"): {
                "player_name": "Cheap Producer", "player_key": player_key("Cheap Producer"), "team": "KC",
                "position": "RB", "apy": 1_500_000.0, "total_value": None, "guaranteed": None,
                "years": 4, "signed": 2024, "salary_tier": "ROOKIE_MIN", "estimated": False,
            },
            player_key("Bench Rookie"): {
                "player_name": "Bench Rookie", "player_key": player_key("Bench Rookie"), "team": "SF",
                "position": "RB", "apy": 1_000_000.0, "total_value": None, "guaranteed": None,
                "years": 4, "signed": 2025, "salary_tier": "ROOKIE_MIN", "estimated": False,
            },
        },
    }
    history = {
        "1": make_history_profile(name="Paid Star", position="WR", team="KC", ppg=20.0),
        "2": make_history_profile(name="Paid Bust", position="WR", team="KC", ppg=10.0),
        "3": make_history_profile(name="Cheap Producer", position="RB", team="KC", ppg=14.0),
        "4": make_history_profile(name="Bench Rookie", position="RB", team="SF", ppg=4.0),
    }
    if candidates is None:
        candidates = [
            {"player_key": player_key("Paid Star"), "team": "KC", "opponent": "SF", "game_id": "g1"},
            {"player_key": player_key("Paid Bust"), "team": "KC", "opponent": "SF", "game_id": "g1"},
            {"player_key": player_key("Cheap Producer"), "team": "KC", "opponent": "SF", "game_id": "g1"},
        ]
    return build_nfl_salary_board(
        history_profiles=history,
        contracts=contracts,
        candidates=candidates,
        history_seasons=[2026, 2025, 2024],
    )


def test_board_reports_unavailable_without_contracts():
    board = build_nfl_salary_board(
        history_profiles={},
        contracts={"meta": {"reason": "no contracts file found"}, "players": {}},
        candidates=[],
        history_seasons=[2026, 2025, 2024],
    )

    assert board["available"] is False
    assert board["reason"] == "no contracts file found"
    assert board["tiers"] == []


def test_tiers_summarize_the_rolling_window():
    board = build_board()

    elite = next(tier for tier in board["tiers"] if tier["tier"] == "ELITE")
    assert elite["players"] == 2
    assert elite["games"] == 68
    assert elite["median_ppg"] == 15.0            # median of 20.0 and 10.0
    assert elite["thin_sample"] is False
    assert [row["player_name"] for row in elite["top_producers"]] == ["Paid Star", "Paid Bust"]

    empty = next(tier for tier in board["tiers"] if tier["tier"] == "SUPERMAX")
    assert empty["players"] == 0
    assert empty["median_ppg"] is None
    assert empty["thin_sample"] is True, "a tier with no games is never a baseline"


def test_value_index_compares_a_player_to_his_own_tier():
    board = build_board()
    rows = {row["player_name"]: row for row in board["slate"]}

    star = rows["Paid Star"]
    # 20 ppg on $30M = 0.667 points per $1M; the ELITE median is 0.5 (15/30).
    assert star["points_per_million"] == round(20.0 / 30.0, 3)
    assert star["tier_median_points_per_million"] == 0.5
    assert star["value_index"] > 1.0, "out-producing his pay grade"
    assert star["vs_tier_median_pct"] == round((20.0 - 15.0) / 15.0 * 100, 1)

    bust = rows["Paid Bust"]
    assert bust["value_index"] < 1.0
    assert bust["vs_tier_median_pct"] < 0
    assert bust["estimated_salary"] is True, "an estimated figure stays labelled on the board"


def test_slate_only_includes_players_actually_playing():
    board = build_board()
    names = {row["player_name"] for row in board["slate"]}

    assert names == {"Paid Star", "Paid Bust", "Cheap Producer"}
    assert "Bench Rookie" not in names, "contracted but not on the slate"
    # The historical tiers still count him — the baseline is the league's
    # history, not tonight's participants.
    rookie_tier = next(tier for tier in board["tiers"] if tier["tier"] == "ROOKIE_MIN")
    assert rookie_tier["players"] == 2


def test_leaders_and_laggards_are_split_by_tier_baseline():
    board = build_board()

    assert board["value_leaders"][0]["player_name"] == "Cheap Producer"
    assert [row["player_name"] for row in board["below_tier_baseline"]] == ["Paid Bust"]


def test_window_reports_contracts_it_could_not_match():
    board = build_board()

    assert board["window"]["players_matched"] == 4
    assert board["window"]["contracts_loaded"] == 4
    assert board["window"]["unmatched_contracts"] == []
    assert board["window"]["seasons"] == [2026, 2025, 2024]


def test_position_breakdown_splits_tiers_by_position():
    board = build_board()
    rows = {(row["tier"], row["position"]): row for row in board["tier_by_position"]}

    assert rows[("ELITE", "WR")]["players"] == 2
    assert rows[("ROOKIE_MIN", "RB")]["players"] == 2
    assert ("ELITE", "RB") not in rows, "empty cells are omitted, not zero-filled"


def test_season_splits_expose_a_decline():
    board = build_board()
    star = next(row for row in board["slate"] if row["player_name"] == "Paid Star")

    seasons = [split["season"] for split in star["season_splits"]]
    assert seasons == sorted(seasons, reverse=True)
    assert sum(split["games"] for split in star["season_splits"]) == star["games"]


# ------------------------------------------------------------------ numerics


def test_median_and_percentile():
    assert median([1.0, 3.0, 2.0]) == 2.0
    assert median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert median([]) == 0.0
    assert percentile([10.0, 20.0, 30.0, 40.0], 0.25) == 17.5
    assert percentile([5.0], 0.75) == 5.0
