"""
Stoch Scalp — Stochastic crossover while both %K and %D are in extreme zone.

Entry: both %K and %D were in zone on prev bar, %K crosses %D on current bar.
  Long:  prev_K < stoch_os AND prev_D < stoch_os AND curr_K > curr_D AND prev_K <= prev_D
  Short: prev_K > stoch_ob AND prev_D > stoch_ob AND curr_K < curr_D AND prev_K >= prev_D
  stoch_ob = 100 - stoch_os (symmetric)

Exit (priority order, verified per-candle):
  1. SL — candle low  <= sl_price (long) / candle high >= sl_price (short)
  2. TP — candle high >= tp_price (long) / candle low  <= tp_price (short)
  No BB midline exit.
"""

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger
from bot.strategies.base import BaseStrategy, select_tf_df
from bot.strategies.live_filters import apply_live_filters

log = get_logger("strategies.stoch_scalp")


class StochScalpStrategy(BaseStrategy):
    NAME = "stoch_scalp"
    DISPLAY_NAME = "Stoch Scalp (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = {
        "timeframe": "5m",
        "stoch_k":    9,
        "stoch_d":    3,
        "stoch_os":   40,
        "tp_pct":     0.5,
        "sl_pct":     0.8,
        "ema_period": 50,
        # ── Live filters (scanner v2) — defaults = off ──
        "adx_period":    0,
        "adx_min":       0,
        "session_start": 0,
        "session_end":   24,
        "atr_tp_mode":   False,
        "atr_tp_mult":   1.0,
        "atr_sl_mult":   1.0,
        "atr_period":    14,
        "assets":     [],
        "asset_overrides": {},
    }

    def __init__(self, name: str | None = None, display_name: str | None = None,
                 extra_defaults: dict | None = None):
        if name:
            self.NAME = name
        if display_name:
            self.DISPLAY_NAME = display_name
        if extra_defaults:
            self.DEFAULT_PARAMS = {**self.__class__.DEFAULT_PARAMS, **extra_defaults}

    def _resolve_params(self, asset: str, params: dict) -> dict:
        return {**self.DEFAULT_PARAMS, **params}

    def evaluate(self, asset, indicators, funding_rate, cfg, params,
                 df_1m=None, df_5m=None, df_15m=None, df_30m=None, df_1h=None, **kwargs):
        p = self._resolve_params(asset, params)
        tf, df = select_tf_df(p, kwargs,
                              df_5m=df_5m, df_15m=df_15m, df_30m=df_30m, df_1h=df_1h)
        if df is None:
            return None

        stoch_k    = int(p["stoch_k"])
        stoch_d    = int(p["stoch_d"])
        stoch_os   = float(p["stoch_os"])
        stoch_ob   = 100.0 - stoch_os
        tp_pct     = float(p["tp_pct"]) / 100.0
        sl_pct     = float(p["sl_pct"]) / 100.0
        ema_period = int(p.get("ema_period", 0))

        min_len = max(stoch_k, ema_period if ema_period > 0 else 0) + 10
        if len(df) < min_len:
            return None

        stoch = ta.stoch(df["high"], df["low"], df["close"],
                         k=stoch_k, d=stoch_d, smooth_k=3)
        if stoch is None:
            return None

        stk_col = [c for c in stoch.columns if c.startswith("STOCHk_")]
        std_col = [c for c in stoch.columns if c.startswith("STOCHd_")]
        if not stk_col or not std_col:
            log.warning(f"[{asset}] Stoch columns not found: {list(stoch.columns)}")
            return None

        stk = stoch[stk_col[0]]
        std = stoch[std_col[0]]

        if len(stk) < 2:
            return None

        curr_k = stk.iloc[-1]
        prev_k = stk.iloc[-2]
        curr_d = std.iloc[-1]
        prev_d = std.iloc[-2]

        if any(pd.isna(v) for v in [curr_k, prev_k, curr_d, prev_d]):
            return None

        curr_k = float(curr_k)
        prev_k = float(prev_k)
        curr_d = float(curr_d)
        prev_d = float(prev_d)
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
        long_trig = prev_k < stoch_os and prev_d < stoch_os and curr_k > curr_d and prev_k <= prev_d
        short_trig = prev_k > stoch_ob and prev_d > stoch_ob and curr_k < curr_d and prev_k >= prev_d
        log.signals(
            f"[{asset}] STOCH_SCALP SCAN [{self.NAME}] — "
            f"close={close_curr:.4f} prevK={prev_k:.1f} currK={curr_k:.1f} "
            f"prevD={prev_d:.1f} currD={curr_d:.1f} "
            f"(os={stoch_os:.0f} ob={stoch_ob:.0f}) "
            f"trig=long:{long_trig} short:{short_trig}"
        )

        # ── LONG: both K and D were oversold, K crosses above D ──────
        if prev_k < stoch_os and prev_d < stoch_os and curr_k > curr_d and prev_k <= prev_d:
            if ema_val is not None and close_curr < ema_val:
                log.debug(
                    f"[{asset}] STOCH_SCALP LONG blocked: "
                    f"close {close_curr:.4f} < EMA{ema_period} {ema_val:.4f}"
                )
                return None
            log.signals(
                f"[{asset}] STOCH_SCALP LONG — "
                f"close={close_curr:.4f} prevK={prev_k:.1f} currK={curr_k:.1f} "
                f"prevD={prev_d:.1f} currD={curr_d:.1f} "
                f"os={stoch_os:.0f} tp={tp_pct:.3%} sl={sl_pct:.3%}"
            )
            return apply_live_filters(p, df, {
                **base,
                "side": "long",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": None,
                "bb_mid_exit": False,
            }, is_trend_strategy=False)

        # ── SHORT: both K and D were overbought, K crosses below D ───
        if prev_k > stoch_ob and prev_d > stoch_ob and curr_k < curr_d and prev_k >= prev_d:
            if ema_val is not None and close_curr > ema_val:
                log.debug(
                    f"[{asset}] STOCH_SCALP SHORT blocked: "
                    f"close {close_curr:.4f} > EMA{ema_period} {ema_val:.4f}"
                )
                return None
            log.signals(
                f"[{asset}] STOCH_SCALP SHORT — "
                f"close={close_curr:.4f} prevK={prev_k:.1f} currK={curr_k:.1f} "
                f"prevD={prev_d:.1f} currD={curr_d:.1f} "
                f"ob={stoch_ob:.0f} tp={tp_pct:.3%} sl={sl_pct:.3%}"
            )
            return apply_live_filters(p, df, {
                **base,
                "side": "short",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": None,
                "bb_mid_exit": False,
            }, is_trend_strategy=False)

        return None
