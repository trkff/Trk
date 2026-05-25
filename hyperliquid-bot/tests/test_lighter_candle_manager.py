import threading
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
from unittest.mock import MagicMock

from bot.exchanges.lighter_ws import LighterCandleManager


def _mk_client():
    """Mock LighterExchangeClient com get_candles, _client.get_market, e buffer compartilhado."""
    client = MagicMock()
    client._client.get_market.side_effect = lambda a: {"marketId": {"BTC": 0, "ETH": 1}.get(a)}
    client.get_candles.return_value = pd.DataFrame()
    client._candle_buffer = {}
    client._candle_buffer_lock = threading.RLock()
    return client


class TestManagerLifecycle:
    def test_construct_does_not_connect(self):
        mgr = LighterCandleManager(
            client=_mk_client(),
            assets=["BTC", "ETH"],
            intervals=["5m"],
            on_candle_close=MagicMock(),
        )
        assert mgr.intervals == ["5m"]
        assert mgr._assets == ["BTC", "ETH"]

    def test_pause_resume_flags(self):
        mgr = LighterCandleManager(
            client=_mk_client(),
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=MagicMock(),
        )
        assert mgr._paused is False
        mgr.pause()
        assert mgr._paused is True
        mgr.resume()
        assert mgr._paused is False


import json


def _build_mgr(callback=None):
    mgr = LighterCandleManager(
        client=_mk_client(),
        assets=["BTC", "ETH"],
        intervals=["5m"],
        on_candle_close=callback or MagicMock(),
    )
    # Simula que os subscribes já foram feitos
    mgr._subscriptions = {("BTC", "5m"): 0, ("ETH", "5m"): 1}
    return mgr


def _msg(channel: str, candles: list[dict], msg_type: str = "update/candle") -> str:
    return json.dumps({
        "type": msg_type,
        "channel": channel,
        "timestamp": 1700000005000,
        "candles": candles,
    })


class TestOnMessage:
    def test_update_same_t_does_not_emit(self):
        cb = MagicMock()
        mgr = _build_mgr(cb)
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000000000, c=1.0)]))
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000000000, c=2.0)]))
        assert mgr._queue.qsize() == 0  # nenhum evento de close

    def test_update_new_t_enqueues_close(self):
        cb = MagicMock()
        mgr = _build_mgr(cb)
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000000000)]))
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000300000)]))
        assert mgr._queue.qsize() == 1
        item = mgr._queue.get_nowait()
        assert item == ("BTC", "5m")

    def test_subscribed_snapshot_does_not_emit(self):
        cb = MagicMock()
        mgr = _build_mgr(cb)
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000000000)], msg_type="subscribed/candle"))
        assert mgr._queue.qsize() == 0
        # Mas o buffer deve estar populado
        assert ("BTC", "5m") in mgr._client._candle_buffer
        assert len(mgr._client._candle_buffer[("BTC", "5m")]) == 1

    def test_unknown_channel_ignored(self):
        cb = MagicMock()
        mgr = _build_mgr(cb)
        # Channel ticker/ não está em _subscriptions → ignora silenciosamente
        mgr._on_message(None, _msg("ticker:0", [_row(1700000000000)]))
        assert mgr._queue.qsize() == 0

    def test_dedup_no_duplicate_emit_same_close(self):
        # Se o WS reenviar o mesmo close por algum motivo, não duplica
        cb = MagicMock()
        mgr = _build_mgr(cb)
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000000000)]))
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000300000)]))
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000300000)]))  # repete
        assert mgr._queue.qsize() == 1  # ainda 1

    def test_ping_triggers_pong_reply(self):
        # Lighter manda {"type":"ping"} a nível de aplicação; precisamos
        # responder {"type":"pong"} ou o servidor fecha após ~2min de "inatividade".
        cb = MagicMock()
        mgr = _build_mgr(cb)
        ws = MagicMock()
        mgr._on_message(ws, json.dumps({"type": "ping"}))
        ws.send.assert_called_once_with(json.dumps({"type": "pong"}))
        # ping não deve gerar evento de candle
        assert mgr._queue.qsize() == 0


class TestStart:
    def test_start_seeds_and_spawns_threads(self, monkeypatch):
        cb = MagicMock()
        client = _mk_client()
        # get_candles returns translated columns (timestamp, open, high, low, close, volume)
        # with a datetime index — same shape as _apply_candle_update output
        raw = {"timestamp": 1700000000000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}
        sample_df = pd.DataFrame([raw])
        sample_df["datetime"] = pd.to_datetime(sample_df["timestamp"], unit="ms", utc=True)
        sample_df.set_index("datetime", inplace=True)
        client.get_candles.return_value = sample_df

        # Stub do WebSocketApp para não conectar de verdade.
        # run_forever precisa bloquear até stop() ser chamado para que
        # _ws_thread fique vivo durante as asserções.
        import threading as _threading
        import bot.exchanges.lighter_ws as mod
        _ws_block = _threading.Event()
        ws_app_mock = MagicMock()
        ws_app_mock.run_forever = lambda **kw: _ws_block.wait()
        ws_app_mock.close = lambda: _ws_block.set()
        monkeypatch.setattr(mod.websocket, "WebSocketApp", lambda *a, **kw: ws_app_mock)

        mgr = LighterCandleManager(
            client=client,
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=cb,
        )
        mgr.start()
        try:
            # subscribes registrados
            assert ("BTC", "5m") in mgr._subscriptions
            # buffer seedado (sample_df foi consumido)
            assert ("BTC", "5m") in client._candle_buffer
            # threads vivas
            assert mgr._ws_thread is not None and mgr._ws_thread.is_alive()
            assert mgr._worker_thread is not None and mgr._worker_thread.is_alive()
            assert mgr._watchdog_thread is not None and mgr._watchdog_thread.is_alive()
            assert mgr._boundary_thread is not None and mgr._boundary_thread.is_alive()
        finally:
            mgr.stop()


class TestSharedBuffer:
    def test_on_message_writes_to_client_buffer(self):
        client = _mk_client()
        # client._candle_buffer e _candle_buffer_lock devem existir no mock
        client._candle_buffer = {}
        client._candle_buffer_lock = threading.RLock()

        mgr = LighterCandleManager(
            client=client,
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=MagicMock(),
        )
        mgr._subscriptions = {("BTC", "5m"): 0}

        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000000000, c=1.5)]))

        key = ("BTC", "5m")
        assert key in client._candle_buffer
        assert client._candle_buffer[key].iloc[-1]["close"] == 1.5

    def test_get_candles_reads_from_client_buffer(self):
        client = _mk_client()
        client._candle_buffer = {}
        client._candle_buffer_lock = threading.RLock()

        mgr = LighterCandleManager(
            client=client,
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=MagicMock(),
        )
        mgr._subscriptions = {("BTC", "5m"): 0}
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000000000)]))
        mgr._on_message(None, _msg("candle:0:5m", [_row(1700000300000)]))

        df = mgr.get_candles("BTC", "5m", count=10)
        assert len(df) == 2


class TestBoundaryFallback:
    def test_silent_channel_triggers_rest_and_emits(self):
        # Canal WS sem nenhuma mensagem recente (channel_last_msg_ts antigo) →
        # boundary fallback dispara REST e enfileira close.
        cb = MagicMock()
        client = _mk_client()
        new_row_df = pd.DataFrame([{
            "timestamp": 1700000300000,
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 99.0, "volume": 0.0,
        }])
        new_row_df["datetime"] = pd.to_datetime(new_row_df["timestamp"], unit="ms", utc=True)
        new_row_df.set_index("datetime", inplace=True)
        client.get_candles.return_value = new_row_df

        mgr = LighterCandleManager(
            client=client,
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=cb,
        )
        mgr._subscriptions = {("BTC", "5m"): 0}
        # WS silente: nenhuma mensagem recente (channel_last_msg_ts ausente = 0)
        # Já que o threshold é 10s, time.time() - 0 >> 10 → silente.

        boundary_ms = 1700000300000
        mgr._check_boundary_fallback(boundary_ms, "5m")

        client.get_candles.assert_called()
        assert mgr._queue.qsize() == 1

    def test_active_channel_skips_rest(self):
        # WS empurrando mensagens recentes para o canal (mesmo que sejam updates
        # do candle antigo) → não dispara fallback. WS está vivo, vai emitir close
        # via _on_message quando a primeira trade do novo candle chegar.
        import time as _time
        cb = MagicMock()
        client = _mk_client()
        mgr = LighterCandleManager(
            client=client,
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=cb,
        )
        mgr._subscriptions = {("BTC", "5m"): 0}
        # Sinaliza que canal recebeu mensagem agora (dentro do threshold)
        mgr._channel_last_msg_ts[("BTC", "5m")] = _time.time()

        boundary_ms = 1700000300000
        mgr._check_boundary_fallback(boundary_ms, "5m")

        client.get_candles.assert_not_called()
        assert mgr._queue.qsize() == 0

    def test_already_emitted_skips_rest(self):
        # _on_message já emitiu o close via WS push do novo t → boundary fallback
        # respeita o dedup e não chama REST.
        cb = MagicMock()
        client = _mk_client()
        mgr = LighterCandleManager(
            client=client,
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=cb,
        )
        mgr._subscriptions = {("BTC", "5m"): 0}
        boundary_ms = 1700000300000
        prev_t = boundary_ms - _INTERVAL_MS["5m"]
        mgr._last_emitted_t = {("BTC", "5m"): prev_t}
        # canal silente (channel_last_msg_ts ausente) mas já emitido → skip
        mgr._check_boundary_fallback(boundary_ms, "5m")

        client.get_candles.assert_not_called()
        assert mgr._queue.qsize() == 0


class TestUpdateAssets:
    def test_add_new_asset_registers_subscription(self):
        client = _mk_client()
        mgr = LighterCandleManager(
            client=client,
            assets=["BTC"],
            intervals=["5m"],
            on_candle_close=MagicMock(),
        )
        # Simula estado pós-start parcial: BTC já subscrito
        mgr._subscriptions = {("BTC", "5m"): 0}
        mgr._ws = MagicMock()

        mgr.update_assets(["BTC", "ETH"])

        assert ("ETH", "5m") in mgr._subscriptions
        # ws.send foi chamado com subscribe de ETH
        sent = [json.loads(call.args[0]) for call in mgr._ws.send.call_args_list]
        assert any(s.get("channel") == "candle/1/5m" for s in sent)

    def test_remove_asset_unsubscribes(self):
        client = _mk_client()
        mgr = LighterCandleManager(
            client=client,
            assets=["BTC", "ETH"],
            intervals=["5m"],
            on_candle_close=MagicMock(),
        )
        mgr._subscriptions = {("BTC", "5m"): 0, ("ETH", "5m"): 1}
        mgr._ws = MagicMock()

        mgr.update_assets(["BTC"])

        assert ("ETH", "5m") not in mgr._subscriptions
        sent = [json.loads(call.args[0]) for call in mgr._ws.send.call_args_list]
        assert any(s.get("type") == "unsubscribe" and s.get("channel") == "candle/1/5m" for s in sent)
