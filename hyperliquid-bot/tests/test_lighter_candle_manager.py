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
