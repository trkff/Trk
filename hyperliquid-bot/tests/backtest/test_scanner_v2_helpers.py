"""Tests for scanner_v2 helpers — ADX/ATR caches, session mask, combo sampling."""
import numpy as np
import pandas as pd

from bot.backtest import scanner_v2


def _series_pair(n=300):
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 0.5
    low = close - 0.5
    return pd.Series(high), pd.Series(low), pd.Series(close)


def test_adx_cache_returns_arrays_per_period():
    high_s, low_s, close_s = _series_pair()
    cache = scanner_v2._adx_cache(high_s, low_s, close_s, [14, 21])
    assert set(cache.keys()) == {14, 21}
    assert cache[14].shape == (300,)
    finite = cache[14][~np.isnan(cache[14])]
    assert finite.size > 0
    assert (finite >= 0).all() and (finite <= 100).all()


def test_adx_cache_skips_zero_period():
    high_s, low_s, close_s = _series_pair()
    cache = scanner_v2._adx_cache(high_s, low_s, close_s, [0, 14])
    assert 0 not in cache
    assert 14 in cache


def test_atr_cache_returns_arrays_per_period():
    high_s, low_s, close_s = _series_pair()
    cache = scanner_v2._atr_cache(high_s, low_s, close_s, [14, 21])
    assert set(cache.keys()) == {14, 21}
    finite = cache[14][~np.isnan(cache[14])]
    assert finite.size > 0
    assert (finite > 0).all()


def test_session_mask_basic():
    start = pd.Timestamp("2026-01-01T00:00:00Z").value // 1_000_000
    step = 5 * 60 * 1000
    ts = np.array([start + i * step for i in range(288)], dtype=np.int64)
    mask = scanner_v2._session_mask(ts, 13, 21)
    hours = ((ts // (60 * 60 * 1000)) % 24).astype(int)
    assert mask.dtype == bool
    expected = (hours >= 13) & (hours < 21)
    np.testing.assert_array_equal(mask, expected)


def test_session_mask_no_filter_all_true():
    ts = np.array([0, 60_000, 120_000], dtype=np.int64)
    mask = scanner_v2._session_mask(ts, 0, 24)
    assert mask.all()


def test_iter_combos_returns_all_when_below_cap():
    out = list(scanner_v2._iter_combos([(1, 2), (3, 4)], max_combos=10))
    assert sorted(out) == sorted([(1, 3), (1, 4), (2, 3), (2, 4)])


def test_iter_combos_samples_when_above_cap():
    iterables = [list(range(10)), list(range(10)), list(range(10))]
    out = list(scanner_v2._iter_combos(iterables, max_combos=50))
    assert len(out) == 50
    assert len(set(out)) == 50
    out2 = list(scanner_v2._iter_combos(iterables, max_combos=50))
    assert out == out2


def test_filter_grids_default_counts():
    grids = scanner_v2._filter_grids()
    adx_count = 1 + len(scanner_v2.ADX_MIN_TREND) * len([p for p in scanner_v2.ADX_PERIODS if p > 0])
    assert len(grids["adx"]) == adx_count
    assert (0, 0) in grids["adx"]
    assert grids["session"] == list(scanner_v2.SESSION_FILTERS)
    atr_count = 1 + len(scanner_v2.ATR_TP_MULTS) * len(scanner_v2.ATR_SL_MULTS)
    assert len(grids["atr"]) == atr_count
    assert (False, 0.0, 0.0) in grids["atr"]


def test_build_adx_mask_trend_requires_geq_threshold():
    adx_cache = {14: np.array([15.0, 25.0, 18.0, 30.0])}
    mask = scanner_v2._build_adx_mask(adx_cache, 4, 14, 20.0, is_trend_strategy=True)
    np.testing.assert_array_equal(mask, [False, True, False, True])


def test_build_adx_mask_mean_reversion_requires_lt_threshold():
    adx_cache = {14: np.array([15.0, 25.0, 18.0, 30.0])}
    mask = scanner_v2._build_adx_mask(adx_cache, 4, 14, 20.0, is_trend_strategy=False)
    np.testing.assert_array_equal(mask, [True, False, True, False])


def test_build_adx_mask_period_zero_is_no_filter():
    adx_cache = {14: np.array([15.0, 25.0])}
    assert scanner_v2._build_adx_mask(adx_cache, 2, 0, 20.0, False) is None
