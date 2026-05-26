"""Replay parity test — for every family, _replay_params_on_slice must produce
the same trade count and ROI as the matching _scan_* function on the same data.
Catches drift where _scan_* signal logic changes but the replay dispatch doesn't.
"""
import numpy as np
import pandas as pd
import pytest

from bot.backtest import scanner_v2


def _synthetic(n=800, seed=11):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 0.5
    low = close - 0.5
    start = pd.Timestamp("2026-01-01T00:00:00Z").value // 1_000_000
    step = 5 * 60 * 1000
    ts = np.array([start + i * step for i in range(n)], dtype=np.int64)
    return close, high, low, ts


@pytest.mark.parametrize("family", [
    "BB_Stoch", "BB_Reversion", "BB_RSI",
    "RSI_Scalp", "Stoch_Scalp", "Williams_R",
    "EMA_Cross", "MACD_Cross",
])
def test_replay_matches_scan_for_every_family(family):
    close, high, low, ts_ms = _synthetic()
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    fn = scanner_v2._SCANNERS_V2[family]
    results = fn(close, high, low, len(close), close_s, high_s, low_s,
                 cb=None, ts_ms=ts_ms, max_combos=400)
    if not results:
        pytest.skip(f"{family}: no results from scan on synthetic data")
    row = results[0]
    replayed = scanner_v2._replay_params_on_slice(
        close, high, low, ts_ms, close_s, high_s, low_s, mins=5, params=row,
    )
    assert replayed is not None
    assert replayed["trades"] == row["trades"], \
        f"{family}: trade count mismatch — scan={row['trades']} replay={replayed['trades']}"
    assert abs(replayed["roi"] - row["roi"]) < 1e-6, \
        f"{family}: ROI mismatch — scan={row['roi']} replay={replayed['roi']}"
