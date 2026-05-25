"""
MACD Cross — MACD line crosses the signal line on 5m candles.

Entry:
  Long : MACD crosses above signal (prev MACD <= signal, curr MACD > signal)
  Short: MACD crosses below signal (prev MACD >= signal, curr MACD < signal)

Optional EMA trend filter (ema_trend > 0):
  Long  blocked when close < EMA.
  Short blocked when close > EMA.

Exit (priority order, verified per-candle):
  1. SL — candle low  <= sl_price (long) / candle high >= sl_price (short)
  2. TP — candle high >= tp_price (long) / candle low  <= tp_price (short)
"""

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger
from bot.strategies.base import BaseStrategy, select_tf_df

log = get_logger("strategies.macd_cross")


class MACDCrossStrategy(BaseStrategy):
    NAME = "macd_cross"
    DISPLAY_NAME = "MACD Cross (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = {
        "timeframe": "5m",
        "macd_fast":   12,
        "macd_slow":   26,
        "macd_signal": 9,
        "tp_pct":      1.0,
        "sl_pct":      0.5,
        "ema_trend":   0,
        "assets":      [],
        "asset_overrides": {},
    }

    def __init__(self, name=None, display_name=None, extra_defaults=None):
        if name:
            self.NAME = name
        if display_name:
            self.DISPLAY_NAME = display_name
        if extra_defaults:
            self.DEFAULT_PARAMS = {**self.__class__.DEFAULT_PARAMS, **extra_defaults}

    def _resolve_params(self, asset, params):
        return {**self.DEFAULT_PARAMS, **params}

    def evaluate(self, asset, indicators, funding_rate, cfg, params,
                 df_1m=None, df_5m=None, df_15m=None, df_30m=None, df_1h=None, **kwargs):
        p = self._resolve_params(asset, params)
        tf, df = select_tf_df(p, kwargs,
                              df_5m=df_5m, df_15m=df_15m, df_30m=df_30m, df_1h=df_1h)
        if df is None:
            return None
        macd_fast   = int(p["macd_fast"])
        macd_slow   = int(p["macd_slow"])
        macd_signal = int(p["macd_signal"])
        tp_pct      = float(p["tp_pct"]) / 100.0
        sl_pct      = float(p["sl_pct"]) / 100.0
        ema_trend   = int(p.get("ema_trend", 0))

        min_len = macd_slow + macd_signal + 10
        if len(df) < min_len:
            return None

        macd_df = ta.macd(df["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal)
        if macd_df is None:
            return None

        macd_col = [c for c in macd_df.columns if c.startswith("MACD_")]
        sig_col  = [c for c in macd_df.columns if c.startswith("MACDs_")]
        if not macd_col or not sig_col:
            log.warning(f"[{asset}] MACD columns not found: {list(macd_df.columns)}")
            return None

        macd_line = macd_df[macd_col[0]]
        sig_line  = macd_df[sig_col[0]]

        if len(macd_line) < 2:
            return None

        curr_macd = float(macd_line.iloc[-1])
        prev_macd = float(macd_line.iloc[-2])
        curr_sig  = float(sig_line.iloc[-1])
        prev_sig  = float(sig_line.iloc[-2])

        if any(pd.isna(v) for v in [curr_macd, prev_macd, curr_sig, prev_sig]):
            return None

        close_curr = float(df["close"].iloc[-1])

        ema_val = None
        if ema_trend > 0:
            ema_series = ta.ema(df["close"], length=ema_trend)
            if ema_series is None or pd.isna(ema_series.iloc[-1]):
                return None
            ema_val = float(ema_series.iloc[-1])

        now = datetime.now(timezone.utc).isoformat()
        base = {
            "timestamp": now, "asset": asset,
            "executed": 0, "reason": None,
            "ema9": indicators.get("ema9"), "ema21": indicators.get("ema21"),
            "rsi2": indicators.get("rsi2", 0),
            "volume": indicators.get("volume_5m", indicators["volume"]),
            "volume_avg": indicators.get("volume_avg_5m", indicators["volume_avg"]),
            "atr": indicators.get("atr_5m", indicators["atr"]),
            "funding_rate": funding_rate,
            "strategy_name": self.NAME,
        }

        # ── Diagnostic scan log (permanente) ──────────────────────────
        long_trig = curr_macd > curr_sig and prev_macd <= prev_sig
        short_trig = curr_macd < curr_sig and prev_macd >= prev_sig
        log.signals(
            f"[{asset}] MACD_CROSS SCAN [{self.NAME}] — "
            f"close={close_curr:.4f} MACD={curr_macd:.4f} SIG={curr_sig:.4f} "
            f"prev_MACD={prev_macd:.4f} prev_SIG={prev_sig:.4f}"
            + (f" trend{ema_trend}={ema_val:.4f}" if ema_val is not None else "")
            + f" trig=long:{long_trig} short:{short_trig}"
        )

        # ── LONG: MACD crosses above signal ──────────────────────────
        if curr_macd > curr_sig and prev_macd <= prev_sig:
            if ema_val is not None and close_curr < ema_val:
                return None
            log.signals(
                f"[{asset}] MACD_CROSS LONG — "
                f"close={close_curr:.4f} MACD={curr_macd:.4f} SIG={curr_sig:.4f} "
                f"tp={tp_pct:.3%} sl={sl_pct:.3%}"
            )
            return {
                **base,
                "side": "long",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": None,
                "bb_mid_exit": False,
            }

        # ── SHORT: MACD crosses below signal ─────────────────────────
        if curr_macd < curr_sig and prev_macd >= prev_sig:
            if ema_val is not None and close_curr > ema_val:
                return None
            log.signals(
                f"[{asset}] MACD_CROSS SHORT — "
                f"close={close_curr:.4f} MACD={curr_macd:.4f} SIG={curr_sig:.4f} "
                f"tp={tp_pct:.3%} sl={sl_pct:.3%}"
            )
            return {
                **base,
                "side": "short",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": None,
                "bb_mid_exit": False,
            }

        return None
