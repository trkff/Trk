"""Tests for live_filters — session, ADX and ATR override."""
import numpy as np
import pandas as pd
import pytest

from bot.strategies.live_filters import (
    apply_atr_tp_sl,
    apply_live_filters,
    passes_adx,
    passes_session,
)


def _df(n=300, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 0.5
    low = close - 0.5
    start = pd.Timestamp("2026-01-01T00:00:00Z").value // 1_000_000
    step = 5 * 60 * 1000
    ts = np.array([start + i * step for i in range(n)], dtype=np.int64)
    return pd.DataFrame({"timestamp": ts, "open": close, "high": high, "low": low,
                         "close": close, "volume": np.ones(n)})


# ── Session ──────────────────────────────────────────────────────

def test_session_no_filter_when_default():
    assert passes_session({}, 0) is True
    assert passes_session({"session_start": 0, "session_end": 24}, 999_999_999_999) is True


def test_session_blocks_outside_range():
    # 2026-01-01T15:00:00Z -> hour=15. Range 7-21 lets it pass.
    ts_15h = pd.Timestamp("2026-01-01T15:00:00Z").value // 1_000_000
    assert passes_session({"session_start": 7, "session_end": 21}, ts_15h) is True
    # 2026-01-01T05:00:00Z -> hour=5. Range 7-21 blocks.
    ts_5h = pd.Timestamp("2026-01-01T05:00:00Z").value // 1_000_000
    assert passes_session({"session_start": 7, "session_end": 21}, ts_5h) is False


def test_session_endpoint_exclusive():
    # 21:00 should be blocked when end=21 (exclusive).
    ts_21h = pd.Timestamp("2026-01-01T21:00:00Z").value // 1_000_000
    assert passes_session({"session_start": 7, "session_end": 21}, ts_21h) is False


# ── ADX ──────────────────────────────────────────────────────────

def test_adx_no_filter_when_period_zero():
    df = _df()
    assert passes_adx({"adx_period": 0}, df, is_trend_strategy=False) is True
    assert passes_adx({"adx_period": 0}, df, is_trend_strategy=True) is True


def test_adx_mean_reversion_requires_low_adx():
    df = _df()
    # Force ADX low → mean-reversion (False) should pass
    # We can't easily force ADX value, so just check that the function returns a bool.
    result = passes_adx({"adx_period": 14, "adx_min": 100}, df, is_trend_strategy=False)
    assert result is True  # 100 is unreachable, so ADX < 100 always

    # adx_min=0 means we need ADX < 0 which never happens → always False except NaN warmup
    result = passes_adx({"adx_period": 14, "adx_min": 0}, df, is_trend_strategy=False)
    assert result is False


def test_adx_trend_requires_high_adx():
    df = _df()
    # adx_min=0 → ADX >= 0 always true (every ADX >= 0)
    assert passes_adx({"adx_period": 14, "adx_min": 0}, df, is_trend_strategy=True) is True
    # adx_min=100 → ADX >= 100 never true
    assert passes_adx({"adx_period": 14, "adx_min": 100}, df, is_trend_strategy=True) is False


# ── ATR override ─────────────────────────────────────────────────

def test_atr_no_override_when_mode_false():
    df = _df()
    signal = {"tp_pct": 0.008, "sl_pct": 0.008, "signal_price": 100.0}
    out = apply_atr_tp_sl({"atr_tp_mode": False}, df, signal)
    assert out["tp_pct"] == 0.008
    assert out["sl_pct"] == 0.008


def test_atr_overrides_when_mode_true():
    df = _df()
    signal = {"tp_pct": 0.999, "sl_pct": 0.999, "signal_price": float(df["close"].iloc[-1])}
    out = apply_atr_tp_sl(
        {"atr_tp_mode": True, "atr_tp_mult": 1.5, "atr_sl_mult": 0.5, "atr_period": 14},
        df, signal,
    )
    # tp_pct must have changed away from 0.999 (the dummy original)
    assert out["tp_pct"] != 0.999
    assert out["sl_pct"] != 0.999
    # tp = atr/close * 1.5; sl = atr/close * 0.5; so tp > sl
    assert out["tp_pct"] > out["sl_pct"]


def test_atr_mode_accepts_string_true():
    df = _df()
    signal = {"tp_pct": 0.999, "sl_pct": 0.999, "signal_price": 100.0}
    out = apply_atr_tp_sl(
        {"atr_tp_mode": "true", "atr_tp_mult": 1.0, "atr_sl_mult": 1.0},
        df, signal,
    )
    assert out["tp_pct"] != 0.999  # actually computed from ATR


# ── Wrapper apply_live_filters ───────────────────────────────────

def test_apply_live_filters_returns_none_when_signal_none():
    df = _df()
    assert apply_live_filters({}, df, None) is None


def test_apply_live_filters_passes_through_when_no_filters():
    df = _df()
    signal = {"asset": "BTC", "side": "long", "tp_pct": 0.01, "sl_pct": 0.01,
              "signal_price": 100.0}
    out = apply_live_filters({}, df, signal)
    assert out is not None
    assert out["side"] == "long"


def test_apply_live_filters_session_blocks():
    df = _df()
    # Last candle in df is at 2026-01-01T00:00:00Z + 299*5min = ~24h53min later
    # So hour around 0-1. Force session 13-21 → blocks.
    signal = {"asset": "BTC", "side": "long", "tp_pct": 0.01, "sl_pct": 0.01,
              "signal_price": 100.0}
    out = apply_live_filters(
        {"session_start": 13, "session_end": 21}, df, signal,
    )
    # The last ts in our _df is around 25 hours after 2026-01-01T00 → wrap → hour 0 or 1
    # Force it to be outside [13, 21)
    last_hour = (int(df["timestamp"].iloc[-1]) // 3_600_000) % 24
    if not (13 <= last_hour < 21):
        assert out is None
    else:
        # If by accident we landed in the session, no block expected
        assert out is not None
