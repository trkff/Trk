"""
RSI Scalp — RSI crossover exiting the extreme zone on 5m candles.

Entry:
  Long : prev RSI < os AND curr RSI >= os  (exits oversold)
  Short: prev RSI > ob AND curr RSI <= ob  (exits overbought)
  ob = 100 - os (symmetric)

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

log = get_logger("strategies.rsi_scalp")


class RSIScalpStrategy(BaseStrategy):
    NAME = "rsi_scalp"
    DISPLAY_NAME = "RSI Scalp (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = {
        "timeframe": "5m",
        "rsi_period": 14,
        "rsi_os":     30,
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
        rsi_period = int(p["rsi_period"])
        rsi_os     = float(p["rsi_os"])
        rsi_ob     = 100.0 - rsi_os
        tp_pct     = float(p["tp_pct"]) / 100.0
        sl_pct     = float(p["sl_pct"]) / 100.0
        ema_period = int(p.get("ema_period", 0))

        min_len = rsi_period + 10
        if len(df) < min_len:
            return None

        rsi_series = ta.rsi(df["close"], length=rsi_period)
        if rsi_series is None or len(rsi_series) < 2:
            return None

        curr_rsi = float(rsi_series.iloc[-1])
        prev_rsi = float(rsi_series.iloc[-2])
        if pd.isna(curr_rsi) or pd.isna(prev_rsi):
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
        long_trig = prev_rsi < rsi_os and curr_rsi >= rsi_os
        short_trig = prev_rsi > rsi_ob and curr_rsi <= rsi_ob
        log.signals(
            f"[{asset}] RSI_SCALP SCAN [{self.NAME}] — "
            f"close={close_curr:.4f} prevRSI={prev_rsi:.1f} currRSI={curr_rsi:.1f} "
            f"(os={rsi_os:.0f} ob={rsi_ob:.0f})"
            + (f" EMA{ema_period}={ema_val:.4f}" if ema_val is not None else "")
            + f" trig=long:{long_trig} short:{short_trig}"
        )

        # ── LONG: RSI crosses out of oversold ────────────────────────
        if prev_rsi < rsi_os and curr_rsi >= rsi_os:
            if ema_val is not None and close_curr < ema_val:
                return None
            log.signals(
                f"[{asset}] RSI_SCALP LONG — "
                f"close={close_curr:.4f} prevRSI={prev_rsi:.1f} currRSI={curr_rsi:.1f} "
                f"os={rsi_os:.0f} tp={tp_pct:.3%} sl={sl_pct:.3%}"
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

        # ── SHORT: RSI crosses out of overbought ─────────────────────
        if prev_rsi > rsi_ob and curr_rsi <= rsi_ob:
            if ema_val is not None and close_curr > ema_val:
                return None
            log.signals(
                f"[{asset}] RSI_SCALP SHORT — "
                f"close={close_curr:.4f} prevRSI={prev_rsi:.1f} currRSI={curr_rsi:.1f} "
                f"ob={rsi_ob:.0f} tp={tp_pct:.3%} sl={sl_pct:.3%}"
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
