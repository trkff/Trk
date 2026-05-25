import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from bot.indicators import compute_all, is_fee_viable


def _make_df_1m(n=60, base_price=100.0, base_ts=None):
    """Helper: n candles de 1m com datetime index UTC."""
    if base_ts is None:
        base_ts = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    rows = []
    for i in range(n):
        ts = base_ts + timedelta(minutes=i)
        rows.append({
            "timestamp": int(ts.timestamp() * 1000),
            "open":  base_price + i * 0.01,
            "high":  base_price + i * 0.01 + 0.05,
            "low":   base_price + i * 0.01 - 0.05,
            "close": base_price + i * 0.01,
            "volume": 100.0 + i,
        })
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("datetime", inplace=True)
    return df


def _make_df_5m(n=30, base_price=100.0):
    """Helper: n candles de 5m com datetime index UTC."""
    base_ts = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    rows = []
    for i in range(n):
        ts = base_ts + timedelta(minutes=i * 5)
        rows.append({
            "timestamp": int(ts.timestamp() * 1000),
            "open":  base_price + i * 0.05,
            "high":  base_price + i * 0.05 + 0.1,
            "low":   base_price + i * 0.05 - 0.1,
            "close": base_price + i * 0.05,
            "volume": 200.0 + i,
        })
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("datetime", inplace=True)
    return df


CFG = {
    "ema_fast": "9", "ema_slow": "21",
    "rsi_period": "2", "atr_period": "14",
    "volume_avg_period": "20", "stochrsi_period": "14",
}


def test_compute_all_returns_dict_with_mandatory_keys():
    result = compute_all(_make_df_1m(60), _make_df_5m(30), CFG)
    assert result is not None
    for key in ["ema9", "ema21", "rsi2", "atr", "volume", "volume_avg", "close_1m", "close_5m"]:
        assert key in result


def test_compute_all_returns_optional_vwap_stochrsi_keys():
    result = compute_all(_make_df_1m(60), _make_df_5m(30), CFG)
    assert result is not None
    # Optional keys are present (may be None or float)
    for key in ["vwap", "stochrsi_k", "stochrsi_d", "stochrsi_k_prev", "stochrsi_d_prev"]:
        assert key in result


def test_compute_all_vwap_none_does_not_block():
    """Quando VWAP é NaN (ex: candles de ontem), compute_all() retorna o dict mesmo assim."""
    yesterday = datetime.now(timezone.utc).replace(hour=10) - timedelta(days=1)
    df_1m = _make_df_1m(60, base_ts=yesterday)
    result = compute_all(df_1m, _make_df_5m(30), CFG)
    assert result is not None          # não bloqueia
    assert result["vwap"] is None      # mas o campo é None


def test_compute_all_insufficient_candles_returns_none():
    """Poucos candles → indicadores obrigatórios NaN → retorna None."""
    result = compute_all(_make_df_1m(5), _make_df_5m(5), CFG)
    assert result is None


def test_is_fee_viable_blocked_spec_example():
    # BTC: ATR=$20, price=$66747, tp=2 → (20/66747)*2 = 0.000599 < 0.0009
    assert is_fee_viable(20.0, 66747.0, 2.0, 0.0009) is False


def test_is_fee_viable_allowed_spec_example():
    # BTC: ATR=$35, price=$66747, tp=2 → (35/66747)*2 = 0.001049 > 0.0009
    assert is_fee_viable(35.0, 66747.0, 2.0, 0.0009) is True


def test_is_fee_viable_default_fee_rate():
    # default fee_rate=0.0009 should match explicit
    assert is_fee_viable(20.0, 66747.0, 2.0) is False
    assert is_fee_viable(35.0, 66747.0, 2.0) is True


def test_is_fee_viable_exact_boundary():
    # (atr/price)*tp == fee_rate → not strictly greater → False
    # atr=30, price=100000, tp=3 → (30/100000)*3 = 0.0009 → not > 0.0009
    assert is_fee_viable(30.0, 100000.0, 3.0, 0.0009) is False


def test_is_fee_viable_just_above_boundary():
    # atr=30.01 → (30.01/100000)*3 = 0.0009003 > 0.0009 → True
    assert is_fee_viable(30.01, 100000.0, 3.0, 0.0009) is True


def test_is_fee_viable_zero_price_returns_false():
    assert is_fee_viable(20.0, 0.0, 2.0, 0.0009) is False


# ── 5m indicator keys ─────────────────────────────────────────────────────────

def test_compute_all_returns_5m_indicator_keys():
    """compute_all deve retornar rsi2_5m, atr_5m, volume_5m, volume_avg_5m."""
    result = compute_all(_make_df_1m(60), _make_df_5m(30), CFG)
    assert result is not None
    for key in ["rsi2_5m", "atr_5m", "volume_5m", "volume_avg_5m"]:
        assert key in result, f"Key '{key}' missing from compute_all result"


def test_compute_all_5m_indicators_are_numeric():
    """rsi2_5m, atr_5m, volume_5m, volume_avg_5m devem ser floats com dados suficientes."""
    result = compute_all(_make_df_1m(60), _make_df_5m(30), CFG)
    assert result is not None
    for key in ["rsi2_5m", "atr_5m", "volume_5m", "volume_avg_5m"]:
        assert result[key] is not None, f"Key '{key}' is None with 30 5m candles"
        assert isinstance(result[key], float), f"Key '{key}' is not float: {result[key]}"
        assert result[key] >= 0.0, f"Key '{key}' is negative: {result[key]}"


def test_compute_all_5m_volume_matches_last_candle():
    """volume_5m deve ser o volume do candle 5m mais recente."""
    df_5m = _make_df_5m(30)
    result = compute_all(_make_df_1m(60), df_5m, CFG)
    assert result is not None
    assert result["volume_5m"] == round(float(df_5m["volume"].iloc[-1]), 4)
