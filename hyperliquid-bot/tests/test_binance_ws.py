import json
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from bot.exchanges.binance_ws import BinanceCandleManager


def _make_df(timestamps_ms: list[int]) -> pd.DataFrame:
    rows = [{"timestamp": t, "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0, "volume": 1.0} for t in timestamps_ms]
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("datetime", inplace=True)
    return df


def _make_manager():
    return BinanceCandleManager(assets=["BTC"], on_candle_close=lambda a, i: None)


class TestBuffer:
    def test_get_candles_returns_from_buffer(self):
        mgr = _make_manager()
        df = _make_df([1000, 2000, 3000])
        with mgr._lock:
            mgr._buffer["BTC"] = {"5m": df}
        result = mgr.get_candles("BTC", "5m", count=2)
        assert len(result) == 2
        assert list(result["timestamp"]) == [2000, 3000]

    def test_get_candles_fallback_rest_when_empty(self):
        mgr = _make_manager()
        df = _make_df([1000, 2000])
        with patch("bot.exchanges.binance_ws.fetch_binance_candles", return_value=df) as mock_rest:
            result = mgr.get_candles("BTC", "5m", count=100)
        mock_rest.assert_called_once_with("BTC", "5m", 100)
        assert len(result) == 2

    def test_get_candles_fallback_rest_when_asset_missing(self):
        mgr = _make_manager()
        df = _make_df([1000])
        with patch("bot.exchanges.binance_ws.fetch_binance_candles", return_value=df):
            result = mgr.get_candles("ETH", "5m")
        assert not result.empty


class TestSeed:
    def test_seed_populates_buffer_for_all_intervals(self):
        # Manager with all intervals explicitly — verifies seed covers whatever is requested
        all_intervals = ["5m", "15m", "1h", "4h", "1d"]
        mgr = BinanceCandleManager(assets=["BTC"], on_candle_close=lambda a, i: None,
                                   intervals=all_intervals)

        def fake_fetch(asset, interval, count):
            return _make_df([count, count + 1])

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", side_effect=fake_fetch):
            mgr._seed_buffer()

        for interval in all_intervals:
            assert "BTC" in mgr._buffer
            assert interval in mgr._buffer["BTC"]
            assert not mgr._buffer["BTC"][interval].empty

    def test_seed_only_requested_intervals(self):
        # Default manager only seeds 5m
        mgr = _make_manager()

        def fake_fetch(asset, interval, count):
            return _make_df([count, count + 1])

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", side_effect=fake_fetch):
            mgr._seed_buffer()

        assert "5m" in mgr._buffer["BTC"]
        for interval in ["15m", "1h", "4h", "1d"]:
            assert interval not in mgr._buffer.get("BTC", {})

    def test_seed_retries_on_rest_failure(self):
        mgr = _make_manager()
        df = _make_df([1000])
        call_count = {"5m": 0}

        def flaky_fetch(asset, interval, count):
            if interval == "5m":
                call_count["5m"] += 1
                if call_count["5m"] < 3:
                    raise Exception("network error")
            return df

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", side_effect=flaky_fetch):
            with patch("time.sleep"):
                mgr._seed_buffer()

        assert call_count["5m"] == 3
        assert "BTC" in mgr._buffer
        assert "5m" in mgr._buffer["BTC"]

    def test_get_candles_empty_rest_not_cached(self):
        mgr = _make_manager()
        with patch("bot.exchanges.binance_ws.fetch_binance_candles", return_value=pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )):
            result = mgr.get_candles("BTC", "5m", count=100)
        assert result.empty
        assert "BTC" not in mgr._buffer or "5m" not in mgr._buffer.get("BTC", {})


class TestStreamUrl:
    def test_single_asset_single_interval(self):
        from bot.exchanges.binance_ws import _build_stream_url
        url = _build_stream_url(["BTC"], ["5m"])
        assert "btcusdt@kline_5m" in url
        assert url.startswith("wss://stream.binance.com")

    def test_multiple_assets_intervals(self):
        from bot.exchanges.binance_ws import _build_stream_url
        url = _build_stream_url(["BTC", "ETH"], ["5m", "1h"])
        assert "btcusdt@kline_5m" in url
        assert "ethusdt@kline_5m" in url
        assert "btcusdt@kline_1h" in url
        assert "ethusdt@kline_1h" in url


class TestParseKlineEvent:
    def _event(self, symbol="BTCUSDT", interval="5m", is_closed=True, close="76789.0"):
        return json.dumps({
            "stream": f"{symbol.lower()}@kline_{interval}",
            "data": {
                "e": "kline",
                "s": symbol,
                "k": {
                    "t": 1700000000000,
                    "o": "76700.0", "h": "76900.0", "l": "76600.0",
                    "c": close, "v": "100.0",
                    "i": interval,
                    "x": is_closed,
                }
            }
        })

    def test_parses_closed_candle(self):
        from bot.exchanges.binance_ws import _parse_kline_event
        asset, interval, row, is_closed = _parse_kline_event(self._event())
        assert asset == "BTC"
        assert interval == "5m"
        assert is_closed is True
        assert row["close"] == 76789.0
        assert row["timestamp"] == 1700000000000

    def test_parses_open_candle(self):
        from bot.exchanges.binance_ws import _parse_kline_event
        _, _, _, is_closed = _parse_kline_event(self._event(is_closed=False))
        assert is_closed is False

    def test_returns_none_on_bad_message(self):
        from bot.exchanges.binance_ws import _parse_kline_event
        result = _parse_kline_event('{"not": "kline"}')
        assert result is None


class TestOnMessage:
    def _closed_event(self, asset="BTC", interval="5m", ts=1700000000000):
        return json.dumps({
            "stream": f"{asset.lower()}usdt@kline_{interval}",
            "data": {
                "e": "kline", "s": f"{asset}USDT",
                "k": {
                    "t": ts, "o": "1.0", "h": "1.0", "l": "1.0",
                    "c": "2.0", "v": "10.0", "i": interval, "x": True,
                }
            }
        })

    def test_closed_candle_updates_buffer(self):
        mgr = _make_manager()
        mgr._buffer["BTC"] = {"5m": _make_df([999000])}
        mgr._on_message(None, self._closed_event("BTC", "5m", 1700000000000))
        with mgr._lock:
            df = mgr._buffer["BTC"]["5m"]
        assert 1700000000000 in df["timestamp"].values

    def test_closed_candle_enqueues_event(self):
        mgr = _make_manager()
        mgr._buffer["BTC"] = {"5m": pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )}
        mgr._on_message(None, self._closed_event("BTC", "5m"))
        assert not mgr._queue.empty()
        asset, interval = mgr._queue.get_nowait()
        assert asset == "BTC"
        assert interval == "5m"

    def test_open_candle_does_not_enqueue(self):
        mgr = _make_manager()
        msg = json.dumps({
            "stream": "btcusdt@kline_5m",
            "data": {
                "e": "kline", "s": "BTCUSDT",
                "k": {"t": 1, "o": "1", "h": "1", "l": "1",
                      "c": "1", "v": "1", "i": "5m", "x": False}
            }
        })
        mgr._on_message(None, msg)
        assert mgr._queue.empty()

    def test_queue_full_discards_event(self):
        mgr = _make_manager()
        mgr._buffer["BTC"] = {"5m": _make_df([1])}
        # Fill the queue
        for _ in range(50):
            mgr._queue.put(("BTC", "5m"))
        # This should not raise
        mgr._on_message(None, self._closed_event("BTC", "5m", 9999))
        assert mgr._queue.full()


class TestWorker:
    def test_worker_calls_callback_only_for_5m(self):
        called = []
        mgr = BinanceCandleManager(assets=["BTC"], on_candle_close=lambda a, i: called.append((a, i)))
        mgr._queue.put(("BTC", "5m"))
        mgr._queue.put(("BTC", "1h"))   # deve ser ignorado
        mgr._queue.put(("BTC", "5m"))
        # Processar manualmente sem thread
        mgr._process_queue_once()
        mgr._process_queue_once()
        mgr._process_queue_once()
        assert called == [("BTC", "5m"), ("BTC", "5m")]

    def test_resume_drains_stale_events(self):
        mgr = _make_manager()
        for _ in range(5):
            mgr._queue.put(("BTC", "5m"))
        mgr.resume()
        assert mgr._queue.empty()
        assert mgr._paused is False

    def test_pause_sets_paused_flag(self):
        mgr = _make_manager()
        mgr.pause()
        assert mgr._paused is True


class TestReseedOverlap:
    def test_reseed_merges_and_deduplicates(self):
        mgr = _make_manager()
        existing = _make_df([1000, 2000, 3000])
        # fresh overlaps com ts=3000 e adiciona ts=4000
        fresh = _make_df([3000, 4000])
        with mgr._lock:
            mgr._buffer["BTC"] = {"5m": existing}

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", return_value=fresh):
            mgr._reseed_with_overlap("BTC", "5m")

        with mgr._lock:
            result = mgr._buffer["BTC"]["5m"]
        assert len(result) == 4
        assert list(result["timestamp"]) == [1000, 2000, 3000, 4000]

    def test_reseed_survives_rest_failure(self):
        mgr = _make_manager()
        existing = _make_df([1000, 2000])
        with mgr._lock:
            mgr._buffer["BTC"] = {"5m": existing}

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", side_effect=Exception("fail")):
            mgr._reseed_with_overlap("BTC", "5m")  # não deve levantar

        with mgr._lock:
            assert len(mgr._buffer["BTC"]["5m"]) == 2  # buffer intacto


class TestUpdateAssets:
    def test_update_assets_seeds_new_asset(self):
        mgr = _make_manager()  # assets=["BTC"]
        df = _make_df([1000])

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", return_value=df):
            with patch.object(mgr, "_reconnect"):
                mgr.update_assets(["BTC", "ETH"])

        assert "ETH" in mgr._assets
        with mgr._lock:
            assert "ETH" in mgr._buffer
