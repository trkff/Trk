"""
EMA Cross — Fast EMA crosses above/below Slow EMA on 5m candles.

Entry: fast EMA crosses slow EMA.
  Long:  prev_fast <= prev_slow AND curr_fast >  curr_slow AND (close >= EMA_trend or trend==0)
  Short: prev_fast >= prev_slow AND curr_fast <  curr_slow AND (close <= EMA_trend or trend==0)

SL modes (use_atr_sl):
  False: SL fixed at sl_pct % from entry
  True:  SL distance = ATR(atr_period) * atr_mult; converted to sl_pct = dist / close

Exit (priority order):
  1. SL
  2. TP fixed at tp_pct % from entry
  No BB midline exit.
"""

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger
from bot.strategies.base import BaseStrategy, select_tf_df
from bot.strategies.live_filters import apply_live_filters

log = get_logger("strategies.ema_cross")


class EMACrossStrategy(BaseStrategy):
    NAME = "ema_cross"
    DISPLAY_NAME = "EMA Cross (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = {
        "timeframe": "5m",
        "ema_fast":   9,
        "ema_slow":   21,
        "ema_trend":  0,
        "tp_pct":     1.5,
        "sl_pct":     0.5,
        "use_atr_sl": False,
        "atr_period": 14,
        "atr_mult":   1.0,
        # ── Live filters (scanner v2) — defaults = off ──
        # NB: atr_period acima já existe (compartilhado com use_atr_sl).
        # atr_tp_mode aqui é independente — quando True, sobrescreve TANTO
        # tp_pct quanto sl_pct (via apply_atr_tp_sl), tomando precedência
        # sobre use_atr_sl que só mexe em SL.
        "adx_period":    0,
        "adx_min":       0,
        "session_start": 0,
        "session_end":   24,
        "atr_tp_mode":   False,
        "atr_tp_mult":   1.0,
        "atr_sl_mult":   1.0,
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

        ema_fast   = int(p["ema_fast"])
        ema_slow   = int(p["ema_slow"])
        ema_trend  = int(p.get("ema_trend", 0))
        tp_pct     = float(p["tp_pct"]) / 100.0
        sl_pct     = float(p["sl_pct"]) / 100.0
        use_atr_sl = str(p.get("use_atr_sl", False)).lower() not in ("false", "0", "no")
        atr_period = int(p.get("atr_period", 14))
        atr_mult   = float(p.get("atr_mult", 1.0))

        min_len = max(ema_fast, ema_slow,
                      ema_trend if ema_trend > 0 else 0,
                      atr_period if use_atr_sl else 0) + 10
        if len(df) < min_len:
            return None

        fast_series = ta.ema(df["close"], length=ema_fast)
        slow_series = ta.ema(df["close"], length=ema_slow)

        if fast_series is None or slow_series is None:
            return None
        if len(fast_series) < 2 or len(slow_series) < 2:
            return None

        curr_fast = fast_series.iloc[-1]
        prev_fast = fast_series.iloc[-2]
        curr_slow = slow_series.iloc[-1]
        prev_slow = slow_series.iloc[-2]

        if any(pd.isna(v) for v in [curr_fast, prev_fast, curr_slow, prev_slow]):
            return None

        curr_fast = float(curr_fast)
        prev_fast = float(prev_fast)
        curr_slow = float(curr_slow)
        prev_slow = float(prev_slow)
        close_curr = float(df["close"].iloc[-1])

        # ── Optional trend filter ─────────────────────────────────────
        trend_val = None
        if ema_trend > 0:
            trend_series = ta.ema(df["close"], length=ema_trend)
            if trend_series is None or pd.isna(trend_series.iloc[-1]):
                return None
            trend_val = float(trend_series.iloc[-1])

        # ── ATR-based SL ──────────────────────────────────────────────
        atr_sl_dist = None
        if use_atr_sl:
            atr_series = ta.atr(df["high"], df["low"], df["close"],
                                length=atr_period)
            if atr_series is None or pd.isna(atr_series.iloc[-1]):
                return None
            atr_val = float(atr_series.iloc[-1])
            atr_sl_dist = atr_val * atr_mult
            if close_curr > 0:
                sl_pct = atr_sl_dist / close_curr
            else:
                return None

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
        long_trig = prev_fast <= prev_slow and curr_fast > curr_slow
        short_trig = prev_fast >= prev_slow and curr_fast < curr_slow
        log.signals(
            f"[{asset}] EMA_CROSS SCAN [{self.NAME}] — "
            f"close={close_curr:.4f} fast={curr_fast:.4f} slow={curr_slow:.4f} "
            f"prev_fast={prev_fast:.4f} prev_slow={prev_slow:.4f}"
            + (f" trend{ema_trend}={trend_val:.4f}" if trend_val is not None else "")
            + f" trig=long:{long_trig} short:{short_trig}"
        )

        # ── LONG: fast crosses above slow ─────────────────────────────
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            if trend_val is not None and close_curr < trend_val:
                log.debug(
                    f"[{asset}] EMA_CROSS LONG blocked: "
                    f"close {close_curr:.4f} < EMA{ema_trend} {trend_val:.4f}"
                )
                return None
            log.signals(
                f"[{asset}] EMA_CROSS LONG — "
                f"close={close_curr:.4f} fast={curr_fast:.4f} slow={curr_slow:.4f} "
                f"tp={tp_pct:.3%} sl={sl_pct:.3%}"
                + (f" atr_dist={atr_sl_dist:.4f}" if atr_sl_dist is not None else "")
            )
            sig = {
                **base,
                "side": "long",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": None,
                "bb_mid_exit": False,
            }
            if atr_sl_dist is not None:
                sig["atr_sl_dist"] = float(atr_sl_dist)
            return apply_live_filters(p, df, sig, is_trend_strategy=True)

        # ── SHORT: fast crosses below slow ────────────────────────────
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            if trend_val is not None and close_curr > trend_val:
                log.debug(
                    f"[{asset}] EMA_CROSS SHORT blocked: "
                    f"close {close_curr:.4f} > EMA{ema_trend} {trend_val:.4f}"
                )
                return None
            log.signals(
                f"[{asset}] EMA_CROSS SHORT — "
                f"close={close_curr:.4f} fast={curr_fast:.4f} slow={curr_slow:.4f} "
                f"tp={tp_pct:.3%} sl={sl_pct:.3%}"
                + (f" atr_dist={atr_sl_dist:.4f}" if atr_sl_dist is not None else "")
            )
            sig = {
                **base,
                "side": "short",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": None,
                "bb_mid_exit": False,
            }
            if atr_sl_dist is not None:
                sig["atr_sl_dist"] = float(atr_sl_dist)
            return apply_live_filters(p, df, sig, is_trend_strategy=True)

        return None
