"""
Live filters — ADX regime, UTC session and ATR-based TP/SL overrides.

These mirror the filters from scanner_v2.py so combos approved by the scanner
execute with the same gating in production. All filters are opt-in via params
with safe defaults (= filter disabled).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger

log = get_logger("strategies.live_filters")


def passes_session(params: dict, ts_ms_curr: int) -> bool:
    """True se o candle atual está na sessão UTC permitida.

    params['session_start'] e params['session_end'] são horas inteiras UTC.
    (0, 24) = filtro desligado.
    """
    s = int(params.get("session_start", 0))
    e = int(params.get("session_end", 24))
    if s == 0 and e == 24:
        return True
    hour = int((int(ts_ms_curr) // 3_600_000) % 24)
    return s <= hour < e


def passes_adx(params: dict, df: pd.DataFrame, is_trend_strategy: bool) -> bool:
    """True se ADX está na zona certa pra essa família.

    Mean reversion (is_trend_strategy=False): ADX < adx_min (só ranging).
    Trend following (is_trend_strategy=True):  ADX >= adx_min (só trending).

    params['adx_period'] = 0 desliga o filtro (deixa passar).
    ADX NaN (warmup) também deixa passar — evita bloquear entradas válidas
    nos primeiros candles após restart.
    """
    p = int(params.get("adx_period", 0))
    if p <= 0:
        return True
    m = float(params.get("adx_min", 0))
    try:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=p)
    except Exception as exc:
        log.debug(f"adx compute failed: {exc}")
        return True
    if adx_df is None or adx_df.empty:
        return True
    col = [c for c in adx_df.columns if c.startswith("ADX_")]
    if not col:
        return True
    val = adx_df[col[0]].iloc[-1]
    if pd.isna(val):
        return True
    val = float(val)
    return (val >= m) if is_trend_strategy else (val < m)


def apply_atr_tp_sl(params: dict, df: pd.DataFrame, signal: dict) -> dict:
    """Quando atr_tp_mode=True, sobrescreve tp_pct/sl_pct com ATR*mult/close.

    atr_tp_mode aceita bool ou string ('true'/'1'/'yes') para sobreviver a
    valores vindos do DB. Sem signal['signal_price'] (close do candle), usa
    df['close'].iloc[-1] como base.
    """
    mode_raw = params.get("atr_tp_mode", False)
    if isinstance(mode_raw, str):
        atr_mode = mode_raw.lower() in ("true", "1", "yes")
    else:
        atr_mode = bool(mode_raw)
    if not atr_mode:
        return signal

    period = int(params.get("atr_period", 14))
    try:
        atr_series = ta.atr(df["high"], df["low"], df["close"], length=period)
    except Exception as exc:
        log.debug(f"atr compute failed: {exc}")
        return signal
    if atr_series is None or pd.isna(atr_series.iloc[-1]):
        return signal

    atr_val = float(atr_series.iloc[-1])
    base_price = float(signal.get("signal_price") or df["close"].iloc[-1])
    if base_price <= 0:
        return signal
    atr_pct = atr_val / base_price  # já em fração (0.005 = 0.5%)

    tp_mult = float(params.get("atr_tp_mult", 1.0))
    sl_mult = float(params.get("atr_sl_mult", 1.0))
    signal["tp_pct"] = atr_pct * tp_mult
    signal["sl_pct"] = atr_pct * sl_mult
    return signal


def apply_live_filters(params: dict, df: pd.DataFrame,
                       signal: dict | None,
                       is_trend_strategy: bool = False) -> dict | None:
    """Wrapper: aplica session + adx + atr na ordem. Retorna signal modificado
    ou None se algum filtro bloqueou.

    Estratégias devem chamar isso no lugar de cada `return signal_dict` no
    evaluate(). Família trend (EMA_Cross, MACD_Cross) passa is_trend_strategy=True;
    todas as outras passam False (mean reversion).
    """
    if signal is None:
        return None
    if "timestamp" in df.columns:
        ts_ms = int(df["timestamp"].iloc[-1])
    elif "ts_ms" in df.columns:
        ts_ms = int(df["ts_ms"].iloc[-1])
    else:
        ts_ms = 0  # sem timestamp, session filter vira no-op (passes_session retorna True)
    if not passes_session(params, ts_ms):
        log.signals(f"[{signal.get('asset')}] live filter blocked: SESSION (hour outside {params.get('session_start')}-{params.get('session_end')})")
        return None
    if not passes_adx(params, df, is_trend_strategy):
        log.signals(f"[{signal.get('asset')}] live filter blocked: ADX (period={params.get('adx_period')} min={params.get('adx_min')} trend={is_trend_strategy})")
        return None
    return apply_atr_tp_sl(params, df, signal)
