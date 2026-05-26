"""
Strategy Scanner — vectorized parameter grid search over historical OHLCV.

Replicates the logic of the external scan scripts, using the same signal
conditions as the live strategies (after the scan-alignment refactor).
Runs both long and short symmetrically; no BB mid exit.
"""

import threading
import uuid
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as ta

from bot.backtest.csv_loader import _update_csv
from bot.logger import get_logger

log = get_logger("backtest.scanner")

_CANDLES_DIR = Path(__file__).parents[3] / "candles"

# Extra days loaded BEFORE the requested window so indicators (BB, EMA, RSI,
# Stoch, MACD, Williams %R) are already warm at the first candle of the
# "real" window. 2 days = 576 candles 5m — folga ampla para qualquer período
# (max indicador usado = ~30 candles).
_SCAN_WARMUP_DAYS = 2

# ── Approval criteria ──────────────────────────────────────────────────────
APPROVAL = {
    "pf_min":     1.1,
    "wr_min":     0.50,
    "tpd_min":    1.0,
    "tpd_max":    15.0,
    "max_dd_max": 30.0,
    "min_trades": 5,
}

# ── Timeframe helpers ──────────────────────────────────────────────────────

SUPPORTED_TIMEFRAMES = ["5m", "15m", "30m", "1h"]
_TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}


def _tf_minutes(tf: str) -> int:
    return _TF_MINUTES.get(tf, 5)


# ── Job registry ───────────────────────────────────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()


# ── CSV loading ────────────────────────────────────────────────────────────

def _load_csv(asset: str, days: int | None = None, timeframe: str = "5m") -> pd.DataFrame | None:
    csv_path = _CANDLES_DIR / f"{asset.lower()}_{timeframe}.csv"
    if not csv_path.exists():
        return None

    df = pd.read_csv(csv_path)
    ts_col = "timestamp" if "timestamp" in df.columns else "ts"

    sample = df[ts_col].iloc[0]
    if isinstance(sample, str):
        df["ts_ms"] = pd.to_datetime(df[ts_col]).astype(np.int64) // 1_000_000
    else:
        v = int(sample)
        df["ts_ms"] = df[ts_col].astype(np.int64) if v > 1e12 else (df[ts_col] * 1000).astype(np.int64)

    df = df.sort_values("ts_ms").reset_index(drop=True)

    if days:
        cutoff = df["ts_ms"].iloc[-1] - days * 86_400_000
        df = df[df["ts_ms"] >= cutoff].reset_index(drop=True)

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.dropna(subset=["close", "high", "low"]).reset_index(drop=True)


def get_available_assets(timeframe: str = "5m") -> list[str]:
    suffix = f"_{timeframe}"
    return sorted(
        p.stem.replace(suffix, "").upper()
        for p in _CANDLES_DIR.glob(f"*{suffix}.csv")
    )


# ── Trade simulation ───────────────────────────────────────────────────────

def _backtest(sig_long: np.ndarray, sig_short: np.ndarray,
              close: np.ndarray, high: np.ndarray, low: np.ndarray,
              tp_pct: float, sl_pct: float,
              bb_mid: np.ndarray | None = None) -> list[tuple[float, int]]:
    """
    Bar-by-bar simulation. No overlap between trades.

    Per-candle priority: SL > TP > BB-mid (matches engine.py).
    On a same-bar SL+TP hit, SL always wins (pessimistic).

    Returns list of (return_pct, entry_idx) tuples:
      - +tp_pct on TP, -sl_pct on SL
      - Actual percent change (close vs entry) on BB-mid exit
      - entry_idx é o índice do candle de entrada (usado pelo breakdown mensal)
    """
    trades: list[tuple[float, int]] = []
    N = len(close)
    i = 0
    while i < N - 1:
        is_long = bool(sig_long[i])
        is_short = bool(sig_short[i])
        if not is_long and not is_short:
            i += 1
            continue

        entry = close[i]
        if is_long:
            tp = entry * (1 + tp_pct / 100)
            sl = entry * (1 - sl_pct / 100)
        else:
            tp = entry * (1 - tp_pct / 100)
            sl = entry * (1 + sl_pct / 100)

        j = i + 1
        outcome = None        # ("win"|"loss"|"bb_mid", j)
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
                trades.append((tp_pct, i))
            elif kind == "loss":
                trades.append((-sl_pct, i))
            else:  # bb_mid — actual percent change
                ret = (close[j_exit] - entry) / entry * 100
                if not is_long:
                    ret = -ret
                trades.append((float(ret), i))
            i = j_exit + 1
        else:
            i += 1

    return trades


# ── Stats ──────────────────────────────────────────────────────────────────

def _monthly_breakdown(trades_with_idx: list[tuple[float, int]],
                       ts_ms: np.ndarray) -> list[dict]:
    """Agrupa trades por mês calendário (YYYY-MM) usando ts_ms[entry_idx].
    Retorna lista ordenada por mês: [{month, trades, wr, roi, pf}, ...]"""
    if not trades_with_idx or ts_ms is None or len(ts_ms) == 0:
        return []
    buckets: dict[str, list[float]] = {}
    for ret, idx in trades_with_idx:
        if idx < 0 or idx >= len(ts_ms):
            continue
        month = pd.Timestamp(int(ts_ms[idx]), unit="ms", tz="UTC").strftime("%Y-%m")
        buckets.setdefault(month, []).append(ret)
    out = []
    for month in sorted(buckets):
        rets = buckets[month]
        wins = [t for t in rets if t > 0]
        losses = [t for t in rets if t <= 0]
        wr = len(wins) / len(rets) if rets else 0.0
        gp = sum(wins)
        gl = abs(sum(losses)) if losses else 0.0
        pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
        out.append({
            "month":  month,
            "trades": len(rets),
            "wr":     round(wr, 4),
            "roi":    round(sum(rets), 2),
            "pf":     round(pf, 4),
        })
    return out


def _stats(trades_with_idx: list[tuple[float, int]],
           n_candles: int,
           mins_per_candle: int = 5,
           ts_ms: np.ndarray | None = None) -> dict | None:
    if len(trades_with_idx) < APPROVAL["min_trades"]:
        return None

    trades = [t[0] for t in trades_with_idx]
    wins   = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    wr  = len(wins) / len(trades)
    gp  = sum(wins)
    gl  = abs(sum(losses)) if losses else 0.0
    pf  = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    roi = sum(trades)

    total_days = n_candles * mins_per_candle / 60 / 24
    tpd = len(trades) / total_days if total_days > 0 else 0.0

    eq, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        eq += t
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd

    approved = (
        pf  >= APPROVAL["pf_min"]    and
        wr  >= APPROVAL["wr_min"]    and
        APPROVAL["tpd_min"] <= tpd <= APPROVAL["tpd_max"] and
        max_dd <= APPROVAL["max_dd_max"]
    )

    result = {
        "trades": len(trades),
        "wr":     round(wr, 4),
        "pf":     round(pf, 4),
        "roi":    round(roi, 2),
        "tpd":    round(tpd, 3),
        "max_dd": round(max_dd, 2),
        "approved": approved,
    }
    # Só adiciona breakdown se ts_ms passado E período cobre >1 mês
    if ts_ms is not None and total_days >= 31:
        result["monthly"] = _monthly_breakdown(trades_with_idx, ts_ms)
    return result


# ── Indicator helpers ──────────────────────────────────────────────────────

def _ema_cache(close_s: pd.Series, periods: list[int]) -> dict[int, np.ndarray]:
    cache = {}
    for p in periods:
        if p > 0:
            cache[p] = ta.ema(close_s, length=p).values.astype(float)
    return cache


def _bb_cache(close_s: pd.Series, close: np.ndarray,
              periods: list[int], stds: list[float]) -> dict:
    cache = {}
    for bbp, bbs in product(periods, stds):
        bb = ta.bbands(close_s, length=bbp, std=bbs)
        bbu = bb[[c for c in bb.columns if c.startswith("BBU_")][0]].values.astype(float)
        bbl = bb[[c for c in bb.columns if c.startswith("BBL_")][0]].values.astype(float)
        span = bbu - bbl
        with np.errstate(invalid="ignore", divide="ignore"):
            bbp_arr = np.where(span > 0, (close - bbl) / span, np.nan)
        cache[(bbp, bbs)] = bbp_arr
    return cache


def _bb_mid_cache(close_s: pd.Series, periods: list[int]) -> dict[int, np.ndarray]:
    """BBM (middle band = SMA(period)) per period — used when bb_mid_exit=True."""
    cache = {}
    for p in periods:
        cache[p] = ta.sma(close_s, length=p).values.astype(float)
    return cache


def _stoch_cache(high_s, low_s, close_s, k_periods: list[int]) -> dict:
    cache = {}
    for kp in k_periods:
        df = ta.stoch(high_s, low_s, close_s, k=kp, d=3, smooth_k=3)
        K = df[[c for c in df.columns if c.startswith("STOCHk_")][0]].values.astype(float)
        D = df[[c for c in df.columns if c.startswith("STOCHd_")][0]].values.astype(float)
        cache[kp] = (K, D)
    return cache


def _apply_window(sig_long: np.ndarray, sig_short: np.ndarray, window_start_idx: int) -> tuple[np.ndarray, np.ndarray]:
    """Zero out signals BEFORE the "real" window — indicators were warmed up
    on the prior warmup region, but those candles aren't part of the requested
    period and shouldn't produce counted trades."""
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


def _scale_tp_sl(tps: list[float], sls: list[float], mins: int) -> tuple[list[float], list[float]]:
    """Escala TP/SL pela volatilidade do TF — sqrt(mins/5) replica o scaling random-walk
    da volatilidade com tempo. 5m mantém base; 15m ~1.73×; 30m ~2.45×; 1h ~3.46×.
    Sem isso, scans em TFs maiores usariam TP/SL muito apertados (dentro do range de
    1 vela) e inflariam artificialmente a WR.
    """
    if mins <= 5:
        return list(tps), list(sls)
    scale = (mins / 5.0) ** 0.5
    return ([round(t * scale, 2) for t in tps],
            [round(s * scale, 2) for s in sls])


# ── Strategy scanners ──────────────────────────────────────────────────────

def _scan_bb_stoch(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    BB_PERIODS  = [10, 15]
    BB_STDS     = [1.5, 2.0]
    BBP_THS     = [0.05, 0.10, 0.15]
    STOCH_OS    = [20, 25, 30]
    STOCH_K     = 14
    TREND_EMAS  = [0, 50, 200]
    TPS         = [0.5, 0.8, 1.0, 1.5]
    SLS         = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)
    BB_MID_EXITS = [False, True]

    bb  = _bb_cache(close_s, close, BB_PERIODS, BB_STDS)
    bbm = _bb_mid_cache(close_s, BB_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    K, D = _stoch_cache(high_s, low_s, close_s, [STOCH_K])[STOCH_K]

    combos = list(product(BB_PERIODS, BB_STDS, BBP_THS, STOCH_OS, TREND_EMAS, TPS, SLS, BB_MID_EXITS))
    results = []
    for idx, (bbp, bbs, bbp_th, sos, te, tp, sl, bme) in enumerate(combos):
        if cb and idx % 100 == 0:
            cb(f"BB Stoch {idx}/{len(combos)}")

        BBP = bb[(bbp, bbs)]
        ob  = 100 - sos
        valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(BBP)

        sl_long  = valid & (BBP < bbp_th)        & (K < sos) & (D < sos)
        sl_short = valid & (BBP > 1 - bbp_th)    & (K > ob)  & (D > ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        bb_mid_arr = bbm[bbp] if bme else None
        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl, bb_mid=bb_mid_arr), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "BB_Stoch",
                            "bb_period": bbp, "bb_std": bbs, "bbp_th": bbp_th,
                            "bb_mid_exit": bme,
                            "stoch_k": STOCH_K, "stoch_os": sos,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


def _scan_stoch_scalp(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    K_PERIODS  = [5, 9, 14]
    OS_LEVELS  = [30, 40, 50]
    TREND_EMAS = [0, 50, 200]
    TPS        = [0.5, 0.8, 1.0]
    SLS        = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    stoch = _stoch_cache(high_s, low_s, close_s, K_PERIODS)
    ema   = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])

    combos = list(product(K_PERIODS, OS_LEVELS, TREND_EMAS, TPS, SLS))
    results = []
    for idx, (kp, os_lvl, te, tp, sl) in enumerate(combos):
        if cb and idx % 50 == 0:
            cb(f"Stoch Scalp {idx}/{len(combos)}")

        K, D  = stoch[kp]
        ob    = 100 - os_lvl
        pK    = np.roll(K, 1); pK[0] = np.nan
        pD    = np.roll(D, 1); pD[0] = np.nan
        valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(pK) & ~np.isnan(pD)

        sl_long  = valid & (pK < os_lvl) & (pD < os_lvl) & (K > D) & (pK <= pD)
        sl_short = valid & (pK > ob)     & (pD > ob)     & (K < D) & (pK >= pD)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "Stoch_Scalp",
                            "k_period": kp, "os": os_lvl,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


def _scan_ema_cross(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    PAIRS      = [(3, 8), (5, 13), (9, 21), (3, 13), (8, 21)]
    TREND_EMAS = [0, 50, 200]
    TPS        = [0.5, 1.0, 1.5]
    SLS        = [0.3, 0.5, 0.8]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    all_periods = {p for pair in PAIRS for p in pair} | {e for e in TREND_EMAS if e > 0}
    ema = _ema_cache(close_s, list(all_periods))

    combos = list(product(PAIRS, TREND_EMAS, TPS, SLS))
    results = []
    for idx, ((fp, sp), te, tp, sl) in enumerate(combos):
        if cb and idx % 30 == 0:
            cb(f"EMA Cross {idx}/{len(combos)}")

        FAST  = ema[fp]; SLOW  = ema[sp]
        pFAST = np.roll(FAST, 1); pFAST[0] = np.nan
        pSLOW = np.roll(SLOW, 1); pSLOW[0] = np.nan
        valid = ~np.isnan(FAST) & ~np.isnan(SLOW) & ~np.isnan(pFAST) & ~np.isnan(pSLOW)

        sl_long  = valid & (FAST > SLOW) & (pFAST <= pSLOW)
        sl_short = valid & (FAST < SLOW) & (pFAST >= pSLOW)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "EMA_Cross",
                            "ema_fast": fp, "ema_slow": sp,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


def _scan_bb_reversion(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    BB_PERIODS = [10, 15, 20]
    BB_STDS    = [1.5, 2.0, 2.5]
    BBP_THS    = [0.05, 0.10, 0.15, 0.20]
    TREND_EMAS = [0, 50, 200]
    TPS        = [0.5, 1.0, 1.5, 2.0]
    SLS        = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)
    BB_MID_EXITS = [False, True]

    bb  = _bb_cache(close_s, close, BB_PERIODS, BB_STDS)
    bbm = _bb_mid_cache(close_s, BB_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])

    # Cache full bands (BBU, BBL) per (period, std) — needed for the strict reversion
    # entry rule that matches the live bb_reversion strategy.
    bands = {}
    for bbp_p, bbs_s in product(BB_PERIODS, BB_STDS):
        bb_df = ta.bbands(close_s, length=bbp_p, std=bbs_s)
        bands[(bbp_p, bbs_s)] = (
            bb_df[[c for c in bb_df.columns if c.startswith("BBU_")][0]].values.astype(float),
            bb_df[[c for c in bb_df.columns if c.startswith("BBL_")][0]].values.astype(float),
        )

    combos = list(product(BB_PERIODS, BB_STDS, BBP_THS, TREND_EMAS, TPS, SLS, BB_MID_EXITS))
    results = []
    for idx, (bbp, bbs, bbp_th, te, tp, sl, bme) in enumerate(combos):
        if cb and idx % 100 == 0:
            cb(f"BB Reversion {idx}/{len(combos)}")

        BBP   = bb[(bbp, bbs)]
        BBU, BBL = bands[(bbp, bbs)]
        BBM   = bbm[bbp]
        BBP_prev = np.roll(BBP, 1); BBP_prev[0] = np.nan
        valid = ~np.isnan(BBP_prev) & ~np.isnan(BBU) & ~np.isnan(BBL) & ~np.isnan(BBM)

        # Same entry as live bot: previous candle outside band, current candle reverting back inside.
        sl_long  = valid & (BBP_prev < bbp_th)       & (close > BBL) & (close < BBM)
        sl_short = valid & (BBP_prev > 1 - bbp_th)   & (close < BBU) & (close > BBM)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        bb_mid_arr = BBM if bme else None
        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl, bb_mid=bb_mid_arr), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "BB_Reversion",
                            "bb_period": bbp, "bb_std": bbs, "bbp_th": bbp_th,
                            "bb_mid_exit": bme,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


def _rsi_cache(close_s: pd.Series, periods: list[int]) -> dict[int, np.ndarray]:
    return {p: ta.rsi(close_s, length=p).values.astype(float) for p in periods}


def _scan_rsi_scalp(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    RSI_PERIODS = [7, 14]
    OS_LEVELS   = [25, 30, 35, 40]
    TREND_EMAS  = [0, 50, 200]
    TPS         = [0.5, 0.8, 1.0]
    SLS         = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    rsi = _rsi_cache(close_s, RSI_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])

    combos = list(product(RSI_PERIODS, OS_LEVELS, TREND_EMAS, TPS, SLS))
    results = []
    for idx, (rp, os_lvl, te, tp, sl) in enumerate(combos):
        if cb and idx % 30 == 0:
            cb(f"RSI Scalp {idx}/{len(combos)}")

        RSI   = rsi[rp]
        ob    = 100 - os_lvl
        pRSI  = np.roll(RSI, 1); pRSI[0] = np.nan
        valid = ~np.isnan(RSI) & ~np.isnan(pRSI)

        sl_long  = valid & (pRSI <  os_lvl) & (RSI >= os_lvl)
        sl_short = valid & (pRSI >  ob)     & (RSI <= ob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "RSI_Scalp",
                            "rsi_period": rp, "os": os_lvl,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


def _scan_bb_rsi(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    BB_PERIODS  = [10, 15]
    BB_STDS     = [1.5, 2.0]
    BBP_THS     = [0.05, 0.10, 0.15]
    RSI_PERIODS = [7, 14]
    RSI_OS      = [25, 30, 35]
    TREND_EMAS  = [0, 50, 200]
    TPS         = [0.8, 1.5]
    SLS         = [0.5, 0.8]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)
    BB_MID_EXITS = [False, True]

    bb  = _bb_cache(close_s, close, BB_PERIODS, BB_STDS)
    bbm = _bb_mid_cache(close_s, BB_PERIODS)
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])
    rsi = _rsi_cache(close_s, RSI_PERIODS)

    combos = list(product(BB_PERIODS, BB_STDS, BBP_THS, RSI_PERIODS, RSI_OS, TREND_EMAS, TPS, SLS, BB_MID_EXITS))
    results = []
    for idx, (bbp, bbs, bbp_th, rp, ros, te, tp, sl, bme) in enumerate(combos):
        if cb and idx % 100 == 0:
            cb(f"BB RSI {idx}/{len(combos)}")

        BBP   = bb[(bbp, bbs)]
        RSI   = rsi[rp]
        rob   = 100 - ros
        valid = ~np.isnan(BBP) & ~np.isnan(RSI)

        sl_long  = valid & (BBP < bbp_th)     & (RSI < ros)
        sl_short = valid & (BBP > 1 - bbp_th) & (RSI > rob)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        bb_mid_arr = bbm[bbp] if bme else None
        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl, bb_mid=bb_mid_arr), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "BB_RSI",
                            "bb_period": bbp, "bb_std": bbs, "bbp_th": bbp_th,
                            "bb_mid_exit": bme,
                            "rsi_period": rp, "rsi_os": ros,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


def _scan_macd_cross(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    MACD_COMBOS = [(8, 21, 7), (8, 21, 9), (12, 26, 7), (12, 26, 9), (8, 26, 9)]
    TREND_EMAS  = [0, 50, 200]
    TPS         = [0.5, 1.0, 1.5]
    SLS         = [0.3, 0.5, 0.8]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    macd_cache = {}
    for fast, slow, sig in MACD_COMBOS:
        df   = ta.macd(close_s, fast=fast, slow=slow, signal=sig)
        mcol = [c for c in df.columns if c.startswith("MACD_")][0]   # MACD_ not MACDh_ or MACDs_
        scol = [c for c in df.columns if c.startswith("MACDs_")][0]
        macd_cache[(fast, slow, sig)] = (
            df[mcol].values.astype(float),
            df[scol].values.astype(float),
        )

    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])

    combos = list(product(MACD_COMBOS, TREND_EMAS, TPS, SLS))
    results = []
    for idx, ((fast, slow, sig), te, tp, sl) in enumerate(combos):
        if cb and idx % 20 == 0:
            cb(f"MACD Cross {idx}/{len(combos)}")

        MACD, SIG = macd_cache[(fast, slow, sig)]
        pMACD = np.roll(MACD, 1); pMACD[0] = np.nan
        pSIG  = np.roll(SIG,  1); pSIG[0]  = np.nan
        valid = ~np.isnan(MACD) & ~np.isnan(SIG) & ~np.isnan(pMACD) & ~np.isnan(pSIG)

        sl_long  = valid & (MACD > SIG) & (pMACD <= pSIG)
        sl_short = valid & (MACD < SIG) & (pMACD >= pSIG)
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "MACD_Cross",
                            "macd_fast": fast, "macd_slow": slow, "macd_sig": sig,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


def _scan_williams_r(close, high, low, n, close_s, high_s, low_s, cb, *, window_start_idx: int = 0, mins: int = 5, ts_ms=None):
    WR_PERIODS = [7, 14]
    OS_LEVELS  = [-80, -70, -60]   # Williams %R: -100=oversold, 0=overbought
    TREND_EMAS = [0, 50, 200]
    TPS        = [0.5, 0.8, 1.0]
    SLS        = [0.5, 0.8, 1.0]
    TPS, SLS = _scale_tp_sl(TPS, SLS, mins)

    wr_cache = {wp: ta.willr(high_s, low_s, close_s, length=wp).values.astype(float)
                for wp in WR_PERIODS}
    ema = _ema_cache(close_s, [e for e in TREND_EMAS if e > 0])

    combos = list(product(WR_PERIODS, OS_LEVELS, TREND_EMAS, TPS, SLS))
    results = []
    for idx, (wp, os_lvl, te, tp, sl) in enumerate(combos):
        if cb and idx % 30 == 0:
            cb(f"Williams %R {idx}/{len(combos)}")

        WR    = wr_cache[wp]
        ob    = os_lvl + 100        # e.g. os=-80 → ob=-20
        pWR   = np.roll(WR, 1); pWR[0] = np.nan
        valid = ~np.isnan(WR) & ~np.isnan(pWR)

        sl_long  = valid & (pWR <  os_lvl) & (WR >= os_lvl)   # exits oversold
        sl_short = valid & (pWR >  ob)     & (WR <= ob)        # exits overbought
        sl_long, sl_short = _apply_ema_filter(sl_long, sl_short, close, ema, te)
        sl_long, sl_short = _apply_window(sl_long, sl_short, window_start_idx)

        s = _stats(_backtest(sl_long, sl_short, close, high, low, tp, sl), n, mins, ts_ms=ts_ms)
        if s:
            results.append({"strategy": "Williams_R",
                            "wr_period": wp, "os": os_lvl,
                            "trend_ema": te, "tp": tp, "sl": sl, **s})
    return results


# ── Main scan entry point ──────────────────────────────────────────────────

_SCANNERS = {
    "BB_Stoch":     _scan_bb_stoch,
    "Stoch_Scalp":  _scan_stoch_scalp,
    "EMA_Cross":    _scan_ema_cross,
    "BB_Reversion": _scan_bb_reversion,
    "RSI_Scalp":    _scan_rsi_scalp,
    "BB_RSI":       _scan_bb_rsi,
    "MACD_Cross":   _scan_macd_cross,
    "Williams_R":   _scan_williams_r,
}


def run_scan(asset: str, days: int = 90,
             strategies: list[str] | None = None,
             progress_cb=None,
             timeframe: str = "5m") -> dict:
    if strategies is None:
        strategies = list(_SCANNERS.keys())
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"error": f"Timeframe inválido: {timeframe}. Suportados: {SUPPORTED_TIMEFRAMES}"}
    mins = _tf_minutes(timeframe)

    # Pull missing candles from Lighter REST before scanning (5m only — auto-update only suporta 5m).
    if timeframe == "5m":
        if progress_cb:
            progress_cb(f"Atualizando CSV de {asset}...")
        try:
            _update_csv(asset, progress_cb)
        except Exception as e:
            log.warning(f"[scanner] _update_csv failed for {asset}: {e}")

    # Load `days + warmup` so indicators are already warm at the first candle
    # of the requested window. The "real" window starts at `window_start_idx`.
    df = _load_csv(asset, days + _SCAN_WARMUP_DAYS, timeframe=timeframe)
    if df is None or len(df) < 100:
        return {"error": f"CSV {asset.lower()}_{timeframe}.csv não encontrado ou insuficiente para {asset}"}

    last_ts_ms = int(df["ts_ms"].iloc[-1])
    real_start_ms = last_ts_ms - days * 86_400_000
    in_window = df["ts_ms"].values >= real_start_ms
    if in_window.any():
        window_start_idx = int(np.argmax(in_window))
    else:
        window_start_idx = 0  # fallback if CSV doesn't span the requested window

    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    ts_ms = df["ts_ms"].values.astype(np.int64)
    n_real = len(close) - window_start_idx  # number of candles in the "real" window — used for TPD

    close_s = pd.Series(close)
    high_s  = pd.Series(high)
    low_s   = pd.Series(low)

    all_results: list[dict] = []
    for strat in strategies:
        fn = _SCANNERS.get(strat)
        if fn is None:
            continue
        if progress_cb:
            progress_cb(f"Escaneando {strat}...")
        results = fn(close, high, low, n_real, close_s, high_s, low_s, progress_cb,
                     window_start_idx=window_start_idx, mins=mins, ts_ms=ts_ms)
        # Marca cada resultado com o timeframe — necessário para Apply / display
        for r in results:
            r["tf"] = timeframe
        all_results.extend(results)
        n_ap = sum(1 for r in results if r["approved"])
        log.info(f"[scanner] {asset} {strat}: {len(results)} testados, {n_ap} aprovados")

    approved = sorted(
        (r for r in all_results if r["approved"]),
        key=lambda x: -x["roi"],
    )

    summary = {}
    for strat in strategies:
        sr = [r for r in all_results if r["strategy"] == strat]
        sa = [r for r in sr if r["approved"]]
        summary[strat] = {
            "tested":    len(sr),
            "approved":  len(sa),
            "best_roi":  max(sa, key=lambda x: x["roi"])  if sa else None,
            "best_wr":   max(sa, key=lambda x: x["wr"])   if sa else None,
        }

    if progress_cb:
        progress_cb("Concluído")

    return {
        "asset":              asset,
        "timeframe":          timeframe,
        "days":               days,
        "candles":            n_real,
        "total_tested":       len(all_results),
        "total_approved":     len(approved),
        "approval_criteria":  APPROVAL,
        "per_strategy":       summary,
        "approved":           approved[:100],
    }


# ── Params translation ────────────────────────────────────────────────────
# _INSTANCE_MAP foi removido — após migration M6 todas as instâncias têm `_5m` no
# nome e são criadas/atualizadas via `manager.register_dynamic_instance(...)` com TF.


def _translate_params(strategy: str, params: dict) -> dict:
    if strategy == "BB_Stoch":
        th = float(params.get("bbp_th", 0.10))
        return {
            "bb_period":           params.get("bb_period"),
            "bb_std":              params.get("bb_std"),
            "bbp_long_threshold":  th,
            "bbp_short_threshold": round(1 - th, 4),
            "stoch_k":             params.get("stoch_k", 14),
            "stoch_long":          params.get("stoch_os"),
            "stoch_short":         100 - int(params.get("stoch_os", 25)),
            "ema_period":          params.get("trend_ema", 0),
            "tp_pct":              params.get("tp"),
            "sl_pct":              params.get("sl"),
            "bb_mid_exit":         bool(params.get("bb_mid_exit", False)),
        }
    if strategy == "Stoch_Scalp":
        return {
            "stoch_k":    params.get("k_period"),
            "stoch_os":   params.get("os"),
            "ema_period": params.get("trend_ema", 0),
            "tp_pct":     params.get("tp"),
            "sl_pct":     params.get("sl"),
        }
    if strategy == "EMA_Cross":
        return {
            "ema_fast":   params.get("ema_fast"),
            "ema_slow":   params.get("ema_slow"),
            "ema_trend":  params.get("trend_ema", 0),
            "tp_pct":     params.get("tp"),
            "sl_pct":     params.get("sl"),
            "use_atr_sl": False,  # scanner usa sl_pct fixo, força fixed-mode mesmo em instância com ATR
        }
    if strategy == "BB_Reversion":
        th = float(params.get("bbp_th", 0.10))
        return {
            "bb_period":           params.get("bb_period"),
            "bb_std":              params.get("bb_std"),
            "bbp_long_threshold":  th,
            "bbp_short_threshold": round(1 - th, 4),
            "ema_period":          params.get("trend_ema", 0),
            "tp_pct":              params.get("tp"),
            "sl_pct":              params.get("sl"),
            "rsi_long_max":        100,    # scanner não modela RSI guard — desabilita
            "rsi_short_min":       0,
            "bb_mid_exit":         bool(params.get("bb_mid_exit", False)),
        }
    if strategy == "RSI_Scalp":
        return {
            "rsi_period": params.get("rsi_period"),
            "rsi_os":     params.get("os"),
            "ema_period": params.get("trend_ema", 0),
            "tp_pct":     params.get("tp"),
            "sl_pct":     params.get("sl"),
        }
    if strategy == "BB_RSI":
        th = float(params.get("bbp_th", 0.10))
        return {
            "bb_period":           params.get("bb_period"),
            "bb_std":              params.get("bb_std"),
            "bbp_long_threshold":  th,
            "bbp_short_threshold": round(1 - th, 4),
            "rsi_period":          params.get("rsi_period"),
            "rsi_os":              params.get("rsi_os"),
            "ema_period":          params.get("trend_ema", 0),
            "tp_pct":              params.get("tp"),
            "sl_pct":              params.get("sl"),
            "bb_mid_exit":         bool(params.get("bb_mid_exit", False)),
        }
    if strategy == "MACD_Cross":
        return {
            "macd_fast":   params.get("macd_fast"),
            "macd_slow":   params.get("macd_slow"),
            "macd_signal": params.get("macd_sig"),
            "ema_trend":   params.get("trend_ema", 0),
            "tp_pct":      params.get("tp"),
            "sl_pct":      params.get("sl"),
        }
    if strategy == "Williams_R":
        return {
            "wr_period": params.get("wr_period"),
            "wr_os":     params.get("os"),
            "ema_period": params.get("trend_ema", 0),
            "tp_pct":    params.get("tp"),
            "sl_pct":    params.get("sl"),
        }
    return {}


_METRIC_KEYS = {"trades", "wr", "pf", "roi", "tpd", "max_dd", "approved"}


def apply_result(asset: str, strategy: str, params: dict, tag: str | None = None,
                 timeframe: str = "5m") -> dict:
    """Apply scanner result params to the matching live strategy instance.
    - timeframe: 5m/15m/30m/1h — sempre vai no nome da instância dinâmica
    - tag opcional: cria versão nomeada `{prefix}_{asset}_{tf}_{tag_slug}`
    - sem tag: instância `{prefix}_{asset}_{tf}` (atualiza/sobrescreve esse TF)
    - Cria instância dinâmica se não houver registrada
    - Salva params traduzidos (inclui timeframe) em strategy.{name}.params
    - Salva métricas + raw scanner params em strategy.{name}.scanner_metrics (inclui tf e tag)
    - Auto-ativa (enabled=true)
    """
    import json as _json
    from datetime import datetime, timezone
    from bot import db
    from bot.strategies import manager

    asset_u = asset.upper()
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {"error": f"Timeframe inválido: {timeframe}"}
    log.info(f"[scanner] apply_result chamado: strategy={strategy!r} asset={asset_u!r} tf={timeframe} tag={tag!r}")
    # Sempre cria/atualiza instância dinâmica com TF no nome.
    # Legado: instâncias hardcoded sem TF (bb_stoch_btc, etc) NÃO são tocadas — novos applies criam bb_stoch_btc_5m.
    instance = manager.register_dynamic_instance(strategy, asset_u, tag=tag, timeframe=timeframe)
    if not instance:
        log.error(f"[scanner] apply_result: register_dynamic_instance retornou None para ({strategy!r}, {asset_u!r}, tf={timeframe}, tag={tag!r})")
        return {"error": f"Estratégia desconhecida: {strategy}"}

    translated = _translate_params(strategy, params)
    if not translated:
        return {"error": "Não foi possível traduzir os parâmetros"}

    translated["assets"] = [asset_u]
    translated["timeframe"] = timeframe   # estratégia live usa esse param para escolher o df

    existing = db.get_strategy_config(instance)
    merged = {**(existing.get("params") or {}), **translated}

    # Separa métricas dos params raw do scanner (para exibir no card)
    scanner_params = {k: v for k, v in params.items() if k not in _METRIC_KEYS and k != "strategy" and k != "tf"}
    scanner_metrics = {
        "strategy":       strategy,
        "asset":          asset_u,
        "timeframe":      timeframe,
        "tag":            (tag or "").strip() or None,
        "applied_at":     datetime.now(timezone.utc).isoformat(),
        "scanner_params": scanner_params,
        "trades":         params.get("trades"),
        "wr":             params.get("wr"),
        "pf":             params.get("pf"),
        "roi":            params.get("roi"),
        "tpd":            params.get("tpd"),
        "max_dd":         params.get("max_dd"),
    }

    db.set_configs({
        f"strategy.{instance}.params":          _json.dumps(merged),
        f"strategy.{instance}.scanner_metrics": _json.dumps(scanner_metrics),
        f"strategy.{instance}.enabled":         "true",
    })
    log.info(f"[scanner] Aplicado {strategy}/{asset_u} → {instance} (auto-enabled)")
    return {"ok": True, "instance": instance, "params": translated, "metrics": scanner_metrics}


# ── Job management ─────────────────────────────────────────────────────────

def start_scan_job(asset: str, days: int = 90,
                   strategies: list[str] | None = None,
                   timeframe: str = "5m") -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "progress": "Iniciando...",
                         "result": None, "error": None}

    def _run():
        try:
            def cb(msg: str):
                with _jobs_lock:
                    if job_id in _jobs:
                        _jobs[job_id]["progress"] = msg

            result = run_scan(asset, days, strategies, progress_cb=cb, timeframe=timeframe)
            with _jobs_lock:
                if "error" in result:
                    _jobs[job_id].update(status="error", error=result["error"])
                else:
                    _jobs[job_id].update(status="done", result=result)
        except Exception as exc:
            log.error(f"[scanner] Job {job_id} failed: {exc}")
            with _jobs_lock:
                _jobs[job_id].update(status="error", error=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def get_scan_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)
