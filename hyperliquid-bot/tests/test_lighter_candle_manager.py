import pytest

from bot.exchanges.lighter_ws import _parse_channel, _next_boundary_ms, _INTERVAL_MS


class TestParseChannel:
    def test_candle_channel_with_colon(self):
        # Lighter envia "candle:0:5m" no payload
        assert _parse_channel("candle:0:5m") == (0, "5m")

    def test_candle_channel_with_slash(self):
        # Subscribe usa "candle/0/5m"; parser deve aceitar ambos
        assert _parse_channel("candle/0/5m") == (0, "5m")

    def test_invalid_channel_returns_none(self):
        assert _parse_channel("trade:0") is None
        assert _parse_channel("garbage") is None
        assert _parse_channel("candle:abc:5m") is None

    def test_unknown_resolution_returns_none(self):
        assert _parse_channel("candle:0:7s") is None


class TestNextBoundary:
    def test_5m_boundary(self):
        # 12:03:45 (5m boundaries: 12:00, 12:05) → próximo é 12:05:00
        now_ms = 1700000625_000  # arbitrário
        tf_ms = _INTERVAL_MS["5m"]
        nxt = _next_boundary_ms(now_ms, "5m")
        assert nxt > now_ms
        assert nxt % tf_ms == 0
        assert nxt - now_ms <= tf_ms

    def test_exactly_on_boundary(self):
        # se now já é múltiplo de tf, próximo é now + tf
        tf_ms = _INTERVAL_MS["1h"]
        aligned = 1700000000000 - (1700000000000 % tf_ms)
        assert _next_boundary_ms(aligned, "1h") == aligned + tf_ms

    def test_unknown_interval_raises(self):
        with pytest.raises(KeyError):
            _next_boundary_ms(0, "7s")


import pandas as pd

from bot.exchanges.lighter_ws import _apply_candle_update


def _row(t, o=1.0, h=1.0, low=1.0, c=1.0, v=0.0):
    return {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v}


class TestApplyCandleUpdate:
    def test_empty_buffer_first_update(self):
        df, emitted = _apply_candle_update(pd.DataFrame(), _row(1700000000000))
        assert len(df) == 1
        assert df.iloc[-1]["timestamp"] == 1700000000000
        assert emitted is False  # primeira vela não emite evento (não há "anterior")

    def test_same_t_updates_in_place(self):
        df = pd.DataFrame()
        df, _ = _apply_candle_update(df, _row(1700000000000, c=1.0))
        df, emitted = _apply_candle_update(df, _row(1700000000000, c=2.5))
        assert len(df) == 1  # ainda 1 linha
        assert df.iloc[-1]["close"] == 2.5  # close atualizado
        assert emitted is False

    def test_new_t_emits_close_event(self):
        df = pd.DataFrame()
        df, _ = _apply_candle_update(df, _row(1700000000000))
        df, emitted = _apply_candle_update(df, _row(1700000300000))  # +5m
        assert len(df) == 2
        assert emitted is True  # anterior fechou

    def test_out_of_order_update_ignored(self):
        # Lighter pode reordenar em casos raros — t < último não deve corromper
        df = pd.DataFrame()
        df, _ = _apply_candle_update(df, _row(1700000300000))
        df, emitted = _apply_candle_update(df, _row(1700000000000))
        assert len(df) == 1
        assert df.iloc[-1]["timestamp"] == 1700000300000
        assert emitted is False
