"""Unit tests for the signal-layer diff (checker.diff_signals)."""
import json

from bot.fidelity.checker import diff_signals, PRICE_TOL


def _live(ts, side, price, indicators=None):
    return {
        "ts_ms": ts, "side": side, "signal_price": price,
        "indicators_json": json.dumps(indicators) if indicators else None,
    }


def _bt(ts, side, price, indicators):
    return {"ts_ms": ts, "side": side, "signal_price": price, "indicators": indicators}


def test_exact_match_is_matched():
    live = [_live(1000, "long", 100.0, {"bbp": 0.05})]
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["matched"] == 1
    assert out["diffs"] == []


def test_phantom_when_live_only():
    live = [_live(1000, "long", 100.0, {"bbp": 0.05})]
    bt = []
    out = diff_signals(live, bt)
    assert out["phantom"] == 1
    assert any(d["diff_type"] == "phantom" for d in out["diffs"])


def test_missed_when_bt_only():
    live = []
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["missed"] == 1
    assert any(d["diff_type"] == "missed" for d in out["diffs"])


def test_side_mismatch():
    live = [_live(1000, "long", 100.0)]
    bt = [_bt(1000, "short", 100.0, {})]
    out = diff_signals(live, bt)
    assert out["side_mismatch"] == 1


def test_price_drift_above_tolerance():
    live = [_live(1000, "long", 100.10, {"bbp": 0.05})]   # 0.1% > 0.05% tol
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["price_drift"] == 1
    d = next(d for d in out["diffs"] if d["diff_type"] == "price")
    assert d["delta_pct"] is not None and d["delta_pct"] > PRICE_TOL


def test_price_drift_within_tolerance_is_matched():
    live = [_live(1000, "long", 100.02, {"bbp": 0.05})]   # 0.02% < 0.05%
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["price_drift"] == 0
    assert out["matched"] == 1


def test_indicator_drift():
    live = [_live(1000, "long", 100.0, {"bbp": 0.05, "stoch_k": 20.0})]
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.06, "stoch_k": 25.0})]
    out = diff_signals(live, bt)
    assert out["indicator_drift"] >= 1
    inds = [d for d in out["diffs"] if d["diff_type"] == "indicator"]
    assert any("bbp" in (d.get("notes") or "") for d in inds)


def test_indicator_drift_skipped_when_live_lacks_indicators():
    live = [_live(1000, "long", 100.0, indicators=None)]
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.06})]
    out = diff_signals(live, bt)
    assert out["indicator_drift"] == 0
    assert out["matched"] == 1
