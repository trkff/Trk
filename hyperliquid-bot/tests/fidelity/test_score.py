"""Unit tests for score formula and cause heuristics."""
from bot.fidelity.checker import fidelity_score, attribute_cause


def test_perfect_score_is_one():
    s = fidelity_score(
        signal_counts={"matched": 10, "live_signals": 10, "bt_signals": 10,
                       "price_drift": 0, "indicator_drift": 0},
        trade_outcome_match_rate=1.0,
    )
    assert s == 1.0


def test_zero_match_is_low():
    s = fidelity_score(
        signal_counts={"matched": 0, "live_signals": 10, "bt_signals": 10,
                       "price_drift": 0, "indicator_drift": 0},
        trade_outcome_match_rate=0.0,
    )
    assert s < 0.5


def test_score_components_independent():
    # All matched but 50% have price drift
    s = fidelity_score(
        signal_counts={"matched": 10, "live_signals": 10, "bt_signals": 10,
                       "price_drift": 5, "indicator_drift": 0},
        trade_outcome_match_rate=1.0,
    )
    # match=1.0×0.50  + price=(1-0.5)×0.20  + ind=1.0×0.15  + trades=1.0×0.15  = 0.90
    assert abs(s - 0.90) < 1e-6


def test_attribute_cause_phantom_with_nearby_indicator_drift():
    diff = {"diff_type": "phantom", "ts_ms": 1000}
    siblings = [{"diff_type": "indicator", "ts_ms": 1000, "notes": "indicator=bbp"}]
    cause = attribute_cause(diff, siblings, live_signal=None)
    assert "indicador" in cause.lower()


def test_attribute_cause_missed_with_block_reason():
    diff = {"diff_type": "missed", "ts_ms": 1000}
    live_signal = {"reason": "Funding rate limit exceeded"}
    cause = attribute_cause(diff, [], live_signal=live_signal)
    assert "Funding" in cause


def test_attribute_cause_price_drift_default():
    diff = {"diff_type": "price", "ts_ms": 1000}
    cause = attribute_cause(diff, [], live_signal=None)
    assert "vela" in cause.lower() or "candle" in cause.lower()
