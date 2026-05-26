"""
Scanner v2 — adds ADX regime filter, UTC session filter, ATR-based TP/SL,
and walk-forward optimization (WFO) on top of the original scanner.

Reuses the indicator helpers and stats from scanner.py without modifying it.
"""
import random
import threading
import uuid
from itertools import product

import numpy as np
import pandas as pd
import pandas_ta as ta

from bot.backtest.scanner import (
    APPROVAL,
    SUPPORTED_TIMEFRAMES,
    _bb_cache,
    _bb_mid_cache,
    _ema_cache,
    _load_csv,
    _monthly_breakdown,
    _rsi_cache,
    _scale_tp_sl,
    _stats,
    _stoch_cache,
    _tf_minutes,
    get_available_assets,
)
from bot.backtest.csv_loader import _update_csv
from bot.logger import get_logger

log = get_logger("backtest.scanner_v2")

_SCAN_WARMUP_DAYS = 2
_DEFAULT_MAX_COMBOS = 5000
_SAMPLE_SEED = 42


# ── Core simulator ────────────────────────────────────────────────────────

def _backtest_v2(
    sig_long: np.ndarray,
    sig_short: np.ndarray,
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    tp_pct: float,
    sl_pct: float,
    bb_mid: np.ndarray | None = None,
    tp_dist: np.ndarray | None = None,
    sl_dist: np.ndarray | None = None,
    session_mask: np.ndarray | None = None,
    adx_mask: np.ndarray | None = None,
) -> list[tuple[float, int]]:
    """Bar-by-bar simulation with optional per-candle TP/SL distance and entry masks.

    Per-candle priority: SL > TP > BB-mid (matches scanner._backtest).
    On a same-bar SL+TP hit, SL wins (pessimistic).

    Returns list of (return_pct, entry_idx). Same shape as scanner._backtest.
    """
    if session_mask is not None:
        sig_long = sig_long & session_mask
        sig_short = sig_short & session_mask
    if adx_mask is not None:
        sig_long = sig_long & adx_mask
        sig_short = sig_short & adx_mask

    trades: list[tuple[float, int]] = []
    N = len(close)
    i = 0
    while i < N - 1:
        is_long = bool(sig_long[i])
        is_short = bool(sig_short[i])
        if not is_long and not is_short:
            i += 1
            continue

        entry = float(close[i])
        if tp_dist is not None and not np.isnan(tp_dist[i]):
            tp_abs = float(tp_dist[i])
        else:
            tp_abs = entry * tp_pct / 100.0
        if sl_dist is not None and not np.isnan(sl_dist[i]):
            sl_abs = float(sl_dist[i])
        else:
            sl_abs = entry * sl_pct / 100.0

        if is_long:
            tp = entry + tp_abs
            sl = entry - sl_abs
        else:
            tp = entry - tp_abs
            sl = entry + sl_abs

        j = i + 1
        outcome = None
        while j < N:
            h, lo = high[j], low[j]
            if is_long:
                if lo <= sl:
                    outcome = ("loss", j)
                elif h >= tp:
                    outcome = ("win", j)
                elif bb_mid is not None and not np.isnan(bb_mid[j]) and close[j] >= bb_mid[j]:
                    outcome = ("bb_mid", j)
            else:
                if h >= sl:
                    outcome = ("loss", j)
                elif lo <= tp:
                    outcome = ("win", j)
                elif bb_mid is not None and not np.isnan(bb_mid[j]) and close[j] <= bb_mid[j]:
                    outcome = ("bb_mid", j)
            if outcome:
                break
            j += 1

        if outcome:
            kind, j_exit = outcome
            if kind == "win":
                ret_pct = tp_pct if tp_dist is None or np.isnan(tp_dist[i]) else tp_abs / entry * 100.0
                trades.append((ret_pct, i))
            elif kind == "loss":
                ret_pct = -sl_pct if sl_dist is None or np.isnan(sl_dist[i]) else -sl_abs / entry * 100.0
                trades.append((ret_pct, i))
            else:
                ret = (close[j_exit] - entry) / entry * 100.0
                if not is_long:
                    ret = -ret
                trades.append((float(ret), i))
            i = j_exit + 1
        else:
            i += 1

    return trades


# ── Indicator helpers ─────────────────────────────────────────────────────

def _adx_cache(high_s: pd.Series, low_s: pd.Series, close_s: pd.Series,
               periods: list[int]) -> dict[int, np.ndarray]:
    """ADX per period. period<=0 is skipped (means 'no ADX filter')."""
    cache: dict[int, np.ndarray] = {}
    for p in periods:
        if p <= 0:
            continue
        df = ta.adx(high_s, low_s, close_s, length=p)
        adx_col = [c for c in df.columns if c.startswith("ADX_")][0]
        cache[p] = df[adx_col].values.astype(float)
    return cache


def _atr_cache(high_s: pd.Series, low_s: pd.Series, close_s: pd.Series,
               periods: list[int]) -> dict[int, np.ndarray]:
    cache: dict[int, np.ndarray] = {}
    for p in periods:
        if p <= 0:
            continue
        atr = ta.atr(high_s, low_s, close_s, length=p)
        cache[p] = atr.values.astype(float)
    return cache


def _session_mask(ts_ms: np.ndarray, hour_start_utc: int, hour_end_utc: int) -> np.ndarray:
    """Boolean mask: True on candles whose UTC hour falls in [start, end).
    (0, 24) -> all-True (no filter)."""
    if hour_start_utc == 0 and hour_end_utc == 24:
        return np.ones(len(ts_ms), dtype=bool)
    hours = ((ts_ms // (60 * 60 * 1000)) % 24).astype(int)
    return (hours >= hour_start_utc) & (hours < hour_end_utc)


# ── Combo iteration ───────────────────────────────────────────────────────

def _iter_combos(iterables, max_combos: int = _DEFAULT_MAX_COMBOS):
    """Iterate the Cartesian product. If total > max_combos, sample
    max_combos distinct tuples deterministically (Random(_SAMPLE_SEED))."""
    all_combos = list(product(*iterables))
    if len(all_combos) <= max_combos:
        yield from all_combos
        return
    rng = random.Random(_SAMPLE_SEED)
    sampled = rng.sample(all_combos, max_combos)
    yield from sampled


# ── New filter dimensions ─────────────────────────────────────────────────

ADX_PERIODS = [0, 14]            # 0 = no ADX filter
ADX_MIN_TREND = [20, 25]         # threshold; semantics depend on strategy family
SESSION_FILTERS = [(0, 24), (7, 21), (13, 21)]   # (0,24) = no filter
ATR_TP_MULTS = [1.0, 1.5, 2.0]
ATR_SL_MULTS = [0.5, 1.0, 1.5]
ATR_PERIOD = 14                  # ATR length when atr_tp_mode=True


def _filter_grids() -> dict:
    """Pre-build the 3 filter sub-grids that every strategy scanner uses."""
    adx = [(0, 0)]  # 'no filter' row
    for p in ADX_PERIODS:
        if p <= 0:
            continue
        for m in ADX_MIN_TREND:
            adx.append((p, m))

    atr = [(False, 0.0, 0.0)]  # '% TP/SL' row — atr mults ignored
    for tp_m, sl_m in product(ATR_TP_MULTS, ATR_SL_MULTS):
        atr.append((True, tp_m, sl_m))

    return {"adx": adx, "session": list(SESSION_FILTERS), "atr": atr}


# ── Shared filter application logic ───────────────────────────────────────

_MEAN_REVERSION_FAMILIES = {
    "BB_Stoch", "BB_Reversion", "BB_RSI", "RSI_Scalp", "Stoch_Scalp", "Williams_R",
}
_TREND_FAMILIES = {"EMA_Cross", "MACD_Cross"}


def _build_adx_mask(adx_cache: dict[int, np.ndarray], n: int,
                    adx_period: int, adx_min: float,
                    is_trend_strategy: bool) -> np.ndarray | None:
    """Return per-candle mask (True = entry allowed) or None if no filter."""
    if adx_period <= 0:
        return None
    adx = adx_cache.get(adx_period)
    if adx is None:
        return None
    finite = ~np.isnan(adx)
    if is_trend_strategy:
        return finite & (adx >= adx_min)
    return finite & (adx < adx_min)


def _resolve_tp_sl_arrays(close: np.ndarray, atr_arr: np.ndarray | None,
                          atr_mode: bool, atr_tp_mult: float, atr_sl_mult: float):
    """Return (tp_dist, sl_dist) for _backtest_v2, or (None, None)
    when atr_mode=False."""
    if not atr_mode or atr_arr is None:
        return None, None
    tp_dist = atr_arr * atr_tp_mult
    sl_dist = atr_arr * atr_sl_mult
    return tp_dist, sl_dist


def _apply_window(sig_long, sig_short, window_start_idx):
    if window_start_idx > 0:
        sig_long = sig_long.copy()
        sig_short = sig_short.copy()
        sig_long[:window_start_idx] = False
        sig_short[:window_start_idx] = False
    return sig_long, sig_short


def _apply_ema_filter(sig_long, sig_short, close, ema_cache, trend_e):
    if trend_e <= 0:
        return sig_long, sig_short
    EMA = ema_cache.get(trend_e)
    if EMA is None:
        return sig_long, sig_short
    valid = ~np.isnan(EMA)
    return (sig_long & valid & (close > EMA),
            sig_short & valid & (close < EMA))


# ── Strategy scanners (8 families) ────────────────────────────────────────

def _scan_bb_stoch(close, high, low, n, close_s, high_s, low_s, cb, *,
                   window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                   max_combos: int = _DEFAULT_MAX_COMBOS):
    BB_PERIODS = [10, 15]
    BB_STDS = [1.5, 2.0]
    BBP_THS = [0.05, 0.10, 0.15]
    STOCH_OS = [20, 25, 30]
    STOCH_K = 14
    TREND_EMAS = [0, 50, 200]
    TPS = [0.5, 0.8, 1.0, 1.5]
    SLS = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)
    BB_MID_EXITS = [False, True]

    bb = _bb_cache(close_s, close, BB_PERIODS, BB_STDS)
    bbm = _bb_mid_cache(close_s, BB_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    K, D = _stoch_cache(high_s, low_s, close_s, [STOCH_K])[STOCH_K]
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [BB_PERIODS, BB_STDS, BBP_THS, STOCH_OS, TREND_EMAS, TPS, SLS, BB_MID_EXITS,
            grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, (bbp, bbs, bbp_th, sos, te, tp, sl, bme,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 100 == 0:
            cb(f"BB Stoch v2 {idx}")

        BBP = bb[(bbp, bbs)]
        ob = 100 - sos
        valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(BBP)
        sl_long = valid & (BBP < bbp_th) & (K < sos) & (D < sos)
        sl_short = valid & (BBP > 1 - bbp_th) & (K > ob) & (D > ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=False)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        bb_mid_arr = bbm[bbp] if bme else None

        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         bb_mid=bb_mid_arr, tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "BB_Stoch",
                "bb_period": bbp, "bb_std": bbs, "bbp_th": bbp_th,
                "bb_mid_exit": bme,
                "stoch_k": STOCH_K, "stoch_os": sos,
                "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


def _scan_bb_reversion(close, high, low, n, close_s, high_s, low_s, cb, *,
                       window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                       max_combos: int = _DEFAULT_MAX_COMBOS):
    BB_PERIODS = [10, 15, 20]
    BB_STDS = [1.5, 2.0, 2.5]
    BBP_THS = [0.05, 0.10, 0.15, 0.20]
    TREND_EMAS = [0, 50, 200]
    TPS = [0.5, 1.0, 1.5, 2.0]
    SLS = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)
    BB_MID_EXITS = [False, True]

    bb = _bb_cache(close_s, close, BB_PERIODS, BB_STDS)
    bbm = _bb_mid_cache(close_s, BB_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    bands = {}
    for bbp_p, bbs_s in product(BB_PERIODS, BB_STDS):
        bb_df = ta.bbands(close_s, length=bbp_p, std=bbs_s)
        bands[(bbp_p, bbs_s)] = (
            bb_df[[c for c in bb_df.columns if c.startswith("BBU_")][0]].values.astype(float),
            bb_df[[c for c in bb_df.columns if c.startswith("BBL_")][0]].values.astype(float),
        )
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [BB_PERIODS, BB_STDS, BBP_THS, TREND_EMAS, TPS, SLS, BB_MID_EXITS,
            grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, (bbp, bbs, bbp_th, te, tp, sl, bme,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 100 == 0:
            cb(f"BB Reversion v2 {idx}")
        BBP = bb[(bbp, bbs)]
        BBU, BBL = bands[(bbp, bbs)]
        BBM = bbm[bbp]
        BBP_prev = np.roll(BBP, 1); BBP_prev[0] = np.nan
        valid = ~np.isnan(BBP_prev) & ~np.isnan(BBU) & ~np.isnan(BBL) & ~np.isnan(BBM)
        sl_long = valid & (BBP_prev < bbp_th) & (close > BBL) & (close < BBM)
        sl_short = valid & (BBP_prev > 1 - bbp_th) & (close < BBU) & (close > BBM)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)
        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=False)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        bb_mid_arr = BBM if bme else None
        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         bb_mid=bb_mid_arr, tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "BB_Reversion",
                "bb_period": bbp, "bb_std": bbs, "bbp_th": bbp_th,
                "bb_mid_exit": bme, "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


def _scan_bb_rsi(close, high, low, n, close_s, high_s, low_s, cb, *,
                 window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                 max_combos: int = _DEFAULT_MAX_COMBOS):
    BB_PERIODS = [10, 15]
    BB_STDS = [1.5, 2.0]
    BBP_THS = [0.05, 0.10, 0.15]
    RSI_PERIODS = [7, 14]
    RSI_OS = [25, 30, 35]
    TREND_EMAS = [0, 50, 200]
    TPS = [0.8, 1.5]
    SLS = [0.5, 0.8]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)
    BB_MID_EXITS = [False, True]

    bb = _bb_cache(close_s, close, BB_PERIODS, BB_STDS)
    bbm = _bb_mid_cache(close_s, BB_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    rsi = _rsi_cache(close_s, RSI_PERIODS)
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [BB_PERIODS, BB_STDS, BBP_THS, RSI_PERIODS, RSI_OS, TREND_EMAS,
            TPS, SLS, BB_MID_EXITS, grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, (bbp, bbs, bbp_th, rp, ros, te, tp, sl, bme,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 100 == 0:
            cb(f"BB RSI v2 {idx}")
        BBP = bb[(bbp, bbs)]
        RSI = rsi[rp]
        rob = 100 - ros
        valid = ~np.isnan(BBP) & ~np.isnan(RSI)
        sl_long = valid & (BBP < bbp_th) & (RSI < ros)
        sl_short = valid & (BBP > 1 - bbp_th) & (RSI > rob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)
        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=False)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        bb_mid_arr = bbm[bbp] if bme else None
        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         bb_mid=bb_mid_arr, tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "BB_RSI",
                "bb_period": bbp, "bb_std": bbs, "bbp_th": bbp_th,
                "bb_mid_exit": bme, "rsi_period": rp, "rsi_os": ros,
                "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


def _scan_rsi_scalp(close, high, low, n, close_s, high_s, low_s, cb, *,
                    window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                    max_combos: int = _DEFAULT_MAX_COMBOS):
    RSI_PERIODS = [7, 14]
    OS_LEVELS = [25, 30, 35, 40]
    TREND_EMAS = [0, 50, 200]
    TPS = [0.5, 0.8, 1.0]
    SLS = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    rsi = _rsi_cache(close_s, RSI_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [RSI_PERIODS, OS_LEVELS, TREND_EMAS, TPS, SLS,
            grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, (rp, os_lvl, te, tp, sl,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 50 == 0:
            cb(f"RSI Scalp v2 {idx}")
        RSI = rsi[rp]
        ob = 100 - os_lvl
        pRSI = np.roll(RSI, 1); pRSI[0] = np.nan
        valid = ~np.isnan(RSI) & ~np.isnan(pRSI)
        sl_long = valid & (pRSI < os_lvl) & (RSI >= os_lvl)
        sl_short = valid & (pRSI > ob) & (RSI <= ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)
        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=False)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "RSI_Scalp",
                "rsi_period": rp, "os": os_lvl,
                "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


def _scan_stoch_scalp(close, high, low, n, close_s, high_s, low_s, cb, *,
                      window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                      max_combos: int = _DEFAULT_MAX_COMBOS):
    K_PERIODS = [5, 9, 14]
    OS_LEVELS = [30, 40, 50]
    TREND_EMAS = [0, 50, 200]
    TPS = [0.5, 0.8, 1.0]
    SLS = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    stoch = _stoch_cache(high_s, low_s, close_s, K_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [K_PERIODS, OS_LEVELS, TREND_EMAS, TPS, SLS,
            grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, (kp, os_lvl, te, tp, sl,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 50 == 0:
            cb(f"Stoch Scalp v2 {idx}")
        K, D = stoch[kp]
        ob = 100 - os_lvl
        pK = np.roll(K, 1); pK[0] = np.nan
        pD = np.roll(D, 1); pD[0] = np.nan
        valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(pK) & ~np.isnan(pD)
        sl_long = valid & (pK < os_lvl) & (pD < os_lvl) & (K > D) & (pK <= pD)
        sl_short = valid & (pK > ob) & (pD > ob) & (K < D) & (pK >= pD)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)
        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=False)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "Stoch_Scalp",
                "k_period": kp, "os": os_lvl,
                "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


def _scan_williams_r(close, high, low, n, close_s, high_s, low_s, cb, *,
                     window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                     max_combos: int = _DEFAULT_MAX_COMBOS):
    WR_PERIODS = [7, 14]
    OS_LEVELS = [-80, -70, -60]
    TREND_EMAS = [0, 50, 200]
    TPS = [0.5, 0.8, 1.0]
    SLS = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    wr_cache = {wp: ta.willr(high_s, low_s, close_s, length=wp).values.astype(float)
                for wp in WR_PERIODS}
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [WR_PERIODS, OS_LEVELS, TREND_EMAS, TPS, SLS,
            grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, (wp, os_lvl, te, tp, sl,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 50 == 0:
            cb(f"Williams %R v2 {idx}")
        WR = wr_cache[wp]
        ob = os_lvl + 100
        pWR = np.roll(WR, 1); pWR[0] = np.nan
        valid = ~np.isnan(WR) & ~np.isnan(pWR)
        sl_long = valid & (pWR < os_lvl) & (WR >= os_lvl)
        sl_short = valid & (pWR > ob) & (WR <= ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)
        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=False)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "Williams_R",
                "wr_period": wp, "os": os_lvl,
                "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


def _scan_ema_cross(close, high, low, n, close_s, high_s, low_s, cb, *,
                    window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                    max_combos: int = _DEFAULT_MAX_COMBOS):
    PAIRS = [(3, 8), (5, 13), (9, 21), (3, 13), (8, 21)]
    TREND_EMAS = [0, 50, 200]
    TPS = [0.5, 1.0, 1.5]
    SLS = [0.3, 0.5, 0.8]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    all_periods = {p for pair in PAIRS for p in pair} | {e for e in TREND_EMAS if e > 0}
    ema = _ema_cache(close_s, list(all_periods))
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [PAIRS, TREND_EMAS, TPS, SLS, grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, ((fp, sp), te, tp, sl,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 30 == 0:
            cb(f"EMA Cross v2 {idx}")
        FAST = ema[fp]; SLOW = ema[sp]
        pFAST = np.roll(FAST, 1); pFAST[0] = np.nan
        pSLOW = np.roll(SLOW, 1); pSLOW[0] = np.nan
        valid = ~np.isnan(FAST) & ~np.isnan(SLOW) & ~np.isnan(pFAST) & ~np.isnan(pSLOW)
        sl_long = valid & (FAST > SLOW) & (pFAST <= pSLOW)
        sl_short = valid & (FAST < SLOW) & (pFAST >= pSLOW)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)
        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=True)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "EMA_Cross",
                "ema_fast": fp, "ema_slow": sp,
                "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


def _scan_macd_cross(close, high, low, n, close_s, high_s, low_s, cb, *,
                     window_start_idx: int = 0, mins: int = 5, ts_ms=None,
                     max_combos: int = _DEFAULT_MAX_COMBOS):
    MACD_COMBOS = [(8, 21, 7), (8, 21, 9), (12, 26, 7), (12, 26, 9), (8, 26, 9)]
    TREND_EMAS = [0, 50, 200]
    TPS = [0.5, 1.0, 1.5]
    SLS = [0.3, 0.5, 0.8]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    macd_cache = {}
    for fast, slow, sig in MACD_COMBOS:
        df = ta.macd(close_s, fast=fast, slow=slow, signal=sig)
        mcol = [c for c in df.columns if c.startswith("MACD_")][0]
        scol = [c for c in df.columns if c.startswith("MACDs_")][0]
        macd_cache[(fast, slow, sig)] = (
            df[mcol].values.astype(float),
            df[scol].values.astype(float),
        )
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    adx_cache = _adx_cache(high_s, low_s, close_s, [p for p in ADX_PERIODS if p > 0])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)

    grids = _filter_grids()
    base = [MACD_COMBOS, TREND_EMAS, TPS, SLS, grids["adx"], grids["session"], grids["atr"]]
    results = []
    for idx, ((fast, slow, sig), te, tp, sl,
              (adx_p, adx_m), (sess_s, sess_e),
              (atr_mode, atr_tp_m, atr_sl_m)) in enumerate(_iter_combos(base, max_combos)):
        if cb and idx % 30 == 0:
            cb(f"MACD Cross v2 {idx}")
        MACD, SIG = macd_cache[(fast, slow, sig)]
        pMACD = np.roll(MACD, 1); pMACD[0] = np.nan
        pSIG = np.roll(SIG, 1); pSIG[0] = np.nan
        valid = ~np.isnan(MACD) & ~np.isnan(SIG) & ~np.isnan(pMACD) & ~np.isnan(pSIG)
        sl_long = valid & (MACD > SIG) & (pMACD <= pSIG)
        sl_short = valid & (MACD < SIG) & (pMACD >= pSIG)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)
        adx_mask = _build_adx_mask(adx_cache, n, adx_p, adx_m, is_trend_strategy=True)
        sess_mask = _session_mask(ts_ms, sess_s, sess_e) if ts_ms is not None else None
        tp_dist, sl_dist = _resolve_tp_sl_arrays(close, atr_arr, atr_mode, atr_tp_m, atr_sl_m)
        s = _stats(
            _backtest_v2(sl_long, sl_short, close, high, low, tp, sl,
                         tp_dist=tp_dist, sl_dist=sl_dist,
                         session_mask=sess_mask, adx_mask=adx_mask),
            n, mins, ts_ms=ts_ms,
        )
        if s:
            results.append({
                "strategy": "MACD_Cross",
                "macd_fast": fast, "macd_slow": slow, "macd_sig": sig,
                "trend_ema": te, "tp": tp, "sl": sl,
                "adx_period": adx_p, "adx_min": adx_m,
                "session_start": sess_s, "session_end": sess_e,
                "atr_tp_mode": atr_mode, "atr_tp_mult": atr_tp_m, "atr_sl_mult": atr_sl_m,
                **s,
            })
    return results


_SCANNERS_V2 = {
    "BB_Stoch":     _scan_bb_stoch,
    "Stoch_Scalp":  _scan_stoch_scalp,
    "EMA_Cross":    _scan_ema_cross,
    "BB_Reversion": _scan_bb_reversion,
    "RSI_Scalp":    _scan_rsi_scalp,
    "BB_RSI":       _scan_bb_rsi,
    "MACD_Cross":   _scan_macd_cross,
    "Williams_R":   _scan_williams_r,
}


# ── Public entry point ────────────────────────────────────────────────────

def run_scan_v2(asset: str, days: int = 90,
                strategies: list[str] | None = None,
                progress_cb=None,
                timeframe: str = "5m",
                max_combos_per_family: int = _DEFAULT_MAX_COMBOS) -> dict:
    if strategies is None:
        strategies = list(_SCANNERS_V2.keys())
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"error": f"Timeframe inválido: {timeframe}. Suportados: {SUPPORTED_TIMEFRAMES}"}
    mins = _tf_minutes(timeframe)

    if timeframe == "5m":
        if progress_cb:
            progress_cb(f"Atualizando CSV de {asset}...")
        try:
            _update_csv(asset, progress_cb)
        except Exception as e:
            log.warning(f"[scanner_v2] _update_csv failed for {asset}: {e}")

    df = _load_csv(asset, days + _SCAN_WARMUP_DAYS, timeframe=timeframe)
    if df is None or len(df) < 100:
        return {"error": f"CSV {asset.lower()}_{timeframe}.csv não encontrado ou insuficiente para {asset}"}

    last_ts_ms = int(df["ts_ms"].iloc[-1])
    real_start_ms = last_ts_ms - days * 86_400_000
    in_window = df["ts_ms"].values >= real_start_ms
    window_start_idx = int(np.argmax(in_window)) if in_window.any() else 0

    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    ts_ms = df["ts_ms"].values.astype(np.int64)
    n_real = len(close) - window_start_idx

    close_s = pd.Series(close)
    high_s = pd.Series(high)
    low_s = pd.Series(low)

    all_results: list[dict] = []
    for strat in strategies:
        fn = _SCANNERS_V2.get(strat)
        if fn is None:
            continue
        if progress_cb:
            progress_cb(f"Escaneando {strat} (v2)...")
        results = fn(close, high, low, n_real, close_s, high_s, low_s, progress_cb,
                     window_start_idx=window_start_idx, mins=mins, ts_ms=ts_ms,
                     max_combos=max_combos_per_family)
        for r in results:
            r["tf"] = timeframe
        all_results.extend(results)
        n_ap = sum(1 for r in results if r["approved"])
        log.info(f"[scanner_v2] {asset} {strat}: {len(results)} testados, {n_ap} aprovados")

    approved = sorted(
        (r for r in all_results if r["approved"]),
        key=lambda x: -x["roi"],
    )

    summary = {}
    for strat in strategies:
        sr = [r for r in all_results if r["strategy"] == strat]
        sa = [r for r in sr if r["approved"]]
        summary[strat] = {
            "tested": len(sr),
            "approved": len(sa),
            "best_roi": max(sa, key=lambda x: x["roi"]) if sa else None,
            "best_wr": max(sa, key=lambda x: x["wr"]) if sa else None,
        }

    if progress_cb:
        progress_cb("Concluído")

    return {
        "asset": asset,
        "timeframe": timeframe,
        "days": days,
        "candles": n_real,
        "total_tested": len(all_results),
        "total_approved": len(approved),
        "approval_criteria": APPROVAL,
        "per_strategy": summary,
        "approved": approved[:100],
        "max_combos_per_family": max_combos_per_family,
    }


# ── Walk-forward optimization ────────────────────────────────────────────

def _replay_params_on_slice(
    close, high, low, ts_ms, close_s, high_s, low_s, mins, params: dict,
) -> dict | None:
    """Re-run a single param row over the given array slice and return stats.
    Used in WFO OOS phase. Replays one combo deterministically — entry logic
    here mirrors each family's _scan_* signal construction exactly."""
    strat = params["strategy"]
    n = len(close)
    bb_mid_arr = None

    if strat == "BB_Stoch":
        bb = _bb_cache(close_s, close, [params["bb_period"]], [params["bb_std"]])
        bbm = _bb_mid_cache(close_s, [params["bb_period"]])
        ema = _ema_cache(close_s, [params["trend_ema"]] if params["trend_ema"] > 0 else [])
        K, D = _stoch_cache(high_s, low_s, close_s, [params["stoch_k"]])[params["stoch_k"]]
        BBP = bb[(params["bb_period"], params["bb_std"])]
        sos = params["stoch_os"]; ob = 100 - sos
        valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(BBP)
        sl_long = valid & (BBP < params["bbp_th"]) & (K < sos) & (D < sos)
        sl_short = valid & (BBP > 1 - params["bbp_th"]) & (K > ob) & (D > ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, params["trend_ema"])
        bb_mid_arr = bbm[params["bb_period"]] if params["bb_mid_exit"] else None

    elif strat == "BB_Reversion":
        bb = _bb_cache(close_s, close, [params["bb_period"]], [params["bb_std"]])
        bbm = _bb_mid_cache(close_s, [params["bb_period"]])
        ema = _ema_cache(close_s, [params["trend_ema"]] if params["trend_ema"] > 0 else [])
        bb_df = ta.bbands(close_s, length=params["bb_period"], std=params["bb_std"])
        BBU = bb_df[[c for c in bb_df.columns if c.startswith("BBU_")][0]].values.astype(float)
        BBL = bb_df[[c for c in bb_df.columns if c.startswith("BBL_")][0]].values.astype(float)
        BBP = bb[(params["bb_period"], params["bb_std"])]
        BBM = bbm[params["bb_period"]]
        BBP_prev = np.roll(BBP, 1); BBP_prev[0] = np.nan
        valid = ~np.isnan(BBP_prev) & ~np.isnan(BBU) & ~np.isnan(BBL) & ~np.isnan(BBM)
        sl_long = valid & (BBP_prev < params["bbp_th"]) & (close > BBL) & (close < BBM)
        sl_short = valid & (BBP_prev > 1 - params["bbp_th"]) & (close < BBU) & (close > BBM)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, params["trend_ema"])
        bb_mid_arr = BBM if params["bb_mid_exit"] else None

    elif strat == "BB_RSI":
        bb = _bb_cache(close_s, close, [params["bb_period"]], [params["bb_std"]])
        bbm = _bb_mid_cache(close_s, [params["bb_period"]])
        ema = _ema_cache(close_s, [params["trend_ema"]] if params["trend_ema"] > 0 else [])
        rsi = _rsi_cache(close_s, [params["rsi_period"]])
        BBP = bb[(params["bb_period"], params["bb_std"])]
        RSI = rsi[params["rsi_period"]]
        ros = params["rsi_os"]; rob = 100 - ros
        valid = ~np.isnan(BBP) & ~np.isnan(RSI)
        sl_long = valid & (BBP < params["bbp_th"]) & (RSI < ros)
        sl_short = valid & (BBP > 1 - params["bbp_th"]) & (RSI > rob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, params["trend_ema"])
        bb_mid_arr = bbm[params["bb_period"]] if params["bb_mid_exit"] else None

    elif strat == "RSI_Scalp":
        rsi = _rsi_cache(close_s, [params["rsi_period"]])
        ema = _ema_cache(close_s, [params["trend_ema"]] if params["trend_ema"] > 0 else [])
        RSI = rsi[params["rsi_period"]]
        os_lvl = params["os"]; ob = 100 - os_lvl
        pRSI = np.roll(RSI, 1); pRSI[0] = np.nan
        valid = ~np.isnan(RSI) & ~np.isnan(pRSI)
        sl_long = valid & (pRSI < os_lvl) & (RSI >= os_lvl)
        sl_short = valid & (pRSI > ob) & (RSI <= ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, params["trend_ema"])

    elif strat == "Stoch_Scalp":
        stoch = _stoch_cache(high_s, low_s, close_s, [params["k_period"]])
        ema = _ema_cache(close_s, [params["trend_ema"]] if params["trend_ema"] > 0 else [])
        K, D = stoch[params["k_period"]]
        os_lvl = params["os"]; ob = 100 - os_lvl
        pK = np.roll(K, 1); pK[0] = np.nan
        pD = np.roll(D, 1); pD[0] = np.nan
        valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(pK) & ~np.isnan(pD)
        sl_long = valid & (pK < os_lvl) & (pD < os_lvl) & (K > D) & (pK <= pD)
        sl_short = valid & (pK > ob) & (pD > ob) & (K < D) & (pK >= pD)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, params["trend_ema"])

    elif strat == "Williams_R":
        ema = _ema_cache(close_s, [params["trend_ema"]] if params["trend_ema"] > 0 else [])
        WR = ta.willr(high_s, low_s, close_s, length=params["wr_period"]).values.astype(float)
        os_lvl = params["os"]; ob = os_lvl + 100
        pWR = np.roll(WR, 1); pWR[0] = np.nan
        valid = ~np.isnan(WR) & ~np.isnan(pWR)
        sl_long = valid & (pWR < os_lvl) & (WR >= os_lvl)
        sl_short = valid & (pWR > ob) & (WR <= ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, params["trend_ema"])

    elif strat == "EMA_Cross":
        fp = params["ema_fast"]; sp = params["ema_slow"]
        te = params["trend_ema"]
        all_periods = {fp, sp} | ({te} if te > 0 else set())
        ema = _ema_cache(close_s, list(all_periods))
        FAST = ema[fp]; SLOW = ema[sp]
        pFAST = np.roll(FAST, 1); pFAST[0] = np.nan
        pSLOW = np.roll(SLOW, 1); pSLOW[0] = np.nan
        valid = ~np.isnan(FAST) & ~np.isnan(SLOW) & ~np.isnan(pFAST) & ~np.isnan(pSLOW)
        sl_long = valid & (FAST > SLOW) & (pFAST <= pSLOW)
        sl_short = valid & (FAST < SLOW) & (pFAST >= pSLOW)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)

    elif strat == "MACD_Cross":
        fast = params["macd_fast"]; slow = params["macd_slow"]; sig_p = params["macd_sig"]
        te = params["trend_ema"]
        df_macd = ta.macd(close_s, fast=fast, slow=slow, signal=sig_p)
        MACD = df_macd[[c for c in df_macd.columns if c.startswith("MACD_")][0]].values.astype(float)
        SIG = df_macd[[c for c in df_macd.columns if c.startswith("MACDs_")][0]].values.astype(float)
        pMACD = np.roll(MACD, 1); pMACD[0] = np.nan
        pSIG = np.roll(SIG, 1); pSIG[0] = np.nan
        valid = ~np.isnan(MACD) & ~np.isnan(SIG) & ~np.isnan(pMACD) & ~np.isnan(pSIG)
        sl_long = valid & (MACD > SIG) & (pMACD <= pSIG)
        sl_short = valid & (MACD < SIG) & (pMACD >= pSIG)
        ema = _ema_cache(close_s, [te] if te > 0 else [])
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)

    else:
        log.warning(f"[scanner_v2.wfo] Unknown strategy in replay: {strat}")
        return None

    adx_cache = _adx_cache(high_s, low_s, close_s,
                          [params["adx_period"]] if params["adx_period"] > 0 else [])
    atr_arr = _atr_cache(high_s, low_s, close_s, [ATR_PERIOD]).get(ATR_PERIOD)
    is_trend = strat in _TREND_FAMILIES
    adx_mask = _build_adx_mask(adx_cache, n, params["adx_period"], params["adx_min"], is_trend)
    sess_mask = _session_mask(ts_ms, params["session_start"], params["session_end"])
    tp_dist, sl_dist = _resolve_tp_sl_arrays(
        close, atr_arr, params["atr_tp_mode"], params["atr_tp_mult"], params["atr_sl_mult"],
    )
    return _stats(
        _backtest_v2(sl_long, sl_short, close, high, low, params["tp"], params["sl"],
                     bb_mid=bb_mid_arr, tp_dist=tp_dist, sl_dist=sl_dist,
                     session_mask=sess_mask, adx_mask=adx_mask),
        n, mins, ts_ms=ts_ms,
    )


def run_scan_wfo(asset: str, total_days: int = 180,
                 n_windows: int = 4, train_ratio: float = 0.7,
                 strategies: list[str] | None = None,
                 timeframe: str = "5m",
                 top_n: int = 5,
                 max_combos_per_family: int = _DEFAULT_MAX_COMBOS,
                 progress_cb=None) -> dict:
    """Walk-forward optimization.

    Splits total_days into n_windows sequential train/test pairs. Each window
    runs run_scan_v2 on the train portion, then replays the top_n approved
    params on the OOS portion. Returns per-window IS/OOS stats and overall
    wfo_efficiency = sum(roi_oos) / sum(roi_is).
    """
    if strategies is None:
        strategies = list(_SCANNERS_V2.keys())
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"error": f"Timeframe inválido: {timeframe}"}
    mins = _tf_minutes(timeframe)

    if timeframe == "5m":
        try:
            _update_csv(asset, progress_cb)
        except Exception as e:
            log.warning(f"[scanner_v2.wfo] _update_csv failed: {e}")

    df = _load_csv(asset, total_days + _SCAN_WARMUP_DAYS, timeframe=timeframe)
    if df is None or len(df) < 200:
        return {"error": f"CSV insuficiente para {asset}"}

    last_ts = int(df["ts_ms"].iloc[-1])
    real_start_ms = last_ts - total_days * 86_400_000

    close_full = df["close"].values.astype(float)
    high_full = df["high"].values.astype(float)
    low_full = df["low"].values.astype(float)
    ts_full = df["ts_ms"].values.astype(np.int64)

    warmup_candles = int(_SCAN_WARMUP_DAYS * 86_400_000 / (mins * 60_000))
    window_size_ms = int((total_days / n_windows) * 86_400_000)

    windows_out: list[dict] = []
    sum_roi_is = 0.0
    sum_roi_oos = 0.0

    for w in range(n_windows):
        if progress_cb:
            progress_cb(f"WFO window {w+1}/{n_windows}")
        w_start_ms = real_start_ms + w * window_size_ms
        train_end_ms = w_start_ms + int(window_size_ms * train_ratio)
        w_end_ms = w_start_ms + window_size_ms

        slice_start_idx = int(np.argmax(ts_full >= w_start_ms))
        train_end_idx = int(np.argmax(ts_full >= train_end_ms))
        if (ts_full >= w_end_ms).any():
            w_end_idx = int(np.argmax(ts_full >= w_end_ms))
        else:
            w_end_idx = len(ts_full)
        warmup_idx = max(0, slice_start_idx - warmup_candles)

        # ── IS (train) slice
        train_close = close_full[warmup_idx:train_end_idx]
        train_high = high_full[warmup_idx:train_end_idx]
        train_low = low_full[warmup_idx:train_end_idx]
        train_ts = ts_full[warmup_idx:train_end_idx]
        win_start_local = slice_start_idx - warmup_idx
        n_train_real = train_end_idx - slice_start_idx
        if n_train_real <= 0 or len(train_close) < 100:
            windows_out.append({
                "window_idx": w,
                "is": {"roi_is": 0.0, "n_approved": 0, "params": []},
                "oos": {"roi_oos": 0.0, "results": []},
                "skipped": True,
            })
            continue

        train_close_s = pd.Series(train_close)
        train_high_s = pd.Series(train_high)
        train_low_s = pd.Series(train_low)

        all_train: list[dict] = []
        for strat in strategies:
            fn = _SCANNERS_V2.get(strat)
            if fn is None:
                continue
            res = fn(train_close, train_high, train_low, n_train_real,
                     train_close_s, train_high_s, train_low_s, cb=None,
                     window_start_idx=win_start_local, mins=mins, ts_ms=train_ts,
                     max_combos=max_combos_per_family)
            all_train.extend(res)
        approved_train = sorted(
            (r for r in all_train if r["approved"]),
            key=lambda x: -x["roi"],
        )[:top_n]

        # ── OOS slice: start at train_end_idx (with warmup behind it)
        oos_warmup_idx = max(0, train_end_idx - warmup_candles)
        oos_close = close_full[oos_warmup_idx:w_end_idx]
        oos_high = high_full[oos_warmup_idx:w_end_idx]
        oos_low = low_full[oos_warmup_idx:w_end_idx]
        oos_ts = ts_full[oos_warmup_idx:w_end_idx]
        oos_close_s = pd.Series(oos_close)
        oos_high_s = pd.Series(oos_high)
        oos_low_s = pd.Series(oos_low)

        oos_results = []
        for p in approved_train:
            stats = _replay_params_on_slice(
                oos_close, oos_high, oos_low, oos_ts,
                oos_close_s, oos_high_s, oos_low_s, mins, p,
            )
            oos_results.append(stats or {"trades": 0, "roi": 0.0})

        roi_is = sum(p.get("roi", 0.0) for p in approved_train)
        roi_oos = sum(r.get("roi", 0.0) for r in oos_results)
        sum_roi_is += roi_is
        sum_roi_oos += roi_oos

        windows_out.append({
            "window_idx": w,
            "is": {"roi_is": roi_is, "n_approved": len(approved_train), "params": approved_train},
            "oos": {"roi_oos": roi_oos, "results": oos_results},
        })

    efficiency = (sum_roi_oos / sum_roi_is) if sum_roi_is > 0 else 0.0
    return {
        "asset": asset,
        "timeframe": timeframe,
        "total_days": total_days,
        "n_windows": n_windows,
        "train_ratio": train_ratio,
        "top_n": top_n,
        "windows": windows_out,
        "wfo_efficiency": efficiency,
        "sum_roi_is": sum_roi_is,
        "sum_roi_oos": sum_roi_oos,
    }


# ── Job management ────────────────────────────────────────────────────────

_jobs: dict = {}
_jobs_lock = threading.Lock()


def _make_progress_cb(job_id: str):
    def cb(msg: str):
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["progress"] = msg
    return cb


def start_scan_v2_job(asset: str, days: int = 90,
                      strategies: list[str] | None = None,
                      timeframe: str = "5m",
                      max_combos_per_family: int = _DEFAULT_MAX_COMBOS) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"kind": "scan", "status": "running", "progress": "Iniciando...",
                         "result": None, "error": None}

    def _run():
        try:
            result = run_scan_v2(asset, days, strategies,
                                 progress_cb=_make_progress_cb(job_id),
                                 timeframe=timeframe,
                                 max_combos_per_family=max_combos_per_family)
            with _jobs_lock:
                if "error" in result:
                    _jobs[job_id].update(status="error", error=result["error"])
                else:
                    _jobs[job_id].update(status="done", result=result)
        except Exception as exc:
            log.error(f"[scanner_v2] Job {job_id} failed: {exc}")
            with _jobs_lock:
                _jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def start_wfo_job(asset: str, total_days: int = 180,
                  n_windows: int = 4, train_ratio: float = 0.7,
                  strategies: list[str] | None = None,
                  timeframe: str = "5m",
                  top_n: int = 5,
                  max_combos_per_family: int = _DEFAULT_MAX_COMBOS) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"kind": "wfo", "status": "running", "progress": "Iniciando WFO...",
                         "result": None, "error": None}

    def _run():
        try:
            result = run_scan_wfo(asset, total_days=total_days, n_windows=n_windows,
                                  train_ratio=train_ratio, strategies=strategies,
                                  timeframe=timeframe, top_n=top_n,
                                  max_combos_per_family=max_combos_per_family,
                                  progress_cb=_make_progress_cb(job_id))
            with _jobs_lock:
                if "error" in result:
                    _jobs[job_id].update(status="error", error=result["error"])
                else:
                    _jobs[job_id].update(status="done", result=result)
        except Exception as exc:
            log.error(f"[scanner_v2.wfo] Job {job_id} failed: {exc}")
            with _jobs_lock:
                _jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


# ── Replay combos across time windows ─────────────────────────────────────

_MAX_REPLAY_COMBOS = 10


def replay_combos_in_windows(asset: str, combos: list[dict], n_windows: int = 6,
                             days: int = 180, timeframe: str = "5m",
                             progress_cb=None) -> dict:
    """For each combo (specific param dict), slice the period into n_windows
    sequential chunks and replay the combo against each. No IS/OOS split —
    pure temporal stability check.

    Returns:
        {
          "combos": [
            {
              "params": {...},
              "windows": [{idx, start_ms, end_ms, trades, wr, pf, roi, max_dd}, ...],
              "summary": {
                "pct_positive_windows": float,
                "worst_window_roi": float,
                "median_window_roi": float,
                "total_roi": float,
                "n_windows": int,
              }
            },
            ...
          ]
        }
    """
    if not combos:
        return {"error": "combos vazio"}
    if len(combos) > _MAX_REPLAY_COMBOS:
        return {"error": f"Máximo {_MAX_REPLAY_COMBOS} combos por replay (recebeu {len(combos)})"}
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"error": f"Timeframe inválido: {timeframe}"}
    if n_windows < 2 or n_windows > 24:
        return {"error": f"n_windows deve estar entre 2 e 24 (recebeu {n_windows})"}

    mins = _tf_minutes(timeframe)

    if timeframe == "5m":
        try:
            _update_csv(asset, progress_cb)
        except Exception as e:
            log.warning(f"[scanner_v2.replay] _update_csv failed: {e}")

    df = _load_csv(asset, days + _SCAN_WARMUP_DAYS, timeframe=timeframe)
    if df is None or len(df) < 100:
        return {"error": f"CSV insuficiente para {asset}"}

    last_ts = int(df["ts_ms"].iloc[-1])
    real_start_ms = last_ts - days * 86_400_000

    close_full = df["close"].values.astype(float)
    high_full = df["high"].values.astype(float)
    low_full = df["low"].values.astype(float)
    ts_full = df["ts_ms"].values.astype(np.int64)

    warmup_candles = int(_SCAN_WARMUP_DAYS * 86_400_000 / (mins * 60_000))
    window_size_ms = int((days / n_windows) * 86_400_000)

    out_combos = []
    for ci, combo in enumerate(combos):
        if progress_cb:
            progress_cb(f"Replay combo {ci+1}/{len(combos)}")
        windows_out = []
        for w in range(n_windows):
            w_start_ms = real_start_ms + w * window_size_ms
            w_end_ms = w_start_ms + window_size_ms

            slice_start_idx = int(np.argmax(ts_full >= w_start_ms))
            if (ts_full >= w_end_ms).any():
                w_end_idx = int(np.argmax(ts_full >= w_end_ms))
            else:
                w_end_idx = len(ts_full)
            warmup_idx = max(0, slice_start_idx - warmup_candles)

            close_slice = close_full[warmup_idx:w_end_idx]
            high_slice = high_full[warmup_idx:w_end_idx]
            low_slice = low_full[warmup_idx:w_end_idx]
            ts_slice = ts_full[warmup_idx:w_end_idx]
            if len(close_slice) < 50:
                windows_out.append({
                    "idx": w, "start_ms": int(w_start_ms), "end_ms": int(w_end_ms),
                    "trades": 0, "wr": 0.0, "pf": 0.0, "roi": 0.0, "max_dd": 0.0,
                    "skipped": True,
                })
                continue

            close_s = pd.Series(close_slice)
            high_s = pd.Series(high_slice)
            low_s = pd.Series(low_slice)

            stats = _replay_params_on_slice(
                close_slice, high_slice, low_slice, ts_slice,
                close_s, high_s, low_s, mins, combo,
            )
            if stats is None:
                stats = {"trades": 0, "wr": 0.0, "pf": 0.0, "roi": 0.0, "max_dd": 0.0}
            windows_out.append({
                "idx": w,
                "start_ms": int(w_start_ms),
                "end_ms": int(w_end_ms),
                "trades": stats.get("trades", 0),
                "wr": stats.get("wr", 0.0),
                "pf": stats.get("pf", 0.0),
                "roi": stats.get("roi", 0.0),
                "max_dd": stats.get("max_dd", 0.0),
            })

        rois = [w["roi"] for w in windows_out if not w.get("skipped")]
        n_real = len(rois)
        n_pos = sum(1 for r in rois if r > 0)
        out_combos.append({
            "params": combo,
            "windows": windows_out,
            "summary": {
                "n_windows": n_real,
                "pct_positive_windows": round(n_pos / n_real * 100, 1) if n_real else 0.0,
                "worst_window_roi": round(min(rois), 2) if rois else 0.0,
                "best_window_roi": round(max(rois), 2) if rois else 0.0,
                "median_window_roi": round(float(np.median(rois)), 2) if rois else 0.0,
                "total_roi": round(sum(rois), 2),
            },
        })

    return {
        "asset": asset,
        "timeframe": timeframe,
        "days": days,
        "n_windows": n_windows,
        "window_days": round(days / n_windows, 1),
        "combos": out_combos,
    }


def start_replay_job(asset: str, combos: list[dict], n_windows: int = 6,
                     days: int = 180, timeframe: str = "5m") -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"kind": "replay", "status": "running",
                         "progress": "Iniciando replay...",
                         "result": None, "error": None}

    def _run():
        try:
            result = replay_combos_in_windows(
                asset, combos, n_windows=n_windows, days=days, timeframe=timeframe,
                progress_cb=_make_progress_cb(job_id),
            )
            with _jobs_lock:
                if "error" in result:
                    _jobs[job_id].update(status="error", error=result["error"])
                else:
                    _jobs[job_id].update(status="done", result=result)
        except Exception as exc:
            log.error(f"[scanner_v2.replay] Job {job_id} failed: {exc}")
            with _jobs_lock:
                _jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return job_id
