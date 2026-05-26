"""End-to-end test: scanner_v2 mean-reversion families on synthetic data."""
import numpy as np
import pandas as pd

from bot.backtest import scanner_v2


def _synthetic_df(n=600):
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 0.5
    low = close - 0.5
    start = pd.Timestamp("2026-01-01T00:00:00Z").value // 1_000_000
    step = 5 * 60 * 1000
    ts = np.array([start + i * step for i in range(n)], dtype=np.int64)
    return pd.DataFrame({
        "ts_ms": ts, "open": close, "high": high, "low": low,
        "close": close, "volume": np.ones(n),
    })


def _arrays(df):
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    ts_ms = df["ts_ms"].values.astype(np.int64)
    return close, high, low, ts_ms, pd.Series(close), pd.Series(high), pd.Series(low)


_NEW_FIELDS = ("adx_period", "adx_min", "session_start", "session_end",
               "atr_tp_mode", "atr_tp_mult", "atr_sl_mult")


def test_bb_stoch_v2_returns_results_with_new_fields():
    df = _synthetic_df()
    close, high, low, ts_ms, close_s, high_s, low_s = _arrays(df)
    results = scanner_v2._scan_bb_stoch(
        close, high, low, len(close), close_s, high_s, low_s, cb=None,
        ts_ms=ts_ms, max_combos=300,
    )
    for r in results:
        assert r["strategy"] == "BB_Stoch"
        for k in _NEW_FIELDS:
            assert k in r


def test_all_mean_reversion_families_return_new_schema():
    df = _synthetic_df()
    close, high, low, ts_ms, close_s, high_s, low_s = _arrays(df)
    fns = [
        ("BB_Reversion", scanner_v2._scan_bb_reversion),
        ("BB_RSI", scanner_v2._scan_bb_rsi),
        ("RSI_Scalp", scanner_v2._scan_rsi_scalp),
        ("Stoch_Scalp", scanner_v2._scan_stoch_scalp),
        ("Williams_R", scanner_v2._scan_williams_r),
    ]
    for label, fn in fns:
        results = fn(close, high, low, len(close), close_s, high_s, low_s,
                     cb=None, ts_ms=ts_ms, max_combos=200)
        for r in results:
            assert r["strategy"] == label
            for k in _NEW_FIELDS:
                assert k in r
