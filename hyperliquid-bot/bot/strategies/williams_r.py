"""
Williams %R Scalp — Williams %R crossover exiting the extreme zone on 5m candles.

Williams %R ranges from -100 (oversold) to 0 (overbought).

Entry:
  Long : prev %R < os AND curr %R >= os  (exits oversold, e.g. os=-80)
  Short: prev %R > ob AND curr %R <= ob  (exits overbought, ob = os + 100)
  Example: os=-80 → ob=-20

Optional EMA filter (ema_period > 0):
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

log = get_logger("strategies.williams_r")


class WilliamsRStrategy(BaseStrategy):
    NAME = "williams_r"
    DISPLAY_NAME = "Williams %R (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = {
        "timeframe": "5m",
        "wr_period":  14,
        "wr_os":      -80,   # oversold threshold (negative)
        "tp_pct":     0.8,
        "sl_pct":     0.8,
        "ema_period": 0,
        "assets":     [],
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
        wr_period  = int(p["wr_period"])
        wr_os      = float(p["wr_os"])      # e.g. -80 (oversold)
        wr_ob      = wr_os + 100.0          # e.g. -20 (overbought)
        tp_pct     = float(p["tp_pct"]) / 100.0
        sl_pct     = float(p["sl_pct"]) / 100.0
        ema_period = int(p.get("ema_period", 0))

        min_len = wr_period + 10
        if len(df) < min_len:
            return None

        wr_series = ta.willr(df["high"], df["low"], df["close"], length=wr_period)
        if wr_series is None or len(wr_series) < 2:
            return None

        curr_wr = float(wr_series.iloc[-1])
        prev_wr = float(wr_series.iloc[-2])
        if pd.isna(curr_wr) or pd.isna(prev_wr):
            return None

        close_curr = float(df["close"].iloc[-1])

        ema_val = None
        if ema_period > 0:
            ema_series = ta.ema(df["close"], length=ema_period)
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
        long_trig = prev_wr < wr_os and curr_wr >= wr_os
        short_trig = prev_wr > wr_ob and curr_wr <= wr_ob
        log.signals(
            f"[{asset}] WILLIAMS_R SCAN [{self.NAME}] — "
            f"close={close_curr:.4f} prevWR={prev_wr:.1f} currWR={curr_wr:.1f} "
            f"(os={wr_os:.0f} ob={wr_ob:.0f})"
            + (f" EMA{ema_period}={ema_val:.4f}" if ema_val is not None else "")
            + f" trig=long:{long_trig} short:{short_trig}"
        )

        # ── LONG: %R exits oversold zone ─────────────────────────────
        if prev_wr < wr_os and curr_wr >= wr_os:
            if ema_val is not None and close_curr < ema_val:
                return None
            log.signals(
                f"[{asset}] WILLIAMS_R LONG — "
                f"close={close_curr:.4f} prevWR={prev_wr:.1f} currWR={curr_wr:.1f} "
                f"os={wr_os:.0f} tp={tp_pct:.3%} sl={sl_pct:.3%}"
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

        # ── SHORT: %R exits overbought zone ──────────────────────────
        if prev_wr > wr_ob and curr_wr <= wr_ob:
            if ema_val is not None and close_curr > ema_val:
                return None
            log.signals(
                f"[{asset}] WILLIAMS_R SHORT — "
                f"close={close_curr:.4f} prevWR={prev_wr:.1f} currWR={curr_wr:.1f} "
                f"ob={wr_ob:.0f} tp={tp_pct:.3%} sl={sl_pct:.3%}"
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
