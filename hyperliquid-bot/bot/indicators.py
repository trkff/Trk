"""
Technical indicators calculated at each candle close using pandas-ta.
- EMA 9 & EMA 21 on 5m timeframe
- RSI 2 on 1m timeframe
- Volume average 20 periods on 1m
- ATR 14 on 1m
- VWAP on 1m (optional, resets daily UTC, None if < 10 candles today)
- StochRSI K/D on 1m (optional, None if NaN)
"""

import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger

log = get_logger("indicators")


def calc_ema(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    return ta.ema(df[col], length=period)


def calc_rsi(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    return ta.rsi(df[col], length=period)


def calc_atr(df: pd.DataFrame, period: int) -> pd.Series:
    return ta.atr(df["high"], df["low"], df["close"], length=period)


def calc_volume_avg(df: pd.DataFrame, period: int) -> pd.Series:
    return ta.sma(df["volume"], length=period)


def compute_all(df_1m: pd.DataFrame, df_5m: pd.DataFrame, cfg: dict) -> dict | None:
    """
    Compute all indicators from the latest candle data.
    Returns a dict with indicator values or None if insufficient data.
    VWAP and StochRSI are optional — set to None if NaN, do not block.
    """
    ema_fast = int(cfg.get("ema_fast", 9))
    ema_slow = int(cfg.get("ema_slow", 21))
    rsi_period = int(cfg.get("rsi_period", 2))
    atr_period = int(cfg.get("atr_period", 14))
    vol_period = int(cfg.get("volume_avg_period", 20))
    stochrsi_period = int(cfg.get("stochrsi_period", 14))

    # Need enough data
    if len(df_5m) < ema_slow + 2:
        log.debug(f"Not enough 5m candles ({len(df_5m)}/{ema_slow + 2})")
        return None
    if len(df_1m) < max(atr_period, vol_period, rsi_period) + 2:
        log.debug(f"Not enough 1m candles ({len(df_1m)})")
        return None

    # 5m EMAs
    ema9 = calc_ema(df_5m, ema_fast)
    ema21 = calc_ema(df_5m, ema_slow)

    # 1m indicators
    rsi2 = calc_rsi(df_1m, rsi_period)
    atr = calc_atr(df_1m, atr_period)
    vol_avg = calc_volume_avg(df_1m, vol_period)

    # Get latest mandatory values
    ema9_val = ema9.iloc[-1] if not pd.isna(ema9.iloc[-1]) else None
    ema21_val = ema21.iloc[-1] if not pd.isna(ema21.iloc[-1]) else None
    rsi2_val = rsi2.iloc[-1] if not pd.isna(rsi2.iloc[-1]) else None
    atr_val = atr.iloc[-1] if not pd.isna(atr.iloc[-1]) else None
    vol_current = df_1m["volume"].iloc[-1]
    vol_avg_val = vol_avg.iloc[-1] if not pd.isna(vol_avg.iloc[-1]) else None

    if any(v is None for v in [ema9_val, ema21_val, rsi2_val, atr_val, vol_avg_val]):
        log.debug("Some mandatory indicators are NaN — skipping")
        return None

    # Optional: VWAP 1m (reset daily — filter to today UTC)
    vwap_val = None
    try:
        today = pd.Timestamp.now(tz="UTC").normalize()
        df_today = df_1m[df_1m.index >= today]
        if len(df_today) >= 10:
            vwap_series = ta.vwap(df_today["high"], df_today["low"], df_today["close"], df_today["volume"])
            if vwap_series is not None and not vwap_series.empty:
                last = vwap_series.iloc[-1]
                if not pd.isna(last):
                    vwap_val = round(float(last), 4)
    except Exception as e:
        log.debug(f"VWAP 1m computation failed: {e}")

    # Optional: VWAP 5m (reset daily — filter to today UTC)
    vwap_5m_val = None
    try:
        today = pd.Timestamp.now(tz="UTC").normalize()
        df_5m_today = df_5m[df_5m.index >= today]
        if len(df_5m_today) >= 5:
            vwap_5m_series = ta.vwap(
                df_5m_today["high"], df_5m_today["low"],
                df_5m_today["close"], df_5m_today["volume"]
            )
            if vwap_5m_series is not None and not vwap_5m_series.empty:
                last = vwap_5m_series.iloc[-1]
                if not pd.isna(last):
                    vwap_5m_val = round(float(last), 4)
    except Exception as e:
        log.debug(f"VWAP 5m computation failed: {e}")

    # Optional: StochRSI 1m
    stochrsi_k_val = stochrsi_d_val = None
    stochrsi_k_prev = stochrsi_d_prev = None
    try:
        srsi = ta.stochrsi(df_1m["close"], length=stochrsi_period)
        if srsi is not None and not srsi.empty and len(srsi) >= 2:
            k_col = [c for c in srsi.columns if c.startswith("STOCHRSIk")]
            d_col = [c for c in srsi.columns if c.startswith("STOCHRSId")]
            if k_col and d_col:
                k_series = srsi[k_col[0]]
                d_series = srsi[d_col[0]]
                if not pd.isna(k_series.iloc[-1]) and not pd.isna(d_series.iloc[-1]):
                    stochrsi_k_val = round(float(k_series.iloc[-1]), 4)
                    stochrsi_d_val = round(float(d_series.iloc[-1]), 4)
                    if not pd.isna(k_series.iloc[-2]) and not pd.isna(d_series.iloc[-2]):
                        stochrsi_k_prev = round(float(k_series.iloc[-2]), 4)
                        stochrsi_d_prev = round(float(d_series.iloc[-2]), 4)
            else:
                log.warning(f"StochRSI 1m columns not found: {list(srsi.columns)}")
    except Exception as e:
        log.debug(f"StochRSI 1m computation failed: {e}")

    # Optional: StochRSI 5m
    stochrsi_k_5m_val = stochrsi_d_5m_val = None
    stochrsi_k_prev_5m = stochrsi_d_prev_5m = None
    try:
        srsi_5m = ta.stochrsi(df_5m["close"], length=stochrsi_period)
        if srsi_5m is not None and not srsi_5m.empty and len(srsi_5m) >= 2:
            k_col = [c for c in srsi_5m.columns if c.startswith("STOCHRSIk")]
            d_col = [c for c in srsi_5m.columns if c.startswith("STOCHRSId")]
            if k_col and d_col:
                k_series = srsi_5m[k_col[0]]
                d_series = srsi_5m[d_col[0]]
                if not pd.isna(k_series.iloc[-1]) and not pd.isna(d_series.iloc[-1]):
                    stochrsi_k_5m_val = round(float(k_series.iloc[-1]), 4)
                    stochrsi_d_5m_val = round(float(d_series.iloc[-1]), 4)
                    if not pd.isna(k_series.iloc[-2]) and not pd.isna(d_series.iloc[-2]):
                        stochrsi_k_prev_5m = round(float(k_series.iloc[-2]), 4)
                        stochrsi_d_prev_5m = round(float(d_series.iloc[-2]), 4)
    except Exception as e:
        log.debug(f"StochRSI 5m computation failed: {e}")

    # 5m indicators (used by várias estratégias 5m)
    rsi2_5m_series = calc_rsi(df_5m, rsi_period)
    atr_5m_series = calc_atr(df_5m, atr_period)
    vol_avg_5m_series = calc_volume_avg(df_5m, vol_period)

    rsi2_5m_val = rsi2_5m_series.iloc[-1] if not pd.isna(rsi2_5m_series.iloc[-1]) else None
    atr_5m_val = atr_5m_series.iloc[-1] if not pd.isna(atr_5m_series.iloc[-1]) else None
    vol_avg_5m_val = vol_avg_5m_series.iloc[-1] if not pd.isna(vol_avg_5m_series.iloc[-1]) else None
    vol_5m_current = float(df_5m["volume"].iloc[-1])

    result = {
        "ema9": round(float(ema9_val), 4),
        "ema21": round(float(ema21_val), 4),
        "rsi2": round(float(rsi2_val), 2),
        "atr": round(float(atr_val), 4),
        "volume": round(float(vol_current), 4),
        "volume_avg": round(float(vol_avg_val), 4),
        "close_1m": round(float(df_1m["close"].iloc[-1]), 4),
        "close_5m": round(float(df_5m["close"].iloc[-1]), 4),
        # 5m scalp indicators
        "rsi2_5m": round(float(rsi2_5m_val), 2) if rsi2_5m_val is not None else None,
        "atr_5m": round(float(atr_5m_val), 4) if atr_5m_val is not None else None,
        "volume_5m": round(vol_5m_current, 4),
        "volume_avg_5m": round(float(vol_avg_5m_val), 4) if vol_avg_5m_val is not None else None,
        # Optional 1m
        "vwap": vwap_val,
        "stochrsi_k": stochrsi_k_val,
        "stochrsi_d": stochrsi_d_val,
        "stochrsi_k_prev": stochrsi_k_prev,
        "stochrsi_d_prev": stochrsi_d_prev,
        # Optional 5m
        "vwap_5m": vwap_5m_val,
        "stochrsi_k_5m": stochrsi_k_5m_val,
        "stochrsi_d_5m": stochrsi_d_5m_val,
        "stochrsi_k_prev_5m": stochrsi_k_prev_5m,
        "stochrsi_d_prev_5m": stochrsi_d_prev_5m,
    }

    log.debug(
        f"Indicators — EMA9={result['ema9']} EMA21={result['ema21']} "
        f"RSI2={result['rsi2']} RSI2_5m={result['rsi2_5m']} "
        f"ATR={result['atr']} ATR_5m={result['atr_5m']} "
        f"Vol={result['volume']} VolAvg={result['volume_avg']} "
        f"Vol5m={result['volume_5m']} VolAvg5m={result['volume_avg_5m']} "
        f"VWAP={result['vwap']} StochRSI_K={result['stochrsi_k']}"
    )

    return result
