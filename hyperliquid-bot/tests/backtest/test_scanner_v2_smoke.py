"""Smoke tests for run_scan_v2 and run_scan_wfo against a real BTC CSV."""
from pathlib import Path

import pytest

from bot.backtest import scanner_v2

_CSV = Path(__file__).parents[3] / "candles" / "btc_5m.csv"


@pytest.mark.skipif(not _CSV.exists(), reason="BTC 5m CSV not present")
def test_run_scan_v2_btc_short_window():
    result = scanner_v2.run_scan_v2(
        "BTC", days=30, strategies=["BB_Stoch"], timeframe="5m",
        max_combos_per_family=200,
    )
    assert "error" not in result
    assert result["asset"] == "BTC"
    assert result["timeframe"] == "5m"
    assert result["total_tested"] > 0
    for r in result["approved"]:
        for k in ("adx_period", "adx_min", "session_start", "session_end",
                  "atr_tp_mode", "atr_tp_mult", "atr_sl_mult"):
            assert k in r


@pytest.mark.skipif(not _CSV.exists(), reason="BTC 5m CSV not present")
def test_run_scan_wfo_btc():
    result = scanner_v2.run_scan_wfo(
        "BTC", total_days=60, n_windows=2, train_ratio=0.7,
        strategies=["BB_Stoch"], timeframe="5m",
        top_n=3, max_combos_per_family=100,
    )
    assert "error" not in result
    assert len(result["windows"]) == 2
    for w in result["windows"]:
        assert "is" in w and "oos" in w
    assert "wfo_efficiency" in result
