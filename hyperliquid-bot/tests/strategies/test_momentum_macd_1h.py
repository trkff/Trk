import pytest
import pandas as pd
from bot.strategies.momentum_macd_1h import MomentumMACD1HStrategy


def _make_df_1h(n=300, prices=None, volumes=None):
    """1H DataFrame. prices/volumes are lists; defaults: 100.0 / 150.0."""
    rows = []
    for i in range(n):
        p = prices[i] if prices and i < len(prices) else 100.0
        v = volumes[i] if volumes and i < len(volumes) else 150.0
        rows.append({
            "timestamp": i * 3600000,
            "open": p - 0.1,
            "high": p + 0.5,
            "low": p - 0.5,
            "close": p,
            "volume": v,
        })
    return pd.DataFrame(rows)


BASE_INDICATORS = {
    "ema9": 100.0, "ema21": 99.0, "rsi2": 50.0,
    "volume": 2.0, "volume_avg": 1.0,
    "atr": 5.0,
    "close_1m": 100.0, "close_5m": 100.0,
}
BASE_CFG = {"fee_rate_round_trip": "0.0009"}
BASE_PARAMS = {
    "vol_multiplier": 1.2,
    "tp_atr_multiplier": 3.0,
    "sl_atr_multiplier": 1.5,
    "funding_rate_limit": 0.0005,
}


def _mock_ema_long(n):
    """EMA50 > EMA200 — bullish."""
    def _ema(s, length):
        if length == 50:
            return pd.Series([101.0] * n)
        else:  # 200
            return pd.Series([100.0] * n)
    return _ema


def _mock_ema_short(n):
    """EMA50 < EMA200 — bearish."""
    def _ema(s, length):
        if length == 50:
            return pd.Series([99.0] * n)
        else:  # 200
            return pd.Series([100.0] * n)
    return _ema


def _mock_macd_zero_cross_up(n):
    """MACD hist: prev <= 0, curr > 0 — bullish zero-cross."""
    def _macd(s, fast, slow, signal):
        hist = pd.Series([0.1] * n)
        hist.iloc[-2] = -0.05
        hist.iloc[-1] = 0.05
        return pd.DataFrame({"MACDh_12_26_9": hist})
    return _macd


def _mock_macd_zero_cross_down(n):
    """MACD hist: prev >= 0, curr < 0 — bearish zero-cross."""
    def _macd(s, fast, slow, signal):
        hist = pd.Series([-0.1] * n)
        hist.iloc[-2] = 0.05
        hist.iloc[-1] = -0.05
        return pd.DataFrame({"MACDh_12_26_9": hist})
    return _macd


def _vol_ok_df(n):
    """All volumes = 150, last = 250 → 250 > rolling_mean~152*1.2 ✓"""
    volumes = [150.0] * n
    volumes[-1] = 250.0
    return _make_df_1h(n, volumes=volumes)


class TestMomentumMACD1H:
    def setup_method(self):
        self.strategy = MomentumMACD1HStrategy()
        self.cfg = BASE_CFG
        self.params = BASE_PARAMS.copy()

    def test_strategy_name(self):
        assert self.strategy.NAME == "momentum_macd_1h"

    def test_display_name_contains_1h(self):
        assert "1H" in self.strategy.DISPLAY_NAME

    def test_returns_none_when_new_1h_false(self, monkeypatch):
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=_make_df_1h(210), new_1h=False,
        )
        assert result is None

    def test_returns_none_when_df_1h_is_none(self, monkeypatch):
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=None, new_1h=True,
        )
        assert result is None

    def test_returns_none_when_df_1h_too_short(self, monkeypatch):
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=_make_df_1h(200), new_1h=True,
        )
        assert result is None

    def test_long_signal_fires(self, monkeypatch):
        """LONG: EMA50>EMA200 (uptrend) + MACD hist zero-cross up + vol ok."""
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_up(n))

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is not None
        assert result["side"] == "long"
        assert "tp_atr_multiplier" in result
        assert "sl_atr_multiplier" in result
        assert "sl_price_hint" not in result  # ATR mode, not RR mode

    def test_short_signal_fires(self, monkeypatch):
        """SHORT: EMA50<EMA200 (downtrend) + MACD hist zero-cross down + vol ok."""
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_short(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_down(n))

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is not None
        assert result["side"] == "short"
        assert "tp_atr_multiplier" in result
        assert "sl_atr_multiplier" in result
        assert "sl_price_hint" not in result

    def test_atr_multipliers_match_params(self, monkeypatch):
        """Signal carries tp_atr_multiplier=3.0 and sl_atr_multiplier=1.5 from params."""
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_up(n))

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is not None
        assert result["tp_atr_multiplier"] == 3.0
        assert result["sl_atr_multiplier"] == 1.5

    def test_no_zero_cross_no_long(self, monkeypatch):
        """MACD hist both negative → no zero-cross → no signal."""
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))

        def _macd_no_cross(s, fast, slow, signal):
            hist = pd.Series([-0.1] * n)
            return pd.DataFrame({"MACDh_12_26_9": hist})

        monkeypatch.setattr(_mod.ta, "macd", _macd_no_cross)

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is None

    def test_low_volume_blocks_signal(self, monkeypatch):
        """Volume below rolling-20-mean * multiplier → no signal."""
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        volumes = [200.0] * n
        volumes[-1] = 50.0  # 50 < 200*1.2 → blocked
        df = _make_df_1h(n, volumes=volumes)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_up(n))

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is None

    def test_volume_uses_df_1h_not_indicators(self, monkeypatch):
        """Volume check uses df_1h data — indicators['volume_avg'] is irrelevant."""
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_up(n))

        ind = {**BASE_INDICATORS, "volume": 0.0001, "volume_avg": 99999.0}
        result = self.strategy.evaluate(
            "BTC", ind, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is not None
        assert result["side"] == "long"

    def test_fee_blocked_long_inserts_signal(self, monkeypatch):
        """Fee check failure inserts blocked signal and returns None."""
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_up(n))

        # atr tiny: (0.001/100)*3.0 = 0.00003 < 0.0009 → blocked
        ind = {**BASE_INDICATORS, "atr": 0.001, "close_1m": 100.0}
        result = self.strategy.evaluate(
            "BTC", ind, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is None
        assert len(signals) == 1
        assert "ATR insuficiente" in signals[0]["reason"]
        assert signals[0]["side"] == "long"

    def test_funding_blocks_long_inserts_signal(self, monkeypatch):
        """Excessive funding blocks LONG and inserts signal."""
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_up(n))

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.001, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is None
        assert len(signals) == 1
        assert "funding" in signals[0]["reason"].lower()

    def test_funding_blocks_short_inserts_signal(self, monkeypatch):
        """Excessive negative funding blocks SHORT and inserts signal."""
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_short(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_down(n))

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, -0.001, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is None
        assert len(signals) == 1
        assert "funding" in signals[0]["reason"].lower()

    def test_uptrend_with_bearish_cross_no_signal(self, monkeypatch):
        """Uptrend (EMA50>EMA200) but MACD crosses DOWN → no long signal."""
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)
        import bot.strategies.momentum_macd_1h as _mod

        n = 210
        df = _vol_ok_df(n)
        monkeypatch.setattr(_mod.ta, "ema", _mock_ema_long(n))
        monkeypatch.setattr(_mod.ta, "macd", _mock_macd_zero_cross_down(n))

        result = self.strategy.evaluate(
            "BTC", BASE_INDICATORS, 0.0, self.cfg, self.params,
            df_1h=df, new_1h=True,
        )
        assert result is None
