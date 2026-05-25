"""
Vectorized backtest engine for RazorHL strategies.

Loads historical OHLCV from local CSV files in the candles/ folder
(via bot.backtest.csv_loader), precomputes indicators once over the full
series with pandas_ta, derives signals as numpy boolean masks, and simulates
trades by finding first TP/SL/BB-mid hit via numpy.argmax over boolean slices.

Per-candle exit priority: SL > TP > BB-mid (pessimistic, no tie-break).

Public API: start_backtest_job(strategy, asset, days, trade_size_usd, fee_rate)
            get_job(job_id)
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta

from bot.backtest.csv_loader import _load_candles_csv, _update_csv
from bot.backtest.report import compute_metrics
from bot.logger import get_logger

log = get_logger("backtest.engine")

_jobs: dict = {}
_jobs_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────

def _apply_ema_filter(sig_long: np.ndarray, sig_short: np.ndarray,
                     close: np.ndarray, ema: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    if ema is None:
        return sig_long, sig_short
    valid = ~np.isnan(ema)
    return sig_long & valid & (close > ema), sig_short & valid & (close < ema)


def _add_pnl(raw_trades: list[dict], trade_size_usd: float, fee_rate: float) -> list[dict]:
    """Identical to engine._add_pnl — duplicated to keep the modules decoupled."""
    result = []
    for t in raw_trades:
        ep = t["entry_price"]
        xp = t["exit_price"]
        size = trade_size_usd / ep
        gross = (xp - ep) * size if t["side"] == "long" else (ep - xp) * size
        fees = trade_size_usd * fee_rate
        pnl = gross - fees
        result.append({**t, "pnl": round(pnl, 4), "pnl_pct": round(pnl / trade_size_usd * 100, 4)})
    return result


# ── Public API ─────────────────────────────────────────────────────────────

def start_backtest_job(strategy_name: str, asset: str, days: int,
                       trade_size_usd: float, fee_rate: float) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": "Na fila...",
            "strategy": strategy_name,
            "asset": asset,
            "days": days,
            "result": None,
            "error": None,
            "elapsed_s": None,
        }
    t = threading.Thread(
        target=_run_job,
        args=(job_id, strategy_name, asset, days, trade_size_usd, fee_rate),
        daemon=True,
    )
    t.start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _run_job(job_id: str, strategy_name: str, asset: str, days: int,
             trade_size_usd: float, fee_rate: float):
    started = time.time()
    try:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["progress"] = "Iniciando..."

        result = _run_backtest(strategy_name, asset, days, trade_size_usd, fee_rate,
                                    progress_cb=lambda m: _set_progress(job_id, m))
        elapsed = round(time.time() - started, 3)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["progress"] = f"Concluído em {elapsed}s"
            _jobs[job_id]["result"] = result
            _jobs[job_id]["elapsed_s"] = elapsed
    except Exception as e:
        log.error(f"[backtest-fast job {job_id}] {e}", exc_info=True)
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            _jobs[job_id]["progress"] = f"Erro: {e}"
            _jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)


def _set_progress(job_id: str, msg: str):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["progress"] = msg


# ── Indicator helpers ──────────────────────────────────────────────────────

def _ema(close_s: pd.Series, period: int) -> np.ndarray | None:
    if period <= 0:
        return None
    return ta.ema(close_s, length=period).values.astype(float)


def _bb_arrays(close_s: pd.Series, close: np.ndarray, period: int, std: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (BBP, BBM) arrays."""
    bb = ta.bbands(close_s, length=period, std=std)
    bbu = bb[[c for c in bb.columns if c.startswith("BBU_")][0]].values.astype(float)
    bbl = bb[[c for c in bb.columns if c.startswith("BBL_")][0]].values.astype(float)
    bbm = bb[[c for c in bb.columns if c.startswith("BBM_")][0]].values.astype(float)
    span = bbu - bbl
    with np.errstate(invalid="ignore", divide="ignore"):
        bbp = np.where(span > 0, (close - bbl) / span, np.nan)
    return bbp, bbm


def _stoch_arrays(high_s, low_s, close_s, k: int, d: int) -> tuple[np.ndarray, np.ndarray]:
    df = ta.stoch(high_s, low_s, close_s, k=k, d=d, smooth_k=3)
    K = df[[c for c in df.columns if c.startswith("STOCHk_")][0]].values.astype(float)
    D = df[[c for c in df.columns if c.startswith("STOCHd_")][0]].values.astype(float)
    return K, D


# ── Signal functions per family ────────────────────────────────────────────

def _signals_bb_stoch(close, high, low, close_s, high_s, low_s, params):
    bb_period   = int(params["bb_period"])
    bb_std      = float(params["bb_std"])
    stoch_k     = int(params["stoch_k"])
    stoch_d     = int(params["stoch_d"])
    stoch_long  = float(params["stoch_long"])    # oversold threshold
    stoch_short = float(params["stoch_short"])   # overbought threshold
    bbp_long_th  = float(params["bbp_long_threshold"])
    bbp_short_th = float(params["bbp_short_threshold"])
    ema_period   = int(params.get("ema_period", 0))
    _bme = params.get("bb_mid_exit", True)
    bb_mid_exit = str(_bme).lower() not in ("false", "0", "no")

    BBP, BBM = _bb_arrays(close_s, close, bb_period, bb_std)
    K, D = _stoch_arrays(high_s, low_s, close_s, stoch_k, stoch_d)
    ema = _ema(close_s, ema_period)

    valid = ~np.isnan(BBP) & ~np.isnan(K) & ~np.isnan(D)
    sig_long  = valid & (BBP < bbp_long_th)  & (K < stoch_long)  & (D < stoch_long)
    sig_short = valid & (BBP > bbp_short_th) & (K > stoch_short) & (D > stoch_short)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)

    bb_mid_out = BBM if bb_mid_exit else None
    return sig_long, sig_short, bb_mid_out, None


# ── Family dispatch ────────────────────────────────────────────────────────

_FAMILY_FNS: dict = {}  # populated below as families are added


def _resolve_family(strategy_name: str) -> str:
    # Longest prefix match (so "bb_reversion" beats "bb_")
    for fam in sorted(_FAMILY_FNS.keys(), key=len, reverse=True):
        if strategy_name.startswith(fam):
            return fam
    raise ValueError(f"No fast family for strategy: {strategy_name}")


def _resolve_strategy_instance(strategy_name: str, asset: str) -> str:
    """Mirror engine._run_backtest's logic for resolving generic → specific name."""
    from bot.strategies.manager import STRATEGY_MAP
    if strategy_name in STRATEGY_MAP:
        return strategy_name
    candidates = [(n, s) for n, s in STRATEGY_MAP.items() if n.startswith(strategy_name + "_")]
    if not candidates:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    asset_matches = [(n, s) for n, s in candidates
                     if asset.upper() in (s.DEFAULT_PARAMS.get("assets") or [])]
    resolved, _ = asset_matches[0] if asset_matches else candidates[0]
    return resolved


def _run_backtest(strategy_name, asset, days, trade_size_usd, fee_rate, progress_cb=None):
    from bot import db as bot_db
    from bot.strategies.manager import STRATEGY_MAP

    strategy_name = _resolve_strategy_instance(strategy_name, asset)
    strategy = STRATEGY_MAP[strategy_name]
    family = _resolve_family(strategy_name)
    fn = _FAMILY_FNS[family]

    params = {**strategy.DEFAULT_PARAMS, **bot_db.get_strategy_config(strategy_name)["params"]}

    if progress_cb: progress_cb("Atualizando CSV...")
    _update_csv(asset, progress_cb)

    if progress_cb: progress_cb("Carregando candles...")
    df = _load_candles_csv(asset, "5m", days=None)
    if df.empty:
        raise ValueError(f"No 5m candles available for {asset}")

    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    ts    = df["timestamp"].values.astype(np.int64)
    close_s = pd.Series(close)
    high_s  = pd.Series(high)
    low_s   = pd.Series(low)

    if progress_cb: progress_cb(f"Computando sinais ({family})...")
    sig_long, sig_short, bb_mid, sl_dist = fn(close, high, low, close_s, high_s, low_s, params)

    tp_pct = float(params["tp_pct"])
    sl_pct = float(params["sl_pct"])

    if progress_cb: progress_cb("Simulando trades...")
    raw_trades = _simulate_fast(
        sig_long, sig_short, close, high, low, ts,
        tp_pct=tp_pct, sl_pct=sl_pct,
        bb_mid=bb_mid, sl_dist=sl_dist,
        strategy_name=strategy_name,
    )

    # Filter by requested period
    now_ms = int(time.time() * 1000)
    cutoff_iso = datetime.fromtimestamp((now_ms - days * 86_400_000) / 1000, tz=timezone.utc).isoformat()
    filtered = [t for t in raw_trades if t["entry_time"] >= cutoff_iso]

    if progress_cb: progress_cb(f"Calculando métricas ({len(filtered)} trades)...")
    trades_with_pnl = _add_pnl(filtered, trade_size_usd, fee_rate)
    metrics = compute_metrics(trades_with_pnl, initial_capital=trade_size_usd)

    return {
        "trades": trades_with_pnl,
        "metrics": metrics,
        "strategy_resolved": strategy_name,
    }


# Register bb_stoch family (other families appended in later tasks)
_FAMILY_FNS["bb_stoch"] = _signals_bb_stoch


def _signals_bb_reversion(close, high, low, close_s, high_s, low_s, params):
    bb_period  = int(params["bb_period"])
    bb_std     = float(params["bb_std"])
    bbp_long_th  = float(params["bbp_long_threshold"])
    bbp_short_th = float(params["bbp_short_threshold"])
    ema_period   = int(params.get("ema_period", 0))
    rsi_long_max  = float(params.get("rsi_long_max", 100))
    rsi_short_min = float(params.get("rsi_short_min", 0))
    _bme = params.get("bb_mid_exit", True)
    bb_mid_exit = str(_bme).lower() not in ("false", "0", "no")

    BBP, BBM = _bb_arrays(close_s, close, bb_period, bb_std)
    # BBU/BBL needed for the close-within-band check
    bb = ta.bbands(close_s, length=bb_period, std=bb_std)
    BBU = bb[[c for c in bb.columns if c.startswith("BBU_")][0]].values.astype(float)
    BBL = bb[[c for c in bb.columns if c.startswith("BBL_")][0]].values.astype(float)

    ema = _ema(close_s, ema_period)
    rsi = ta.rsi(close_s, length=14).values.astype(float)

    BBP_prev = np.roll(BBP, 1); BBP_prev[0] = np.nan
    valid = ~np.isnan(BBP_prev) & ~np.isnan(BBM) & ~np.isnan(rsi)

    sig_long  = (valid
                 & (BBP_prev < bbp_long_th)
                 & (close > BBL) & (close < BBM)
                 & (rsi < rsi_long_max))
    sig_short = (valid
                 & (BBP_prev > bbp_short_th)
                 & (close < BBU) & (close > BBM)
                 & (rsi > rsi_short_min))
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)

    return sig_long, sig_short, (BBM if bb_mid_exit else None), None


_FAMILY_FNS["bb_reversion"] = _signals_bb_reversion


def _signals_stoch_scalp(close, high, low, close_s, high_s, low_s, params):
    stoch_k    = int(params["stoch_k"])
    stoch_d    = int(params["stoch_d"])
    stoch_os   = float(params["stoch_os"])
    stoch_ob   = 100.0 - stoch_os
    ema_period = int(params.get("ema_period", 0))

    K, D = _stoch_arrays(high_s, low_s, close_s, stoch_k, stoch_d)
    ema = _ema(close_s, ema_period)

    pK = np.roll(K, 1); pK[0] = np.nan
    pD = np.roll(D, 1); pD[0] = np.nan
    valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(pK) & ~np.isnan(pD)

    sig_long  = valid & (pK < stoch_os) & (pD < stoch_os) & (K > D) & (pK <= pD)
    sig_short = valid & (pK > stoch_ob) & (pD > stoch_ob) & (K < D) & (pK >= pD)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, None, None


_FAMILY_FNS["stoch_scalp"] = _signals_stoch_scalp


def _signals_ema_cross(close, high, low, close_s, high_s, low_s, params):
    ema_fast   = int(params["ema_fast"])
    ema_slow   = int(params["ema_slow"])
    ema_trend  = int(params.get("ema_trend", 0))
    _uas = params.get("use_atr_sl", False)
    use_atr_sl = str(_uas).lower() not in ("false", "0", "no")
    atr_period = int(params.get("atr_period", 14))
    atr_mult   = float(params.get("atr_mult", 1.0))

    FAST = _ema(close_s, ema_fast)
    SLOW = _ema(close_s, ema_slow)
    TREND = _ema(close_s, ema_trend) if ema_trend > 0 else None

    pFAST = np.roll(FAST, 1); pFAST[0] = np.nan
    pSLOW = np.roll(SLOW, 1); pSLOW[0] = np.nan
    valid = ~np.isnan(FAST) & ~np.isnan(SLOW) & ~np.isnan(pFAST) & ~np.isnan(pSLOW)

    sig_long  = valid & (FAST > SLOW) & (pFAST <= pSLOW)
    sig_short = valid & (FAST < SLOW) & (pFAST >= pSLOW)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, TREND)

    sl_dist = None
    if use_atr_sl:
        atr = ta.atr(high_s, low_s, close_s, length=atr_period).values.astype(float)
        sl_dist = atr * atr_mult

    return sig_long, sig_short, None, sl_dist


_FAMILY_FNS["ema_cross"] = _signals_ema_cross


def _signals_rsi_scalp(close, high, low, close_s, high_s, low_s, params):
    rsi_period = int(params["rsi_period"])
    rsi_os     = float(params["rsi_os"])
    rsi_ob     = 100.0 - rsi_os
    ema_period = int(params.get("ema_period", 0))

    RSI = ta.rsi(close_s, length=rsi_period).values.astype(float)
    ema = _ema(close_s, ema_period)
    pRSI = np.roll(RSI, 1); pRSI[0] = np.nan
    valid = ~np.isnan(RSI) & ~np.isnan(pRSI)

    sig_long  = valid & (pRSI < rsi_os) & (RSI >= rsi_os)
    sig_short = valid & (pRSI > rsi_ob) & (RSI <= rsi_ob)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, None, None


_FAMILY_FNS["rsi_scalp"] = _signals_rsi_scalp


def _signals_bb_rsi(close, high, low, close_s, high_s, low_s, params):
    bb_period   = int(params["bb_period"])
    bb_std      = float(params["bb_std"])
    bbp_long_th  = float(params["bbp_long_threshold"])
    bbp_short_th = float(params["bbp_short_threshold"])
    rsi_period   = int(params["rsi_period"])
    rsi_os       = float(params["rsi_os"])
    rsi_ob       = 100.0 - rsi_os
    ema_period   = int(params.get("ema_period", 0))
    _bme = params.get("bb_mid_exit", False)
    bb_mid_exit = str(_bme).lower() not in ("false", "0", "no")

    BBP, BBM = _bb_arrays(close_s, close, bb_period, bb_std)
    RSI = ta.rsi(close_s, length=rsi_period).values.astype(float)
    ema = _ema(close_s, ema_period)

    valid = ~np.isnan(BBP) & ~np.isnan(RSI)
    sig_long  = valid & (BBP < bbp_long_th)  & (RSI < rsi_os)
    sig_short = valid & (BBP > bbp_short_th) & (RSI > rsi_ob)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, (BBM if bb_mid_exit else None), None


_FAMILY_FNS["bb_rsi"] = _signals_bb_rsi


def _signals_macd_cross(close, high, low, close_s, high_s, low_s, params):
    fast = int(params["macd_fast"])
    slow = int(params["macd_slow"])
    sig  = int(params["macd_signal"])
    ema_trend = int(params.get("ema_trend", 0))

    df = ta.macd(close_s, fast=fast, slow=slow, signal=sig)
    MACD = df[[c for c in df.columns if c.startswith("MACD_")][0]].values.astype(float)
    SIG  = df[[c for c in df.columns if c.startswith("MACDs_")][0]].values.astype(float)
    trend = _ema(close_s, ema_trend) if ema_trend > 0 else None

    pM = np.roll(MACD, 1); pM[0] = np.nan
    pS = np.roll(SIG, 1);  pS[0] = np.nan
    valid = ~np.isnan(MACD) & ~np.isnan(SIG) & ~np.isnan(pM) & ~np.isnan(pS)

    sig_long  = valid & (MACD > SIG) & (pM <= pS)
    sig_short = valid & (MACD < SIG) & (pM >= pS)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, trend)
    return sig_long, sig_short, None, None


_FAMILY_FNS["macd_cross"] = _signals_macd_cross


def _signals_williams_r(close, high, low, close_s, high_s, low_s, params):
    wr_period  = int(params["wr_period"])
    wr_os      = float(params["wr_os"])           # negative, e.g. -80
    wr_ob      = wr_os + 100.0                    # e.g. -20
    ema_period = int(params.get("ema_period", 0))

    WR = ta.willr(high_s, low_s, close_s, length=wr_period).values.astype(float)
    ema = _ema(close_s, ema_period)
    pWR = np.roll(WR, 1); pWR[0] = np.nan
    valid = ~np.isnan(WR) & ~np.isnan(pWR)

    sig_long  = valid & (pWR < wr_os) & (WR >= wr_os)
    sig_short = valid & (pWR > wr_ob) & (WR <= wr_ob)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, None, None


_FAMILY_FNS["williams_r"] = _signals_williams_r


# ── Simulation ─────────────────────────────────────────────────────────────

def _first_true(mask: np.ndarray) -> int | None:
    """Index of first True, or None if no True. np.argmax returns 0 either way,
    so we must check .any() explicitly."""
    if not mask.any():
        return None
    return int(np.argmax(mask))


def _simulate_fast(
    sig_long: np.ndarray,
    sig_short: np.ndarray,
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    ts: np.ndarray,
    tp_pct: float,
    sl_pct: float,
    bb_mid: np.ndarray | None = None,
    sl_dist: np.ndarray | None = None,
    strategy_name: str = "",
) -> list[dict]:
    """
    Walk signal-by-signal. For each entry, find first TP, SL, and (optionally)
    BB-mid hit via numpy.argmax. Pick earliest outcome. No overlap between trades.
    Trades where neither TP nor SL ever hits are discarded (matches engine.py).

    bb_mid: float array of BB midline values, or None to skip BB mid exit.
    sl_dist: per-candle absolute SL distance in price (for ATR-based SL),
             or None to use sl_pct.
    """
    trades: list[dict] = []
    N = len(close)
    i = 0
    while i < N - 1:
        is_long = bool(sig_long[i])
        is_short = bool(sig_short[i])
        if not is_long and not is_short:
            i += 1
            continue
        side = "long" if is_long else "short"  # long takes precedence
        entry = float(close[i])

        if sl_dist is not None and not np.isnan(sl_dist[i]):
            sl_abs = float(sl_dist[i])
        else:
            sl_abs = entry * sl_pct / 100.0
        tp_abs = entry * tp_pct / 100.0

        if side == "long":
            tp = entry + tp_abs
            sl = entry - sl_abs
        else:
            tp = entry - tp_abs
            sl = entry + sl_abs

        # Slices starting at i+1
        h = high[i + 1:]
        lo = low[i + 1:]
        c = close[i + 1:]

        if side == "long":
            tp_mask = h >= tp
            sl_mask = lo <= sl
        else:
            tp_mask = lo <= tp
            sl_mask = h >= sl

        # Build mid_mask if bb_mid exit is enabled
        if bb_mid is not None:
            mid_slice = bb_mid[i + 1:]
            with np.errstate(invalid="ignore"):
                if side == "long":
                    mid_mask = (~np.isnan(mid_slice)) & (c >= mid_slice)
                else:
                    mid_mask = (~np.isnan(mid_slice)) & (c <= mid_slice)
        else:
            mid_mask = np.zeros_like(sl_mask)

        # First candle where ANY exit fires
        any_mask = sl_mask | tp_mask | mid_mask
        if not any_mask.any():
            i += 1
            continue
        j_rel = int(np.argmax(any_mask))

        # Per-candle priority: SL > TP > BB mid (matches engine._simulate_trade)
        if sl_mask[j_rel]:
            outcome_label = "sl"
            exit_price = sl
        elif tp_mask[j_rel]:
            outcome_label = "tp"
            exit_price = tp
        else:
            outcome_label = "bb_mid"
            exit_price = float(c[j_rel])

        j_abs = i + 1 + j_rel
        entry_ts = int(ts[i])
        exit_ts = int(ts[j_abs])

        trades.append({
            "entry_time": datetime.fromtimestamp(entry_ts / 1000, tz=timezone.utc).isoformat(),
            "exit_time":  datetime.fromtimestamp(exit_ts / 1000, tz=timezone.utc).isoformat(),
            "side":       side,
            "entry_price": round(entry, 4),
            "exit_price":  round(exit_price, 4),
            "tp":          round(tp, 4),
            "sl":          round(sl, 4),
            "outcome":     outcome_label,
            "candles_held": j_rel + 1,
            "strategy":    strategy_name,
        })
        i = j_abs + 1

    return trades
