"""Tests for scanner_v2 — new filter dims (ADX, session, ATR) + WFO."""
import numpy as np

from bot.backtest import scanner_v2


def _empty(n: int) -> np.ndarray:
    return np.zeros(n, dtype=bool)


def test_module_imports_and_reexports():
    assert callable(scanner_v2._load_csv)
    assert callable(scanner_v2._stats)
    assert isinstance(scanner_v2.APPROVAL, dict)
    assert "5m" in scanner_v2.SUPPORTED_TIMEFRAMES
    assert callable(scanner_v2._backtest_v2)


def test_backtest_v2_matches_legacy_when_no_extras():
    """With tp_dist=sl_dist=session=adx=None, _backtest_v2 must match scanner._backtest."""
    from bot.backtest.scanner import _backtest

    rng = np.random.default_rng(0)
    n = 200
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 0.3
    low = close - 0.3
    sig_long = rng.random(n) < 0.05
    sig_short = rng.random(n) < 0.05

    legacy = _backtest(sig_long, sig_short, close, high, low, tp_pct=1.0, sl_pct=1.0)
    v2 = scanner_v2._backtest_v2(sig_long, sig_short, close, high, low, tp_pct=1.0, sl_pct=1.0)
    assert v2 == legacy


def test_backtest_v2_tp_dist_overrides_pct():
    close = np.array([100.0, 100.0, 100.0, 100.0])
    high = np.array([100.5, 100.5, 106.0, 100.0])
    low = np.array([99.5, 99.5, 100.0, 100.0])
    sig_long = np.array([True, False, False, False])
    sig_short = _empty(4)
    tp_dist = np.array([5.0, np.nan, np.nan, np.nan])

    trades = scanner_v2._backtest_v2(
        sig_long, sig_short, close, high, low,
        tp_pct=999.0, sl_pct=999.0, tp_dist=tp_dist,
    )
    assert len(trades) == 1
    ret, idx = trades[0]
    assert abs(ret - 5.0) < 1e-9
    assert idx == 0


def test_backtest_v2_sl_dist_overrides_pct():
    close = np.array([100.0, 100.0, 100.0, 100.0])
    high = np.array([100.5, 100.5, 100.5, 100.0])
    low = np.array([99.5, 99.5, 96.0, 100.0])
    sig_long = np.array([True, False, False, False])
    sig_short = _empty(4)
    sl_dist = np.array([3.0, np.nan, np.nan, np.nan])

    trades = scanner_v2._backtest_v2(
        sig_long, sig_short, close, high, low,
        tp_pct=999.0, sl_pct=999.0, sl_dist=sl_dist,
    )
    assert len(trades) == 1
    ret, _ = trades[0]
    assert abs(ret - (-3.0)) < 1e-9


def test_backtest_v2_session_mask_blocks_entries():
    close = np.array([100.0, 102.0, 100.0])
    high = np.array([100.5, 103.0, 100.5])
    low = np.array([99.5, 101.0, 99.5])
    sig_long = np.array([True, False, False])
    sig_short = _empty(3)
    mask = np.zeros(3, dtype=bool)

    trades = scanner_v2._backtest_v2(
        sig_long, sig_short, close, high, low,
        tp_pct=1.0, sl_pct=1.0, session_mask=mask,
    )
    assert trades == []


def test_backtest_v2_adx_mask_blocks_entries():
    close = np.array([100.0, 102.0, 100.0])
    high = np.array([100.5, 103.0, 100.5])
    low = np.array([99.5, 101.0, 99.5])
    sig_long = np.array([True, False, False])
    sig_short = _empty(3)
    mask = np.zeros(3, dtype=bool)

    trades = scanner_v2._backtest_v2(
        sig_long, sig_short, close, high, low,
        tp_pct=1.0, sl_pct=1.0, adx_mask=mask,
    )
    assert trades == []
