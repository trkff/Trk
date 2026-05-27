"""
BB Stoch — Bollinger Band mean-reversion + Stochastic confirmation on 5m candles.

Entry: current candle BBP in extreme zone AND both Stoch %K and %D in extreme zone.

  Long : BBP_curr < bbp_long_threshold AND %K < stoch_long AND %D < stoch_long
  Short: BBP_curr > bbp_short_threshold AND %K > stoch_short AND %D > stoch_short

  smooth_k=3 (matches talib slowk_period=3 used in the original scan).
  No crossover required — simultaneous zone condition on the current bar.

Optional EMA filter (ema_period > 0):
  Long  blocked when close_curr < EMA (downtrend).
  Short blocked when close_curr > EMA (uptrend).

Exit (priority order, verified per-candle):
  1. SL  — candle low  ≤ sl_price  (long) / candle high ≥ sl_price (short)
  2. TP  — candle high ≥ tp_price  (long) / candle low  ≤ tp_price (short)
  3. BB mid — close crosses BB midline / SMA → exit at close price

Parameters:
  bb_period   : 15       — BB / SMA period
  bb_std      : 1.5      — BB standard-deviation multiplier
  stoch_k     : 14       — Stochastic %K window
  stoch_d     : 3        — %D smoothing (SMA of %K)
  stoch_long  : 25       — %K/%D threshold for oversold (long)
  stoch_short : 75       — %K/%D threshold for overbought (short)
  tp_pct      : 2.0      — Take-profit % from entry
  sl_pct      : 1.0      — Stop-loss % from entry
  bb_mid_exit : True     — Exit at BB midline (SMA) before TP/SL
  ema_period  : 0        — EMA trend filter period; 0 = disabled
"""

from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger
from bot import db
from bot.strategies.base import BaseStrategy, select_tf_df
from bot.strategies.live_filters import apply_live_filters

log = get_logger("strategies.bb_stoch")


class BBStochStrategy(BaseStrategy):
    NAME = "bb_stoch"
    DISPLAY_NAME = "BB + Stoch"
    REQUIRED_TIMEFRAMES = ["5m"]   # resolvido dinamicamente em get_required_timeframes() pelos params
    DEFAULT_PARAMS = {
        "timeframe":   "5m",
        "bb_period":   15,
        "bb_std":      1.5,
        "stoch_k":     14,
        "stoch_d":     3,
        "stoch_long":  25,
        "stoch_short": 75,
        "tp_pct":      2.0,
        "sl_pct":      1.0,
        "bb_mid_exit": True,
        "ema_period":  0,
        "bbp_long_threshold":  0.1,
        "bbp_short_threshold": 0.9,
        # ── Live filters (scanner v2) — defaults = off ──
        "adx_period":    0,
        "adx_min":       0,
        "session_start": 0,
        "session_end":   24,
        "atr_tp_mode":   False,
        "atr_tp_mult":   1.0,
        "atr_sl_mult":   1.0,
        "atr_period":    14,
        "assets":      [],
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
        tf, df = select_tf_df(p, kwargs, name=self.NAME, asset=asset,
                              df_5m=df_5m, df_15m=df_15m, df_30m=df_30m, df_1h=df_1h)
        if df is None:
            return None

        bb_period   = int(p["bb_period"])
        bb_std      = float(p["bb_std"])
        stoch_k     = int(p["stoch_k"])
        stoch_d     = int(p["stoch_d"])
        stoch_long  = float(p["stoch_long"])
        stoch_short = float(p["stoch_short"])
        tp_pct      = float(p["tp_pct"]) / 100.0
        sl_pct      = float(p["sl_pct"]) / 100.0
        _bme        = p.get("bb_mid_exit", True)
        bb_mid_exit = str(_bme).lower() not in ("false", "0", "no")
        bbp_long_threshold  = float(p["bbp_long_threshold"])
        bbp_short_threshold = float(p["bbp_short_threshold"])
        ema_period  = int(p.get("ema_period", 0))

        min_len = max(bb_period, stoch_k, ema_period if ema_period > 0 else 0) + 10
        if len(df) < min_len:
            return None

        # ── Indicators ────────────────────────────────────────────────
        bb    = ta.bbands(df["close"], length=bb_period, std=bb_std)
        stoch = ta.stoch(df["high"], df["low"], df["close"],
                         k=stoch_k, d=stoch_d, smooth_k=3)

        if bb is None or stoch is None:
            return None

        bbu_col = [c for c in bb.columns if c.startswith("BBU_")]
        bbl_col = [c for c in bb.columns if c.startswith("BBL_")]
        bbm_col = [c for c in bb.columns if c.startswith("BBM_")]
        stk_col = [c for c in stoch.columns if c.startswith("STOCHk_")]
        std_col = [c for c in stoch.columns if c.startswith("STOCHd_")]

        if not (bbu_col and bbl_col and bbm_col and stk_col and std_col):
            log.warning(f"[{asset}] BB/Stoch columns not found: {list(bb.columns)} {list(stoch.columns)}")
            return None

        bbu = bb[bbu_col[0]]
        bbl = bb[bbl_col[0]]
        bbm = bb[bbm_col[0]]
        stk = stoch[stk_col[0]]
        std = stoch[std_col[0]]

        if len(bbu) < 1 or len(stk) < 1:
            return None

        close_curr = float(df["close"].iloc[-1])

        bbu_curr = float(bbu.iloc[-1])
        bbl_curr = float(bbl.iloc[-1])
        bbm_curr = float(bbm.iloc[-1])

        stk_curr = float(stk.iloc[-1])
        std_curr = float(std.iloc[-1])

        if any(pd.isna(v) for v in [bbu_curr, bbl_curr, bbm_curr, stk_curr, std_curr]):
            return None

        # ── Optional EMA filter ───────────────────────────────────────
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

        # ── Bollinger %B do candle atual ─────────────────────────────
        band_range_curr = bbu_curr - bbl_curr + 1e-9
        bbp_curr = (close_curr - bbl_curr) / band_range_curr

        # ── Entry triggers (replicar lógica do scan) ──────────────────
        long_bb  = bbp_curr < bbp_long_threshold
        short_bb = bbp_curr > bbp_short_threshold

        long_stoch  = stk_curr < stoch_long  and std_curr < stoch_long
        short_stoch = stk_curr > stoch_short and std_curr > stoch_short

        # ── Diagnostic scan log (permanente) ──────────────────────────
        # Loga valores brutos a cada 5m close, mesmo sem sinal — permite
        # comparar com backtest candle a candle quando há divergência.
        log.signals(
            f"[{asset}] BB_STOCH SCAN [{self.NAME}] — "
            f"close={close_curr:.2f} "
            f"BBP={bbp_curr:.3f} (long<{bbp_long_threshold} short>{bbp_short_threshold}) "
            f"StochK={stk_curr:.1f} StochD={std_curr:.1f} (long<{stoch_long} short>{stoch_short}) "
            f"trig=long:{long_bb and long_stoch} short:{short_bb and short_stoch}"
        )

        # ── LONG ──────────────────────────────────────────────────────
        if long_bb and long_stoch:
            if ema_val is not None and close_curr < ema_val:
                log.debug(
                    f"[{asset}] BB_STOCH LONG blocked: close {close_curr:.2f} < EMA{ema_period} {ema_val:.2f}"
                )
                return None
            log.signals(
                f"[{asset}] BB_STOCH LONG — "
                f"close={close_curr:.2f} BBL={bbl_curr:.2f} BBM={bbm_curr:.2f} "
                f"StochK={stk_curr:.1f} StochD={std_curr:.1f}"
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

        # ── SHORT ─────────────────────────────────────────────────────
        if short_bb and short_stoch:
            if ema_val is not None and close_curr > ema_val:
                log.debug(
                    f"[{asset}] BB_STOCH SHORT blocked: close {close_curr:.2f} > EMA{ema_period} {ema_val:.2f}"
                )
                return None
            log.signals(
                f"[{asset}] BB_STOCH SHORT — "
                f"close={close_curr:.2f} BBU={bbu_curr:.2f} BBM={bbm_curr:.2f} "
                f"StochK={stk_curr:.1f} StochD={std_curr:.1f}"
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
