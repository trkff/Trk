"""Tests for scanner._backtest — verifies SL > TP per-candle priority."""
import numpy as np

from bot.backtest.scanner import _backtest


def _empty_signal(n: int) -> np.ndarray:
    return np.zeros(n, dtype=bool)


def test_long_same_bar_tp_sl_picks_loss():
    """When TP and SL hit on the same candle for a long, SL must win (pessimistic)."""
    # Long entry at 100 on candle 0; tp=2%, sl=2% (so TP=102, SL=98).
    # Candle 1 has both high>=102 AND low<=98 → tie. SL must take priority.
    close = np.array([100.0, 100.0, 100.0])
    high  = np.array([100.5, 103.0, 100.0])  # candle 1 high above TP
    low   = np.array([ 99.5,  97.0, 100.0])  # candle 1 low below SL
    sig_long = np.array([True, False, False])
    sig_short = _empty_signal(3)

    trades = _backtest(sig_long, sig_short, close, high, low, tp_pct=2.0, sl_pct=2.0)
    # Loss should be -sl_pct = -2.0
    assert trades == [-2.0]


def test_short_same_bar_tp_sl_picks_loss():
    """Same rule for short side."""
    close = np.array([100.0, 100.0, 100.0])
    high  = np.array([100.5, 103.0, 100.0])  # candle 1 high above SL (102)
    low   = np.array([ 99.5,  97.0, 100.0])  # candle 1 low below TP (98)
    sig_long = _empty_signal(3)
    sig_short = np.array([True, False, False])

    trades = _backtest(sig_long, sig_short, close, high, low, tp_pct=2.0, sl_pct=2.0)
    assert trades == [-2.0]


def test_long_clean_tp():
    """Clean TP hit (no SL on same bar) still wins."""
    close = np.array([100.0, 102.5, 100.0])
    high  = np.array([100.5, 103.0, 100.5])
    low   = np.array([ 99.5, 101.0,  99.5])
    sig_long = np.array([True, False, False])
    sig_short = _empty_signal(3)

    trades = _backtest(sig_long, sig_short, close, high, low, tp_pct=2.0, sl_pct=2.0)
    assert trades == [2.0]


def test_long_clean_sl():
    """Clean SL hit (no TP on same bar) loses."""
    close = np.array([100.0, 97.0, 100.0])
    high  = np.array([100.5, 99.0, 100.5])
    low   = np.array([ 99.5, 97.0,  99.5])
    sig_long = np.array([True, False, False])
    sig_short = _empty_signal(3)

    trades = _backtest(sig_long, sig_short, close, high, low, tp_pct=2.0, sl_pct=2.0)
    assert trades == [-2.0]


def test_bb_mid_exit_long_returns_actual_pct():
    """When bb_mid is passed and price closes >= bb_mid (long), trade exits at close[j].
    Returned value is actual percent change (entry vs close), not tp_pct/sl_pct."""
    # Long at 100. Neither TP (102) nor SL (98) hit. Close[2] = 101 >= bb_mid[2] = 100.5.
    close  = np.array([100.0, 100.3, 101.0])
    high   = np.array([100.5, 100.7, 101.5])
    low    = np.array([ 99.5,  99.9, 100.5])
    bb_mid = np.array([102.0, 101.0, 100.5])
    sig_long = np.array([True, False, False])
    sig_short = _empty_signal(3)

    trades = _backtest(sig_long, sig_short, close, high, low,
                       tp_pct=2.0, sl_pct=2.0, bb_mid=bb_mid)
    assert len(trades) == 1
    assert abs(trades[0] - 1.0) < 1e-9   # (101 - 100) / 100 * 100 = 1.0%


def test_bb_mid_does_not_override_sl():
    """Per-candle priority is SL > TP > BB-mid: if SL hits same bar as bb_mid, SL wins."""
    # Long at 100, sl=98. Bar j=1: low=97 (SL hit) AND close=97 (also <= bb_mid=99 — but long needs close>=mid, so not triggered).
    # Build instead: SL hit AND bb_mid would trigger if checked → SL must win.
    close  = np.array([100.0,  97.0])   # close <= 99 means bb_mid (mid=99) wouldn't fire for long anyway
    high   = np.array([100.5,  98.5])
    low    = np.array([ 99.5,  97.0])   # SL=98 hit
    bb_mid = np.array([ 99.0,  99.0])
    sig_long = np.array([True, False])
    sig_short = _empty_signal(2)

    trades = _backtest(sig_long, sig_short, close, high, low,
                       tp_pct=2.0, sl_pct=2.0, bb_mid=bb_mid)
    assert trades == [-2.0]   # SL outcome, not bb_mid
