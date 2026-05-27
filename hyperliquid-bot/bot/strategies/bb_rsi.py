"""
BB RSI — Bollinger Band position + RSI extreme zone on 5m candles.

Entry: current BBP in extreme zone AND RSI in extreme zone simultaneously.

  Long : BBP_curr < bbp_long_threshold AND RSI < rsi_os
  Short: BBP_curr > bbp_short_threshold AND RSI > rsi_ob
  rsi_ob = 100 - rsi_os (symmetric)

Optional EMA filter (ema_period > 0):
  Long  blocked when close < EMA.
  Short blocked when close > EMA.

Exit (priority order, verified per-candle):
  1. SL  — candle low  <= sl_price (long) / candle high >= sl_price (short)
  2. TP  — candle high >= tp_price (long) / candle low  <= tp_price (short)
  3. BB mid — close crosses BB midline → exit at close price
"""

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger
from bot.strategies.base import BaseStrategy, select_tf_df
from bot.strategies.live_filters import apply_live_filters

log = get_logger("strategies.bb_rsi")


class BBRSIStrategy(BaseStrategy):
    NAME = "bb_rsi"
    DISPLAY_NAME = "BB RSI (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = {
        "timeframe": "5m",
        "bb_period":           15,
        "bb_std":              1.5,
        "bbp_long_threshold":  0.10,
        "bbp_short_threshold": 0.90,
        "rsi_period":          14,
        "rsi_os":              30,
        "tp_pct":              0.8,
        "sl_pct":              0.8,
        "bb_mid_exit":         False,
        "ema_period":          0,
        # ── Live filters (scanner v2) — defaults = off ──
        "adx_period":          0,
        "adx_min":             0,
        "session_start":       0,
        "session_end":         24,
        "atr_tp_mode":         False,
        "atr_tp_mult":         1.0,
        "atr_sl_mult":         1.0,
        "atr_period":          14,
        "assets":              [],
        "asset_overrides":     {},
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
        tf, df = select_tf_df(p, kwargs, name=self.NAME, asset=asset,
                              df_5m=df_5m, df_15m=df_15m, df_30m=df_30m, df_1h=df_1h)
        if df is None:
            return None
        bb_period            = int(p["bb_period"])
        bb_std               = float(p["bb_std"])
        bbp_long_threshold   = float(p["bbp_long_threshold"])
        bbp_short_threshold  = float(p["bbp_short_threshold"])
        rsi_period           = int(p["rsi_period"])
        rsi_os               = float(p["rsi_os"])
        rsi_ob               = 100.0 - rsi_os
        tp_pct               = float(p["tp_pct"]) / 100.0
        sl_pct               = float(p["sl_pct"]) / 100.0
        _bme                 = p.get("bb_mid_exit", False)
        bb_mid_exit          = str(_bme).lower() not in ("false", "0", "no")
        ema_period           = int(p.get("ema_period", 0))

        min_len = max(bb_period, rsi_period, ema_period if ema_period > 0 else 0) + 10
        if len(df) < min_len:
            return None

        bb = ta.bbands(df["close"], length=bb_period, std=bb_std)
        if bb is None:
            return None

        bbu_col = [c for c in bb.columns if c.startswith("BBU_")]
        bbl_col = [c for c in bb.columns if c.startswith("BBL_")]
        bbm_col = [c for c in bb.columns if c.startswith("BBM_")]
        if not (bbu_col and bbl_col and bbm_col):
            return None

        bbu_curr = float(bb[bbu_col[0]].iloc[-1])
        bbl_curr = float(bb[bbl_col[0]].iloc[-1])
        bbm_curr = float(bb[bbm_col[0]].iloc[-1])

        if any(pd.isna(v) for v in [bbu_curr, bbl_curr, bbm_curr]):
            return None

        span = bbu_curr - bbl_curr
        if span <= 0:
            return None

        close_curr = float(df["close"].iloc[-1])
        bbp_curr = (close_curr - bbl_curr) / span

        rsi_series = ta.rsi(df["close"], length=rsi_period)
        if rsi_series is None or pd.isna(rsi_series.iloc[-1]):
            return None
        rsi_curr = float(rsi_series.iloc[-1])

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
        long_trig = bbp_curr < bbp_long_threshold and rsi_curr < rsi_os
        short_trig = bbp_curr > bbp_short_threshold and rsi_curr > rsi_ob
        log.signals(
            f"[{asset}] BB_RSI SCAN [{self.NAME}] — "
            f"close={close_curr:.4f} BBP={bbp_curr:.3f} "
            f"(long<{bbp_long_threshold} short>{bbp_short_threshold}) "
            f"RSI={rsi_curr:.1f} (os={rsi_os:.0f} ob={rsi_ob:.0f})"
            + (f" EMA{ema_period}={ema_val:.4f}" if ema_val is not None else "")
            + f" trig=long:{long_trig} short:{short_trig}"
        )

        # ── LONG ─────────────────────────────────────────────────────
        if bbp_curr < bbp_long_threshold and rsi_curr < rsi_os:
            if ema_val is not None and close_curr < ema_val:
                return None
            log.signals(
                f"[{asset}] BB_RSI LONG — "
                f"close={close_curr:.4f} BBP={bbp_curr:.3f} RSI={rsi_curr:.1f} "
                f"tp={tp_pct:.3%} sl={sl_pct:.3%}"
            )
            return apply_live_filters(p, df, {
                **base,
                "side": "long",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": bbm_curr,
                "bb_mid_exit": bb_mid_exit,
            }, is_trend_strategy=False)

        # ── SHORT ────────────────────────────────────────────────────
        if bbp_curr > bbp_short_threshold and rsi_curr > rsi_ob:
            if ema_val is not None and close_curr > ema_val:
                return None
            log.signals(
                f"[{asset}] BB_RSI SHORT — "
                f"close={close_curr:.4f} BBP={bbp_curr:.3f} RSI={rsi_curr:.1f} "
                f"tp={tp_pct:.3%} sl={sl_pct:.3%}"
            )
            return apply_live_filters(p, df, {
                **base,
                "side": "short",
                "signal_price": close_curr,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "bb_mid": bbm_curr,
                "bb_mid_exit": bb_mid_exit,
            }, is_trend_strategy=False)

        return None
