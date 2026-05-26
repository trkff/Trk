"""Trend-following families (EMA_Cross, MACD_Cross) — schema + ADX semantics."""
import numpy as np
import pandas as pd

from bot.backtest import scanner_v2


def _synthetic(n=600):
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 0.5
    low = close - 0.5
    start = pd.Timestamp("2026-01-01T00:00:00Z").value // 1_000_000
    step = 5 * 60 * 1000
    ts = np.array([start + i * step for i in range(n)], dtype=np.int64)
    return close, high, low, ts


_NEW_FIELDS = ("adx_period", "adx_min", "session_start", "session_end",
               "atr_tp_mode", "atr_tp_mult", "atr_sl_mult")


def test_trend_families_use_adx_geq_threshold():
    close, high, low, ts_ms = _synthetic()
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    for label, fn in [("EMA_Cross", scanner_v2._scan_ema_cross),
                      ("MACD_Cross", scanner_v2._scan_macd_cross)]:
        results = fn(close, high, low, len(close), close_s, high_s, low_s,
                     cb=None, ts_ms=ts_ms, max_combos=300)
        for r in results:
            assert r["strategy"] == label
            for k in _NEW_FIELDS:
                assert k in r
