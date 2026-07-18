from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from backtest.calibration import (
    brier_score,
    bucket_table,
    decile_index,
    decile_table,
    flat_stake_pnl,
    summarize,
)


def picks(*pairs):
    return [{"model_prob": p, "outcome": o} for p, o in pairs]


class TestBrier:
    def test_perfect_forecasts_score_zero(self):
        assert brier_score(picks((1.0, 1), (0.0, 0))) == 0.0

    def test_maximally_wrong_scores_one(self):
        assert brier_score(picks((1.0, 0), (0.0, 1))) == 1.0

    def test_coin_flip_on_balanced_set_scores_quarter(self):
        assert brier_score(picks((0.5, 1), (0.5, 0))) == pytest.approx(0.25)

    def test_known_hand_computed_value(self):
        # (0.7-1)^2=0.09, (0.3-0)^2=0.09, (0.6-0)^2=0.36 -> mean 0.18
        assert brier_score(picks((0.7, 1), (0.3, 0), (0.6, 0))) == pytest.approx(0.18)

    def test_empty_is_none(self):
        assert brier_score([]) is None


class TestSummarize:
    def test_counts_means_and_gap(self):
        stats = summarize(picks((0.6, 1), (0.4, 0), (0.5, 0), (0.5, 1)))
        assert stats["n"] == 4
        assert stats["avg_model_prob"] == pytest.approx(0.5)
        assert stats["hit_rate"] == pytest.approx(0.5)
        assert stats["gap_pp"] == pytest.approx(0.0)

    def test_overconfident_model_has_positive_gap(self):
        stats = summarize(picks((0.8, 0), (0.8, 1)))
        assert stats["gap_pp"] == pytest.approx(30.0)  # 80% claimed, 50% actual

    def test_empty_summary(self):
        stats = summarize([])
        assert stats == {
            "n": 0, "avg_model_prob": None, "hit_rate": None, "gap_pp": None, "brier": None,
        }


class TestBucketTable:
    def test_groups_by_market_and_orders_by_size(self):
        rows = [
            {"market": "HR", "model_prob": 0.3, "outcome": 0},
            {"market": "ML", "model_prob": 0.6, "outcome": 1},
            {"market": "HR", "model_prob": 0.2, "outcome": 1},
            {"market": "HR", "model_prob": 0.4, "outcome": 0},
        ]
        table = bucket_table(rows)
        assert list(table) == ["HR", "ML"]
        assert table["HR"]["n"] == 3
        assert table["HR"]["avg_model_prob"] == pytest.approx(0.3)
        assert table["HR"]["hit_rate"] == pytest.approx(1 / 3, abs=1e-4)
        assert table["ML"]["n"] == 1

    def test_size_ties_break_alphabetically(self):
        rows = [
            {"market": "PTS", "model_prob": 0.5, "outcome": 1},
            {"market": "AST", "model_prob": 0.5, "outcome": 0},
        ]
        assert list(bucket_table(rows)) == ["AST", "PTS"]


class TestDeciles:
    def test_bin_edges(self):
        assert decile_index(0.0) == 0
        assert decile_index(0.0999) == 0
        assert decile_index(0.1) == 1
        assert decile_index(0.95) == 9
        assert decile_index(1.0) == 9  # top bin is closed

    def test_out_of_range_probs_clamp(self):
        assert decile_index(-0.2) == 0
        assert decile_index(1.7) == 9

    def test_table_has_ten_rows_and_places_picks(self):
        rows = decile_table(picks((0.05, 0), (0.35, 1), (0.35, 0), (0.95, 1)))
        assert len(rows) == 10
        assert [r["n"] for r in rows] == [1, 0, 0, 2, 0, 0, 0, 0, 0, 1]
        third = rows[3]
        assert third["lo"] == 0.3 and third["hi"] == 0.4
        assert third["avg_model_prob"] == pytest.approx(0.35)
        assert third["hit_rate"] == pytest.approx(0.5)

    def test_empty_bins_report_zero_n(self):
        rows = decile_table([])
        assert all(r["n"] == 0 and r["hit_rate"] is None for r in rows)


class TestFlatStakePnl:
    def test_win_pays_market_odds_loss_costs_stake(self):
        # $5 YES at 0.50: win pays +5, loss -5
        result = flat_stake_pnl(
            [{"implied_prob": 0.5, "outcome": 1}, {"implied_prob": 0.5, "outcome": 0}],
            stake=5.0,
        )
        assert result["n"] == 2 and result["wins"] == 1
        assert result["pnl"] == pytest.approx(0.0)
        assert result["roi"] == pytest.approx(0.0)

    def test_longshot_win_pays_more(self):
        result = flat_stake_pnl([{"implied_prob": 0.25, "outcome": 1}], stake=5.0)
        assert result["pnl"] == pytest.approx(15.0)  # 5 * 0.75 / 0.25
        assert result["roi"] == pytest.approx(3.0)

    def test_unpriced_bets_are_skipped_not_counted(self):
        result = flat_stake_pnl(
            [{"implied_prob": None, "outcome": 1}, {"implied_prob": 1.0, "outcome": 1}],
            stake=5.0,
        )
        assert result == {
            "n": 0, "wins": 0, "losses": 0, "skipped": 2, "stake": 5.0,
            "staked": 0.0, "pnl": 0.0, "roi": None,
        }

    def test_empty(self):
        assert flat_stake_pnl([], stake=5.0)["roi"] is None
