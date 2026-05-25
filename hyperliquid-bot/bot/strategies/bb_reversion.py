"""
BB Reversion — Bollinger Band mean-reversion on 5m candles.

Entry: price returns inside the band after closing outside (or near it).
  Long : BBP_prev < bbp_long_threshold AND current candle closes back above BBL
         AND close still below BB midline.
  Short: BBP_prev > bbp_short_threshold AND current candle closes back below BBU
         AND close still above BB midline.

Filters:
  - EMA trend  : price above EMA → long only; below EMA → short only.
  - RSI(14)    : long only if RSI < rsi_long_max; short only if RSI > rsi_short_min.

Exit (priority order, verified per-candle):
  1. SL  — candle low  ≤ sl_price  (long) / candle high ≥ sl_price (short)
  2. TP  — candle high ≥ tp_price  (long) / candle low  ≤ tp_price (short)
  3. BB mid — close crosses BB midline (exit at close price)

Three pre-configured instances in manager.py (one per asset):
  bb_reversion_btc : BB(10, 2.0) + EMA50  + RSI<65 / RSI>35 | TP 2.0% / SL 0.8%
  bb_reversion_eth : BB(10, 2.0) + EMA50  + RSI<65 / RSI>35 | TP 1.0% / SL 1.0%
  bb_reversion_sol : BB(10, 2.0) + EMA200 + no RSI filter    | TP 2.0% / SL 0.5%
"""

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger
from bot import db
from bot.strategies.base import BaseStrategy, select_tf_df

log = get_logger("strategies.bb_reversion")


class BBReversionStrategy(BaseStrategy):
    NAME = "bb_reversion"
    DISPLAY_NAME = "BB Reversion (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = {
        "timeframe": "5m",
        "bb_period": 10,
        "bb_std":    2.0,
        "ema_period": 50,
        "rsi_long_max":  65,
        "rsi_short_min": 35,
        "tp_pct": 2.0,
        "sl_pct": 0.8,
        "bb_mid_exit": True,
        "bbp_long_threshold":  0.05,
        "bbp_short_threshold": 0.95,
        "assets": [],
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

    def _resolve_params(self, params: dict) -> dict:
        return {**self.DEFAULT_PARAMS, **params}

    def evaluate(self, asset, indicators, funding_rate, cfg, params,
                 df_1m=None, df_5m=None, df_15m=None, df_30m=None, df_1h=None, **kwargs):
        p = self._resolve_params(asset, params)
        tf, df = select_tf_df(p, kwargs,
                              df_5m=df_5m, df_15m=df_15m, df_30m=df_30m, df_1h=df_1h)
        if df is None:
            return None

        bb_period  = int(p["bb_period"])
        bb_std     = float(p["bb_std"])
        ema_period = int(p["ema_period"])
        rsi_long_max  = float(p["rsi_long_max"])
        rsi_short_min = float(p["rsi_short_min"])
        tp_pct     = float(p["tp_pct"]) / 100.0
        sl_pct     = float(p["sl_pct"]) / 100.0
        bbp_long_threshold  = float(p["bbp_long_threshold"])
        bbp_short_threshold = float(p["bbp_short_threshold"])
        _bme = p.get("bb_mid_exit", True)
        bb_mid_exit = str(_bme).lower() not in ("false", "0", "no")

        min_len = max(bb_period, ema_period) + 5
        if len(df) < min_len:
            return None

        # ── Indicators ────────────────────────────────────────────────
        bb  = ta.bbands(df["close"], length=bb_period, std=bb_std)
        ema = ta.ema(df["close"], length=ema_period)
        rsi = ta.rsi(df["close"], length=14)

        if bb is None or ema is None or rsi is None:
            return None

        bbu_col = [c for c in bb.columns if c.startswith("BBU_")]
        bbm_col = [c for c in bb.columns if c.startswith("BBM_")]
        bbl_col = [c for c in bb.columns if c.startswith("BBL_")]
        bbp_col = [c for c in bb.columns if c.startswith("BBP_")]

        if not (bbu_col and bbm_col and bbl_col and bbp_col):
            log.warning(f"[{asset}] BB columns not found: {list(bb.columns)}")
            return None

        if len(bb) < 2 or len(ema) < 1 or len(rsi) < 1:
            return None

        bbu = bb[bbu_col[0]]
        bbm = bb[bbm_col[0]]
        bbl = bb[bbl_col[0]]
        bbp = bb[bbp_col[0]]

        close_curr = float(df["close"].iloc[-1])

        bbu_curr = float(bbu.iloc[-1])
        bbl_curr = float(bbl.iloc[-1])
        bbm_curr = float(bbm.iloc[-1])
        bbp_prev = float(bbp.iloc[-2])

        ema_val = float(ema.iloc[-1])
        rsi_val = float(rsi.iloc[-1])

        if any(pd.isna(v) for v in [bbu_curr, bbl_curr, bbm_curr, bbp_prev, ema_val, rsi_val]):
            return None

        now = datetime.now(timezone.utc).isoformat()
        base = {
            "timestamp": now, "asset": asset,
            "executed": 0, "reason": None,
            "ema9": indicators.get("ema9"), "ema21": indicators.get("ema21"),
            "rsi2": round(rsi_val, 2),
            "volume": indicators.get("volume_5m", indicators["volume"]),
            "volume_avg": indicators.get("volume_avg_5m", indicators["volume_avg"]),
            "atr": indicators.get("atr_5m", indicators["atr"]),
            "funding_rate": funding_rate,
            "strategy_name": self.NAME,
        }

        # ── Entry triggers ────────────────────────────────────────────
        long_trigger  = bbp_prev < bbp_long_threshold  and close_curr > bbl_curr and close_curr < bbm_curr
        short_trigger = bbp_prev > bbp_short_threshold and close_curr < bbu_curr and close_curr > bbm_curr

        # ── Diagnostic scan log (permanente) ──────────────────────────
        log.signals(
            f"[{asset}] BB_REV SCAN [{self.NAME}] — "
            f"close={close_curr:.2f} BBP_prev={bbp_prev:.3f} "
            f"(long<{bbp_long_threshold} short>{bbp_short_threshold}) "
            f"RSI={rsi_val:.1f} EMA{ema_period}={ema_val:.2f} "
            f"trig=long:{long_trigger} short:{short_trigger}"
        )

        # ── LONG ──────────────────────────────────────────────────────
        if long_trigger:
            if close_curr < ema_val:
                reason = f"BB_REV LONG blocked: close {close_curr:.2f} < EMA{ema_period} {ema_val:.2f}"
                log.debug(f"[{asset}] {reason}")
                db.insert_signal({**base, "side": "long", "reason": reason})
                return None
            if rsi_val >= rsi_long_max:
                reason = f"BB_REV LONG blocked: RSI14={rsi_val:.1f} >= {rsi_long_max}"
                log.debug(f"[{asset}] {reason}")
                db.insert_signal({**base, "side": "long", "reason": reason})
                return None
            log.signals(
                f"[{asset}] BB_REVERSION LONG — "
                f"BBP_prev={bbp_prev:.3f} RSI={rsi_val:.1f} "
                f"EMA{ema_period}={ema_val:.2f} close={close_curr:.2f}"
            )
            return {
                **base,
                "side": "long",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": bbm_curr,
                "bb_mid_exit": bb_mid_exit,
            }

        # ── SHORT ─────────────────────────────────────────────────────
        if short_trigger:
            if close_curr > ema_val:
                reason = f"BB_REV SHORT blocked: close {close_curr:.2f} > EMA{ema_period} {ema_val:.2f}"
                log.debug(f"[{asset}] {reason}")
                db.insert_signal({**base, "side": "short", "reason": reason})
                return None
            if rsi_val <= rsi_short_min:
                reason = f"BB_REV SHORT blocked: RSI14={rsi_val:.1f} <= {rsi_short_min}"
                log.debug(f"[{asset}] {reason}")
                db.insert_signal({**base, "side": "short", "reason": reason})
                return None
            log.signals(
                f"[{asset}] BB_REVERSION SHORT — "
                f"BBP_prev={bbp_prev:.3f} RSI={rsi_val:.1f} "
                f"EMA{ema_period}={ema_val:.2f} close={close_curr:.2f}"
            )
            return {
                **base,
                "side": "short",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": bbm_curr,
                "bb_mid_exit": bb_mid_exit,
            }

        return None
