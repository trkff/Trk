import numpy as np
import pytest

from bot.backtest.engine import _simulate_fast


def _make_ts(n: int, start_ms: int = 1_700_000_000_000) -> np.ndarray:
    return start_ms + np.arange(n, dtype=np.int64) * 300_000  # 5m candles


def test_simulate_long_tp_hit():
    # 5 candles. Signal at index 0 (long), TP hit at index 2.
    close = np.array([100.0, 100.5, 102.0, 101.0, 100.0])
    high  = np.array([100.5, 101.0, 102.5, 101.5, 100.5])
    low   = np.array([ 99.5,  99.8, 100.5, 100.0,  99.0])
    ts    = _make_ts(5)
    sig_long  = np.array([True, False, False, False, False])
    sig_short = np.zeros(5, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=2.0, sl_pct=2.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"
    assert t["outcome"] == "tp"
    assert t["entry_price"] == 100.0
    assert t["tp"] == pytest.approx(102.0)
    assert t["sl"] == pytest.approx(98.0)
    assert t["exit_price"] == pytest.approx(102.0)
    assert t["candles_held"] == 2


def test_simulate_short_sl_hit():
    close = np.array([100.0, 100.5, 102.5, 102.0, 101.0])
    high  = np.array([100.5, 101.0, 102.8, 102.5, 101.5])
    low   = np.array([ 99.8,  99.9, 102.2, 101.5, 100.5])
    ts    = _make_ts(5)
    sig_long  = np.zeros(5, dtype=bool)
    sig_short = np.array([True, False, False, False, False])

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=5.0, sl_pct=2.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "short"
    assert t["outcome"] == "sl"
    assert t["sl"] == pytest.approx(102.0)
    assert t["candles_held"] == 2


def test_simulate_no_hit_discarded():
    # Trade entered but neither TP nor SL ever hit — must be discarded.
    close = np.array([100.0, 100.1, 100.05, 99.95, 100.02])
    high  = np.array([100.2, 100.3, 100.15, 100.05, 100.10])
    low   = np.array([ 99.8,  99.9, 99.95, 99.85, 99.95])
    ts    = _make_ts(5)
    sig_long  = np.array([True, False, False, False, False])
    sig_short = np.zeros(5, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=5.0, sl_pct=5.0)
    assert trades == []


def test_simulate_no_overlap():
    # Two signals; second is inside first trade's holding window — must be ignored.
    close = np.array([100.0, 100.5, 102.0, 101.0, 103.0, 105.0])
    high  = np.array([100.5, 101.0, 102.5, 101.5, 103.5, 105.5])
    low   = np.array([ 99.5,  99.8, 100.5, 100.0, 102.5, 104.0])
    ts    = _make_ts(6)
    sig_long  = np.array([True, True, False, False, False, False])
    sig_short = np.zeros(6, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=2.0, sl_pct=2.0)
    assert len(trades) == 1  # second signal at i=1 falls inside first trade


def test_simulate_bb_mid_exit_long_wins():
    # Long entry at 100, bb_mid starts at 102. Price climbs to 101 (not TP), then closes
    # above bb_mid (which has fallen to 100.5). BB mid hits before TP.
    close = np.array([100.0, 100.5, 100.8, 101.2, 102.0])
    high  = np.array([100.5, 100.8, 101.0, 101.5, 102.2])
    low   = np.array([ 99.5,  99.9, 100.3, 100.8, 101.5])
    ts    = _make_ts(5)
    bb_mid = np.array([102.0, 101.5, 101.0, 100.5, 100.5])
    sig_long  = np.array([True, False, False, False, False])
    sig_short = np.zeros(5, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=5.0, sl_pct=5.0, bb_mid=bb_mid)
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "bb_mid"
    # At candle index 3, close=101.2 >= bb_mid=100.5 → bb_mid exit fires
    assert t["candles_held"] == 3
    assert t["exit_price"] == pytest.approx(101.2)


def test_signals_bb_stoch_basic():
    import pandas as pd
    from bot.backtest.engine import _signals_bb_stoch

    # Build 100 candles with a synthetic oversold dip
    n = 100
    close = np.full(n, 100.0)
    close[50:55] = [98.0, 96.0, 94.0, 92.0, 90.0]   # sharp drop
    high = close + 0.5
    low = close - 0.5
    close_s = pd.Series(close)
    high_s = pd.Series(high)
    low_s = pd.Series(low)

    params = {
        "bb_period": 15, "bb_std": 1.5,
        "stoch_k": 14, "stoch_d": 3,
        "stoch_long": 25, "stoch_short": 75,
        "bbp_long_threshold": 0.1, "bbp_short_threshold": 0.9,
        "ema_period": 0, "bb_mid_exit": False,
    }
    sig_long, sig_short, bb_mid, sl_dist = _signals_bb_stoch(
        close, high, low, close_s, high_s, low_s, params)

    assert sig_long.shape == (n,)
    assert sig_short.shape == (n,)
    assert bb_mid is None
    assert sl_dist is None
    # Drop region should trigger at least one long
    assert sig_long[50:60].any()


def test_signals_bb_stoch_returns_bb_mid_when_enabled():
    import pandas as pd
    from bot.backtest.engine import _signals_bb_stoch
    close = np.linspace(100, 110, 100)
    close_s = pd.Series(close)
    params = {
        "bb_period": 15, "bb_std": 1.5, "stoch_k": 14, "stoch_d": 3,
        "stoch_long": 25, "stoch_short": 75,
        "bbp_long_threshold": 0.1, "bbp_short_threshold": 0.9,
        "ema_period": 0, "bb_mid_exit": True,
    }
    _, _, bb_mid, _ = _signals_bb_stoch(
        close, close + 0.5, close - 0.5, close_s, pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert bb_mid is not None
    assert bb_mid.shape == (100,)


def test_run_backtest_bb_stoch_btc_smoke():
    """Runs the full fast engine on bb_stoch_btc, 30 days. Asserts shape only."""
    import os
    from pathlib import Path
    csv_path = Path(__file__).parents[2].parent / "candles" / "btc_5m.csv"
    if not csv_path.exists():
        pytest.skip("btc_5m.csv not present in candles/")

    from bot.backtest.engine import _run_backtest
    result = _run_backtest("bb_stoch_btc", "BTC", days=30,
                                trade_size_usd=1000.0, fee_rate=0.0)
    assert "trades" in result
    assert "metrics" in result
    assert isinstance(result["trades"], list)
    assert result["strategy_resolved"] == "bb_stoch_btc"
    # Sanity: every trade has the required keys
    for t in result["trades"]:
        for k in ("entry_time","exit_time","side","entry_price","exit_price",
                  "tp","sl","outcome","candles_held","pnl","pnl_pct"):
            assert k in t, f"Trade missing key: {k}"


def test_signals_bb_reversion_shape():
    import pandas as pd
    from bot.backtest.engine import _signals_bb_reversion
    n = 100
    close = np.full(n, 100.0)
    close[50:55] = [98.0, 96.0, 94.0, 92.0, 90.0]
    close[55:60] = [92.0, 94.0, 96.0, 98.0, 99.5]  # mean reversion
    close_s = pd.Series(close)
    params = {
        "bb_period": 10, "bb_std": 2.0,
        "bbp_long_threshold": 0.10, "bbp_short_threshold": 0.90,
        "ema_period": 0, "rsi_long_max": 65, "rsi_short_min": 35,
        "bb_mid_exit": True,
    }
    sl, ss, bbm, sd = _signals_bb_reversion(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,) and ss.shape == (n,)
    assert bbm is not None  # bb_mid_exit=True returns BBM array
    assert sd is None


def test_signals_stoch_scalp_shape():
    import pandas as pd
    from bot.backtest.engine import _signals_stoch_scalp
    n = 200
    rng = np.random.default_rng(0)
    close = 100 + rng.normal(0, 1, n).cumsum() * 0.1
    close_s = pd.Series(close)
    params = {"stoch_k": 9, "stoch_d": 3, "stoch_os": 40, "ema_period": 0}
    sl, ss, bbm, sd = _signals_stoch_scalp(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)
    assert bbm is None and sd is None


def test_signals_ema_cross_atr_sl():
    import pandas as pd
    from bot.backtest.engine import _signals_ema_cross
    n = 200
    rng = np.random.default_rng(1)
    close = 100 + rng.normal(0, 1, n).cumsum() * 0.1
    high = close + 0.5
    low = close - 0.5
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"ema_fast": 9, "ema_slow": 21, "ema_trend": 0,
              "use_atr_sl": True, "atr_period": 14, "atr_mult": 1.0}
    sl, ss, bbm, sd = _signals_ema_cross(close, high, low, close_s, high_s, low_s, params)
    assert sd is not None and sd.shape == (n,)


def test_signals_rsi_scalp_shape():
    import pandas as pd
    from bot.backtest.engine import _signals_rsi_scalp
    n = 100
    close = np.linspace(100, 90, n)
    close_s = pd.Series(close)
    params = {"rsi_period": 14, "rsi_os": 30, "ema_period": 0}
    sl, ss, bbm, sd = _signals_rsi_scalp(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)


def test_signals_bb_rsi_shape():
    import pandas as pd
    from bot.backtest.engine import _signals_bb_rsi
    n = 100
    close = np.full(n, 100.0); close[60:65] = [95, 92, 90, 88, 85]
    close_s = pd.Series(close)
    params = {"bb_period": 15, "bb_std": 1.5,
              "bbp_long_threshold": 0.10, "bbp_short_threshold": 0.90,
              "rsi_period": 14, "rsi_os": 30,
              "ema_period": 0, "bb_mid_exit": False}
    sl, ss, bbm, sd = _signals_bb_rsi(close, close + 0.5, close - 0.5,
                                       close_s, pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,) and bbm is None


def test_signals_macd_cross_shape():
    import pandas as pd
    from bot.backtest.engine import _signals_macd_cross
    n = 200
    rng = np.random.default_rng(2)
    close = 100 + rng.normal(0, 1, n).cumsum() * 0.1
    close_s = pd.Series(close)
    params = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "ema_trend": 0}
    sl, ss, bbm, sd = _signals_macd_cross(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)


def test_signals_williams_r_shape():
    import pandas as pd
    from bot.backtest.engine import _signals_williams_r
    n = 100
    close = np.linspace(100, 105, n)
    close_s = pd.Series(close)
    params = {"wr_period": 14, "wr_os": -80, "ema_period": 0}
    sl, ss, bbm, sd = _signals_williams_r(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)
