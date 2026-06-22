from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.collectors.soccer_collector import (
    SOCCER_LEAGUES,
    build_team_player_candidates,
    calibrate_match_profile,
    extract_three_way_market,
    poisson_at_least,
    poisson_outcome_probabilities,
)


def test_world_cup_is_collected_and_accepts_curved_one_match_sample():
    assert SOCCER_LEAGUES[0] == {"slug": "fifa.world", "label": "FIFA World Cup"}
    athlete = {
        "id": "10",
        "displayName": "Tournament Forward",
        "status": {"type": "active"},
        "injuries": [],
        "position": {"abbreviation": "F"},
        "statistics": {
            "splits": {
                "categories": [
                    {
                        "stats": [
                            {"name": "appearances", "value": 1},
                            {"name": "totalGoals", "value": 1},
                            {"name": "goalAssists", "value": 0},
                            {"name": "shotsOnTarget", "value": 2},
                            {"name": "totalShots", "value": 3},
                        ]
                    }
                ]
            }
        },
    }
    rows = build_team_player_candidates(
        game_id="wc-1",
        market_team="home",
        team={"abbreviation": "AAA"},
        opponent={"abbreviation": "BBB"},
        roster=[athlete],
        team_form={"goals_for_per_match": 1.8},
        opponent_form={"goals_against_per_match": 1.5},
        baseline={"goals_for": 1.2, "goals_against": 1.2, "points": 1.4},
        is_home=True,
        minimum_appearances=1,
        competition_label="FIFA World Cup",
    )

    assert any(row["market"] == "GS" for row in rows)
    assert any(row["market"] == "SHOTS" for row in rows)
    assert any(row["market"] == "SOT" for row in rows)
    assert all("Sample 1 | FIFA World Cup" in row["reason"] for row in rows)
    assert all(0.0 < row["model_hit_rate"] < 1.0 for row in rows)


def test_poisson_and_market_probabilities_are_normalized():
    outcomes = poisson_outcome_probabilities(1.8, 0.8)
    assert abs(sum(outcomes.values()) - 1.0) < 1e-9
    assert outcomes["home"] > outcomes["away"]
    assert 0.0 < poisson_at_least(2.4, 3) < 1.0

    market, prices = extract_three_way_market({
        "moneyline": {
            "home": {"close": {"odds": "-225"}},
            "draw": {"close": {"odds": "+350"}},
            "away": {"close": {"odds": "+700"}},
        }
    })
    assert prices["home"] == "-225"
    assert market is not None
    assert abs(sum(market.values()) - 1.0) < 1e-9
    assert market["home"] > market["draw"] > market["away"]

    calibrated = calibrate_match_profile(
        {"home_xg": 2.2, "away_xg": 1.6},
        {
            "overUnder": 3.5,
            "moneyline": {
                "home": {"close": {"odds": "-1000"}},
                "draw": {"close": {"odds": "+1000"}},
                "away": {"close": {"odds": "+2500"}},
            },
            "total": {
                "over": {"close": {"odds": "-120"}},
                "under": {"close": {"odds": "+100"}},
            },
        },
    )
    assert calibrated["home_xg"] > calibrated["away_xg"] * 2.5
    btts_yes = (1.0 - __import__("math").exp(-calibrated["home_xg"])) * (1.0 - __import__("math").exp(-calibrated["away_xg"]))
    assert btts_yes < 0.60
