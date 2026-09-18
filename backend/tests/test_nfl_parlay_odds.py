from app.sim.nfl_parlays import _estimated_return, _reference_calibrated_odds


def test_reference_slip_odds_are_exact_anchors():
    assert _reference_calibrated_odds(9) == 2363
    assert _reference_calibrated_odds(14) == 11496


def test_four_leg_grind_uses_same_compounding_curve():
    assert _reference_calibrated_odds(4) == 423


def test_total_return_includes_stake():
    assert _estimated_return(25, 11496) == 2899.0
