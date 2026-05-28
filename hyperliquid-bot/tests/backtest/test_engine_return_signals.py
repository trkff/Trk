"""Tests for engine._run_backtest's return_signals=True mode."""
import numpy as np
import pandas as pd
import pandas_ta as ta

from bot.backtest import engine


def _synth_arrays(n=300, seed=1):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.005, n)
    closes = 100.0 * np.exp(np.cumsum(rets))
    highs = closes * (1 + np.abs(rng.normal(0, 0.002, n)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.002, n)))
    return closes, highs, lows


def test_snapshot_bb_stoch_keys_match_live():
    close, high, low = _synth_arrays()
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"bb_period": 15, "bb_std": 1.5, "stoch_k": 14, "stoch_d": 3, "ema_period": 0}
    snap = engine._snapshot_bb_stoch(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "bbp", "bbm", "bbu", "bbl", "stoch_k", "stoch_d"}


def test_snapshot_bb_reversion_keys_match_live():
    close, high, low = _synth_arrays(seed=2)
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"bb_period": 10, "bb_std": 2.0, "ema_period": 50}
    snap = engine._snapshot_bb_reversion(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "bbp", "bbm", "bbu", "bbl", "rsi", "ema"}


def test_snapshot_stoch_scalp_keys():
    close, high, low = _synth_arrays(seed=3)
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"stoch_k": 9, "stoch_d": 3, "ema_period": 50}
    snap = engine._snapshot_stoch_scalp(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "stoch_k", "stoch_d", "stoch_k_prev", "stoch_d_prev"}


def test_snapshot_ema_cross_keys():
    close, high, low = _synth_arrays(seed=4)
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"ema_fast": 9, "ema_slow": 21, "ema_trend": 0, "use_atr_sl": False, "atr_period": 14}
    snap = engine._snapshot_ema_cross(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "ema_fast", "ema_slow", "ema_fast_prev", "ema_slow_prev"}


def test_snapshot_macd_cross_keys():
    close, high, low = _synth_arrays(seed=5)
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "ema_trend": 0}
    snap = engine._snapshot_macd_cross(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "macd", "macd_signal", "macd_prev", "macd_signal_prev"}


def test_snapshot_rsi_scalp_keys():
    close, high, low = _synth_arrays(seed=6)
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"rsi_period": 14, "ema_period": 0}
    snap = engine._snapshot_rsi_scalp(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "rsi", "rsi_prev"}


def test_snapshot_bb_rsi_keys():
    close, high, low = _synth_arrays(seed=7)
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"bb_period": 15, "bb_std": 1.5, "rsi_period": 14, "ema_period": 0}
    snap = engine._snapshot_bb_rsi(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "bbp", "bbu", "bbl", "rsi"}


def test_snapshot_williams_r_keys():
    close, high, low = _synth_arrays(seed=8)
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"wr_period": 14, "ema_period": 0}
    snap = engine._snapshot_williams_r(close, high, low, close_s, high_s, low_s, params)
    out = snap(100)
    assert set(out.keys()) >= {"close", "wr", "wr_prev"}


def test_safe_handles_nan_and_inf():
    arr = np.array([1.0, np.nan, np.inf, -np.inf, 2.5])
    assert engine._safe(arr, 0) == 1.0
    assert engine._safe(arr, 1) is None
    assert engine._safe(arr, 2) is None
    assert engine._safe(arr, 3) is None
    assert engine._safe(arr, 4) == 2.5
    assert engine._safe(None, 0) is None


def test_all_families_registered_in_snapshot_fns():
    """Every family in _FAMILY_FNS must have a matching snapshot factory."""
    for fam in engine._FAMILY_FNS:
        assert fam in engine._FAMILY_SNAPSHOT_FNS, f"Missing snapshot for {fam}"
