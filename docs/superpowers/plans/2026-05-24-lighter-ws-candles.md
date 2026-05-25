# Lighter WebSocket Candle Feed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Substituir o `BinanceCandleManager` no caminho Lighter por um `LighterCandleManager` que consome o canal nativo `candle/{market_id}/{tf}` do WebSocket Lighter, reduzindo latência de candle de 15-30s para <2s.

**Architecture:** Novo módulo `bot/exchanges/lighter_ws.py` com `LighterCandleManager` (threading, mesmo padrão do `BinanceCandleManager`). Conexão WS única em `wss://mainnet.zklighter.elliot.ai/stream` com N subscribes (1 por `(asset, tf)`). Detecção de candle close por mudança do campo `t`; boundary timer + REST fallback cobrem silêncio de WS. Buffer compartilhado com `LighterExchangeClient`. Switch em `main.py` por `selected_exchange`.

**Tech Stack:** Python 3.10+, `websocket-client` (já em uso pelo Binance manager), `threading`, `concurrent.futures.ThreadPoolExecutor`, `pandas`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-05-24-lighter-ws-candles-design.md`

---

## File Structure

**Novo:**
- `hyperliquid-bot/bot/exchanges/lighter_ws.py` — `LighterCandleManager` + helpers puros (parse, boundary calc)
- `hyperliquid-bot/tests/test_lighter_candle_manager.py` — unit + integration tests

**Modificado:**
- `hyperliquid-bot/bot/exchanges/lighter_exchange.py` — remove fallback Binance no `get_candles`; expõe buffer compartilhado
- `hyperliquid-bot/main.py` — switch de manager por `selected_exchange`; flag `use_lighter_ws_candles` para rollout

**Inalterado (escopo):**
- `bot/exchanges/binance_ws.py`, `bot/exchanges/base.py`, `bot/exchanges/hyperliquid.py` — caminho Hyperliquid intacto

---

## Task 1: Scaffold + helpers puros (parser, boundary calculator)

**Files:**
- Create: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Create: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

- [ ] **Step 1: Escrever testes failing dos helpers**

`hyperliquid-bot/tests/test_lighter_candle_manager.py`:
```python
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
```

- [ ] **Step 2: Rodar para confirmar fail por ImportError**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'bot.exchanges.lighter_ws'`

- [ ] **Step 3: Implementar o módulo skeleton**

`hyperliquid-bot/bot/exchanges/lighter_ws.py`:
```python
"""LighterCandleManager — live candle streaming from Lighter WebSocket.

Substitui o BinanceCandleManager no caminho Lighter. Usa o canal nativo
`candle/{market_id}/{resolution}` que empurra updates em batches de 500ms
a cada trade. Detecta candle close por mudança do campo `t` (timestamp).
"""

import re

_WS_URL_MAINNET = "wss://mainnet.zklighter.elliot.ai/stream"
_WS_URL_TESTNET = "wss://testnet.zklighter.elliot.ai/stream"

_INTERVAL_MS: dict[str, int] = {
    "1m":    60_000,
    "5m":    300_000,
    "15m":   900_000,
    "30m":   1_800_000,
    "1h":    3_600_000,
    "4h":    14_400_000,
    "12h":   43_200_000,
    "1d":    86_400_000,
}

_CHANNEL_RE = re.compile(r"^candle[:/](\d+)[:/]([0-9]+[mhd])$")


def _parse_channel(channel: str) -> tuple[int, str] | None:
    """Parse 'candle:0:5m' or 'candle/0/5m' → (market_id, resolution).

    Returns None for unknown channels or unsupported resolutions.
    """
    m = _CHANNEL_RE.match(channel)
    if not m:
        return None
    market_id = int(m.group(1))
    resolution = m.group(2)
    if resolution not in _INTERVAL_MS:
        return None
    return market_id, resolution


def _next_boundary_ms(now_ms: int, interval: str) -> int:
    """Next candle boundary (close timestamp) after now_ms for given interval.

    If now_ms is exactly on a boundary, returns now_ms + tf_ms.
    Raises KeyError for unknown intervals.
    """
    tf_ms = _INTERVAL_MS[interval]
    return ((now_ms // tf_ms) + 1) * tf_ms
```

- [ ] **Step 4: Rodar para confirmar pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (todos os 6 testes)

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter-ws): scaffold module with channel parser and boundary helpers"
```

---

## Task 2: Buffer update + detecção de candle close

**Files:**
- Modify: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Modify: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

Implementa o coração da detecção: quando um update chega, decide se é "mesma vela em formação" (update OHLC, sem evento) ou "nova vela" (anterior fechou, emite evento).

- [ ] **Step 1: Escrever testes failing**

Adicione no `tests/test_lighter_candle_manager.py`:
```python
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
```

- [ ] **Step 2: Rodar — confirma fail**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py::TestApplyCandleUpdate -v`
Expected: FAIL com `ImportError: cannot import name '_apply_candle_update'`

- [ ] **Step 3: Implementar `_apply_candle_update`**

Adicione em `bot/exchanges/lighter_ws.py`:
```python
import pandas as pd


def _candle_payload_to_row(c: dict) -> dict:
    """Convert Lighter candle dict (t/o/h/l/c/v) to internal row format."""
    return {
        "timestamp": int(c["t"]),
        "open":      float(c["o"]),
        "high":      float(c["h"]),
        "low":       float(c["l"]),
        "close":     float(c["c"]),
        "volume":    float(c["v"]),
    }


def _apply_candle_update(
    buffer: pd.DataFrame,
    candle: dict,
) -> tuple[pd.DataFrame, bool]:
    """Merge an incoming Lighter candle into the buffer.

    Returns (new_buffer, emitted_close_event):
    - `emitted_close_event = True` when the incoming `t` is greater than the
      last `t` in the buffer (a new candle started, so the previous one closed).
    - `False` for the very first candle (nothing to close yet), for same-t
      updates (in-place OHLC refresh), and for out-of-order updates (ignored).
    """
    row = _candle_payload_to_row(candle)
    if buffer.empty:
        new_buf = pd.DataFrame([row])
        new_buf["datetime"] = pd.to_datetime(new_buf["timestamp"], unit="ms", utc=True)
        new_buf.set_index("datetime", inplace=True)
        return new_buf, False

    last_t = int(buffer.iloc[-1]["timestamp"])
    incoming_t = row["timestamp"]

    if incoming_t < last_t:
        # out-of-order: ignora
        return buffer, False

    if incoming_t == last_t:
        # mesma vela em formação: substitui OHLCV in-place
        new_buf = buffer.copy()
        for col in ("open", "high", "low", "close", "volume"):
            new_buf.iloc[-1, new_buf.columns.get_loc(col)] = row[col]
        return new_buf, False

    # incoming_t > last_t: nova vela → anterior fechou
    new_row = pd.DataFrame([row])
    new_row["datetime"] = pd.to_datetime(new_row["timestamp"], unit="ms", utc=True)
    new_row.set_index("datetime", inplace=True)
    new_buf = pd.concat([buffer, new_row])
    new_buf = new_buf[~new_buf.index.duplicated(keep="last")].sort_index()
    return new_buf, True
```

- [ ] **Step 4: Rodar — confirma pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (10 testes total agora)

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter-ws): add candle update applier with close-event detection"
```

---

## Task 3: Classe `LighterCandleManager` — esqueleto + start/stop

**Files:**
- Modify: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Modify: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

Cria a classe com construtor, `start`/`stop`/`pause`/`resume`, e métodos públicos vazios. Não conecta ao WS ainda (próxima task). Permite injetar dependências via DI para teste sem rede.

- [ ] **Step 1: Escrever testes failing**

Adicione no `tests/test_lighter_candle_manager.py`:
```python
from unittest.mock import MagicMock

from bot.exchanges.lighter_ws import LighterCandleManager


def _mk_client():
    """Mock LighterExchangeClient com get_candles e _client.get_market."""
    client = MagicMock()
    client._client.get_market.side_effect = lambda a: {"marketId": {"BTC": 0, "ETH": 1}.get(a)}
    client.get_candles.return_value = pd.DataFrame()
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
```

- [ ] **Step 2: Rodar — confirma fail**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py::TestManagerLifecycle -v`
Expected: FAIL com `ImportError: cannot import name 'LighterCandleManager'`

- [ ] **Step 3: Implementar a classe**

Adicione em `bot/exchanges/lighter_ws.py`:
```python
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from bot.logger import get_logger

log = get_logger(__name__)

_QUEUE_MAXSIZE = 50
_SEED_COUNT = 500


class LighterCandleManager:
    """Live candle streaming from Lighter native WebSocket.

    Threading model (matches BinanceCandleManager):
    - _ws_thread:        WebSocket reader, parses messages, updates buffer
    - _worker_thread:    drains queue and dispatches to thread pool
    - _watchdog_thread:  monitors global silence, reconnects
    - _boundary_thread:  fires per-TF boundary REST fallback for silent channels

    Callback signature: on_candle_close(asset: str, interval: str) -> None
    """

    def __init__(
        self,
        client,
        assets: list[str],
        on_candle_close: Callable[[str, str], None],
        intervals: list[str] | None = None,
        ws_url: str = _WS_URL_MAINNET,
    ):
        self._client = client
        self._assets = list(assets)
        self._on_candle_close = on_candle_close
        self._intervals: list[str] = list(intervals) if intervals else ["5m"]
        self._ws_url = ws_url

        self._buffer: dict[tuple[str, str], pd.DataFrame] = {}
        self._lock = threading.RLock()

        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._paused = False
        self._stop_event = threading.Event()

        self._ws = None
        self._last_msg_ts: float = 0.0
        self._ts_lock = threading.Lock()
        self._last_update_ms: dict[tuple[str, str], int] = {}

        # subscriptions: (asset, tf) → market_id
        self._subscriptions: dict[tuple[str, str], int] = {}

        self._ws_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._boundary_thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="lighter-asset-worker")

    @property
    def intervals(self) -> list[str]:
        return list(self._intervals)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._paused = False

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._executor.shutdown(wait=False)
        log.info("LighterCandleManager: stopped.")

    def start(self) -> None:
        """Wired in Task 5 — seeds buffer, starts WS + worker + watchdog + boundary threads."""
        raise NotImplementedError("Wired in Task 5")

    def update_assets(self, new_assets: list[str]) -> None:
        """Wired in Task 8."""
        raise NotImplementedError("Wired in Task 8")

    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
        """Read last `count` candles from buffer. Wired in Task 6."""
        raise NotImplementedError("Wired in Task 6")
```

- [ ] **Step 4: Rodar — confirma pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (12 testes total)

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter-ws): add LighterCandleManager class skeleton with lifecycle"
```

---

## Task 4: `on_message` handler — parsing + buffer + dedup de emissão

**Files:**
- Modify: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Modify: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

Implementa a recepção e processamento de uma mensagem WS, sem precisar de conexão real. Esta é a função que será chamada pelo `websocket-client` via `on_message=self._on_message`.

- [ ] **Step 1: Escrever testes failing**

Adicione no `tests/test_lighter_candle_manager.py`:
```python
import json
from unittest.mock import MagicMock


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
        assert ("BTC", "5m") in mgr._buffer
        assert len(mgr._buffer[("BTC", "5m")]) == 1

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
```

- [ ] **Step 2: Rodar — confirma fail por AttributeError**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py::TestOnMessage -v`
Expected: FAIL com `AttributeError: '...' object has no attribute '_on_message'`

- [ ] **Step 3: Implementar `_on_message` e `_market_to_asset`**

Adicione em `bot/exchanges/lighter_ws.py` (dentro da classe `LighterCandleManager`):
```python
import json
import time


    def _market_to_asset(self, market_id: int, interval: str) -> str | None:
        """Reverse lookup market_id → asset via _subscriptions map."""
        for (asset, tf), mid in self._subscriptions.items():
            if mid == market_id and tf == interval:
                return asset
        return None

    def _on_message(self, ws, raw: str) -> None:
        with self._ts_lock:
            self._last_msg_ts = time.time()

        try:
            msg = json.loads(raw)
        except Exception:
            return

        msg_type = msg.get("type", "")
        if not msg_type.endswith("/candle"):
            return

        channel = msg.get("channel", "")
        parsed = _parse_channel(channel)
        if parsed is None:
            return
        market_id, interval = parsed

        asset = self._market_to_asset(market_id, interval)
        if asset is None:
            return  # canal recebido mas não subscrito (race ou bug)

        candles = msg.get("candles") or []
        if not candles:
            return

        key = (asset, interval)
        is_snapshot = msg_type == "subscribed/candle"

        # Para snapshot inicial pode vir múltiplas velas; aplica todas sem emitir
        # evento. Para update, sempre processa a última.
        candles_to_apply = candles if is_snapshot else [candles[-1]]

        for c in candles_to_apply:
            with self._lock:
                buf = self._buffer.get(key, pd.DataFrame())
                new_buf, emitted_close = _apply_candle_update(buf, c)
                self._buffer[key] = new_buf

            self._last_update_ms[key] = int(c["t"])

            if is_snapshot:
                continue  # snapshot nunca emite

            if emitted_close and not self._paused:
                # dedup: só emite se ainda não emitimos esse close
                last_emitted = getattr(self, "_last_emitted_t", {}).get(key, 0)
                # O close é da vela ANTERIOR. last_emitted guarda o t da vela
                # cujo close já anunciamos. Se o t anterior (= incoming - tf) for
                # > last_emitted, ainda não anunciamos esse close.
                prev_t = int(c["t"]) - _INTERVAL_MS[interval]
                if prev_t > last_emitted:
                    if not hasattr(self, "_last_emitted_t"):
                        self._last_emitted_t: dict[tuple[str, str], int] = {}
                    self._last_emitted_t[key] = prev_t
                    try:
                        self._queue.put_nowait((asset, interval))
                    except queue.Full:
                        pass
```

- [ ] **Step 4: Rodar — confirma pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (17 testes total)

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter-ws): implement on_message with parse/buffer/dedup"
```

---

## Task 5: Conexão WS real + seed + threads (`start`)

**Files:**
- Modify: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Modify: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

Wire up real connection: seed via REST, abrir WS, mandar subscribes, iniciar threads.

- [ ] **Step 1: Escrever teste failing (apenas que `start` chama seed e abre thread)**

Adicione no `tests/test_lighter_candle_manager.py`:
```python
class TestStart:
    def test_start_seeds_and_spawns_threads(self, monkeypatch):
        cb = MagicMock()
        client = _mk_client()
        sample_df = pd.DataFrame([_row(1700000000000)])
        sample_df["datetime"] = pd.to_datetime(sample_df["timestamp"], unit="ms", utc=True)
        sample_df.set_index("datetime", inplace=True)
        client.get_candles.return_value = sample_df

        # Stub do WebSocketApp para não conectar de verdade
        import bot.exchanges.lighter_ws as mod
        ws_app_mock = MagicMock()
        ws_app_mock.run_forever = lambda **kw: None  # retorna imediatamente
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
            assert ("BTC", "5m") in mgr._buffer
            # threads vivas
            assert mgr._ws_thread is not None and mgr._ws_thread.is_alive()
            assert mgr._worker_thread is not None and mgr._worker_thread.is_alive()
            assert mgr._watchdog_thread is not None and mgr._watchdog_thread.is_alive()
            assert mgr._boundary_thread is not None and mgr._boundary_thread.is_alive()
        finally:
            mgr.stop()
```

- [ ] **Step 2: Rodar — confirma fail (`NotImplementedError`)**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py::TestStart -v`
Expected: FAIL com `NotImplementedError: Wired in Task 5`

- [ ] **Step 3: Implementar `start`, `_seed_buffer`, `_run_ws`, loops das threads**

Adicione/substitua em `bot/exchanges/lighter_ws.py`:

```python
import websocket  # websocket-client (já é dep do binance_ws)


    def _resolve_market_id(self, asset: str) -> int | None:
        try:
            market = self._client._client.get_market(asset)
        except Exception as e:
            log.warning(f"[{asset}] market lookup failed: {e}")
            return None
        if not market:
            return None
        return market.get("marketId")

    def _seed_buffer(self) -> None:
        """Cold start: build (asset, tf) → market_id map and prefill buffers via REST."""
        for asset in self._assets:
            market_id = self._resolve_market_id(asset)
            if market_id is None:
                log.warning(f"[{asset}] no Lighter market — skipping subscribe")
                continue
            for tf in self._intervals:
                self._subscriptions[(asset, tf)] = market_id
                try:
                    df = self._client.get_candles(asset, tf, count=_SEED_COUNT)
                    if df is not None and not df.empty:
                        with self._lock:
                            self._buffer[(asset, tf)] = df.copy()
                        last_ts = int(df.iloc[-1]["timestamp"]) if "timestamp" in df.columns else 0
                        self._last_update_ms[(asset, tf)] = last_ts
                except Exception as e:
                    log.warning(f"[{asset}] seed {tf} failed: {e}")

    def _on_open(self, ws) -> None:
        """Send subscribes for all (asset, tf) pairs in self._subscriptions."""
        for (asset, tf), market_id in self._subscriptions.items():
            sub = {"type": "subscribe", "channel": f"candle/{market_id}/{tf}"}
            try:
                ws.send(json.dumps(sub))
            except Exception as e:
                log.warning(f"[{asset}] subscribe {tf} failed: {e}")

    def _on_error(self, ws, error) -> None:
        log.error(f"Lighter WS error: {error}")

    def _on_close(self, ws, code, msg) -> None:
        log.warning(f"Lighter WS closed (code={code})")

    def _run_ws(self) -> None:
        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(ping_interval=90, ping_timeout=15)

    def _safe_callback(self, asset: str, interval: str) -> None:
        try:
            self._on_candle_close(asset, interval)
        except Exception as e:
            log.error(f"[{asset}] on_candle_close error: {e}", exc_info=True)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                first = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            if self._paused:
                continue
            items = [first]
            while True:
                try:
                    items.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            for asset, interval in items:
                self._executor.submit(self._safe_callback, asset, interval)

    def _watchdog_loop(self) -> None:
        """Reconnect if WS silent for >90s (Task 7 expands this)."""
        while not self._stop_event.is_set():
            if self._stop_event.wait(30):
                break
            with self._ts_lock:
                elapsed = time.time() - self._last_msg_ts
            if elapsed > 90:
                log.warning(f"Lighter WS silent for {elapsed:.0f}s — reconnect")
                self._reconnect()

    def _boundary_loop(self) -> None:
        """Per-TF backup: REST fetch for subscribed (asset, tf) that received
        no WS update in the last completed boundary window.
        Wired fully in Task 7 (basic loop here so the thread is alive).
        """
        while not self._stop_event.is_set():
            if self._stop_event.wait(30):
                break

    def _reconnect(self) -> None:
        """Close current WS and respawn thread. Resubscribes via _on_open."""
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        time.sleep(1)
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True, name="lighter-ws")
        self._ws_thread.start()
        with self._ts_lock:
            self._last_msg_ts = time.time()
        log.info("LighterCandleManager: reconnected.")

    def start(self) -> None:
        log.info("LighterCandleManager: seeding buffer via REST...")
        self._seed_buffer()
        log.info(f"LighterCandleManager: seed complete ({len(self._subscriptions)} subscriptions). Opening WS...")
        self._stop_event.clear()
        with self._ts_lock:
            self._last_msg_ts = time.time()

        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True, name="lighter-ws")
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="lighter-worker")
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="lighter-watchdog")
        self._boundary_thread = threading.Thread(target=self._boundary_loop, daemon=True, name="lighter-boundary")

        self._ws_thread.start()
        self._worker_thread.start()
        self._watchdog_thread.start()
        self._boundary_thread.start()
        log.info("LighterCandleManager: started.")
```

- [ ] **Step 4: Rodar — confirma pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (18 testes)

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter-ws): wire up start/seed/ws-threads/reconnect"
```

---

## Task 6: `get_candles` lê do buffer + escreve no buffer compartilhado do `LighterExchangeClient`

**Files:**
- Modify: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Modify: `hyperliquid-bot/bot/exchanges/lighter_exchange.py`
- Modify: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

O `LighterCandleManager` precisa escrever no MESMO buffer que `LighterExchangeClient.get_candles` lê — para `process_asset` ver o dado fresco via `client.get_candles()` sem mudar a interface.

Estratégia: o manager recebe o `client` (já tem `_candle_buffer` + `_candle_buffer_lock`). Em vez de manter buffer interno separado, **delega** leitura/escrita pro buffer do client.

- [ ] **Step 1: Escrever teste failing**

Adicione no `tests/test_lighter_candle_manager.py`:
```python
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
```

- [ ] **Step 2: Rodar — confirma fail**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py::TestSharedBuffer -v`
Expected: FAIL — `get_candles` ainda raise `NotImplementedError` e `_on_message` escreve em `self._buffer` em vez do client.

- [ ] **Step 3: Refatorar para usar buffer do client**

Em `bot/exchanges/lighter_ws.py`, substitua TODAS as referências a `self._buffer` e `self._lock` por:
```python
    def _get_buffer(self, key: tuple[str, str]) -> pd.DataFrame:
        with self._client._candle_buffer_lock:
            return self._client._candle_buffer.get(key, pd.DataFrame())

    def _set_buffer(self, key: tuple[str, str], df: pd.DataFrame) -> None:
        with self._client._candle_buffer_lock:
            self._client._candle_buffer[key] = df
```

E ajuste `_on_message` para usar `self._get_buffer(key)` / `self._set_buffer(key, new_buf)` no lugar de `with self._lock: ...`. Remova `self._buffer` e `self._lock` do `__init__` (não são mais necessários).

Em `_seed_buffer`, troque o write direto para `self._set_buffer(key, df.copy())`.

Implemente `get_candles`:
```python
    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
        key = (asset.upper(), interval)
        df = self._get_buffer(key)
        if df.empty:
            return df
        return df.iloc[-count:].copy()
```

Ajuste o mock `_mk_client()` em testes anteriores para conter `_candle_buffer` e `_candle_buffer_lock`:
```python
def _mk_client():
    client = MagicMock()
    client._client.get_market.side_effect = lambda a: {"marketId": {"BTC": 0, "ETH": 1}.get(a)}
    client.get_candles.return_value = pd.DataFrame()
    client._candle_buffer = {}
    client._candle_buffer_lock = threading.RLock()
    return client
```

- [ ] **Step 4: Rodar — confirma pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (20 testes)

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter-ws): share buffer with LighterExchangeClient (no duplication)"
```

---

## Task 7: Boundary timer + watchdog completo (REST fallback para canais silenciosos)

**Files:**
- Modify: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Modify: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

Implementa o backup que cobre ativos sem trade na janela. A cada boundary, verifica quais canais não receberam update e força REST.

- [ ] **Step 1: Escrever teste failing**

Adicione no `tests/test_lighter_candle_manager.py`:
```python
class TestBoundaryFallback:
    def test_silent_channel_triggers_rest_and_emits(self):
        cb = MagicMock()
        client = _mk_client()
        # Simula REST devolvendo uma vela nova
        new_row_df = pd.DataFrame([_row(1700000300000, c=99.0)])
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
        # Última update conhecida = vela 1700000000000 (já fechada faz tempo)
        mgr._last_update_ms[("BTC", "5m")] = 1700000000000

        # Boundary que já passou (todas as velas após 1700000000000 estão silentes)
        boundary_ms = 1700000300000
        mgr._check_boundary_fallback(boundary_ms, "5m")

        # Deve ter chamado REST e enfileirado o close
        client.get_candles.assert_called()
        assert mgr._queue.qsize() == 1

    def test_recent_update_skips_rest(self):
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
        # WS já trouxe update da vela atual (após o boundary)
        mgr._last_update_ms[("BTC", "5m")] = boundary_ms + 1000

        mgr._check_boundary_fallback(boundary_ms, "5m")

        client.get_candles.assert_not_called()
        assert mgr._queue.qsize() == 0
```

- [ ] **Step 2: Rodar — confirma fail**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py::TestBoundaryFallback -v`
Expected: FAIL com `AttributeError: ... no attribute '_check_boundary_fallback'`

- [ ] **Step 3: Implementar `_check_boundary_fallback` e atualizar `_boundary_loop`**

Adicione em `bot/exchanges/lighter_ws.py` (na classe):
```python
_BOUNDARY_MARGIN_MS = 2000   # espera 2s após boundary antes de checar (WS prioridade)


    def _check_boundary_fallback(self, boundary_ms: int, interval: str) -> None:
        """For every subscribed (asset, interval) where _last_update_ms is older
        than `boundary_ms`, force a REST fetch and emit close event if new candle found.
        """
        for (asset, tf), _ in list(self._subscriptions.items()):
            if tf != interval:
                continue
            last = self._last_update_ms.get((asset, tf), 0)
            if last >= boundary_ms:
                continue  # WS já trouxe update mais novo

            try:
                df = self._client.get_candles(asset, tf, count=2)
            except Exception as e:
                log.warning(f"[{asset}] boundary REST fallback failed ({tf}): {e}")
                continue
            if df is None or df.empty:
                continue

            new_last = int(df.iloc[-1]["timestamp"])
            # Atualiza buffer compartilhado completo (REST já devolve OHLC consolidado)
            self._set_buffer((asset, tf), df.copy())
            self._last_update_ms[(asset, tf)] = new_last

            # Emite close se ainda não emitimos a vela anterior
            prev_close = boundary_ms  # o boundary é o "t" da vela que acabou de abrir
            last_emitted = getattr(self, "_last_emitted_t", {}).get((asset, tf), 0)
            prev_t = prev_close - _INTERVAL_MS[tf]
            if prev_t > last_emitted:
                if not hasattr(self, "_last_emitted_t"):
                    self._last_emitted_t = {}
                self._last_emitted_t[(asset, tf)] = prev_t
                try:
                    self._queue.put_nowait((asset, tf))
                    log.info(f"[{asset}] {tf} boundary fallback fired (WS silent)")
                except queue.Full:
                    pass
```

Substitua `_boundary_loop` para realmente disparar:
```python
    def _boundary_loop(self) -> None:
        """For each interval, sleep until next boundary + margin, then check fallback."""
        # Estado: próximo boundary por TF
        next_check: dict[str, int] = {}
        for tf in self._intervals:
            now_ms = int(time.time() * 1000)
            next_check[tf] = _next_boundary_ms(now_ms, tf)

        while not self._stop_event.is_set():
            now_ms = int(time.time() * 1000)
            # Acha o boundary mais próximo
            tf_next = min(next_check.items(), key=lambda x: x[1])
            tf, boundary = tf_next
            wait_ms = (boundary + _BOUNDARY_MARGIN_MS) - now_ms
            if wait_ms > 0:
                if self._stop_event.wait(wait_ms / 1000):
                    break

            try:
                self._check_boundary_fallback(boundary, tf)
            except Exception as e:
                log.error(f"boundary_loop check failed ({tf}): {e}", exc_info=True)

            now_ms = int(time.time() * 1000)
            next_check[tf] = _next_boundary_ms(now_ms, tf)
```

- [ ] **Step 4: Rodar — confirma pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (22 testes)

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter-ws): add boundary timer with REST fallback for silent channels"
```

---

## Task 8: `update_assets` incremental + remover fallback Binance do `LighterExchangeClient.get_candles`

**Files:**
- Modify: `hyperliquid-bot/bot/exchanges/lighter_ws.py`
- Modify: `hyperliquid-bot/bot/exchanges/lighter_exchange.py`
- Modify: `hyperliquid-bot/tests/test_lighter_candle_manager.py`

`update_assets` permite o heartbeat do `main.py` adicionar/remover ativos sem reiniciar o manager. Em paralelo, removemos o fallback Binance do `LighterExchangeClient.get_candles` (parte do escopo do design).

- [ ] **Step 1: Escrever teste failing para update_assets**

Adicione no `tests/test_lighter_candle_manager.py`:
```python
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
```

- [ ] **Step 2: Rodar — confirma fail**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py::TestUpdateAssets -v`
Expected: FAIL com `NotImplementedError: Wired in Task 8`

- [ ] **Step 3: Implementar `update_assets`**

Substitua `update_assets` em `bot/exchanges/lighter_ws.py`:
```python
    def update_assets(self, new_assets: list[str]) -> None:
        """Diff against current subscriptions, send subscribe/unsubscribe accordingly."""
        new_set = set(a.upper() for a in new_assets)
        current_assets = set(a for (a, _) in self._subscriptions.keys())

        # Remove assets no longer wanted
        for asset in current_assets - new_set:
            for tf in self._intervals:
                key = (asset, tf)
                if key not in self._subscriptions:
                    continue
                market_id = self._subscriptions.pop(key)
                if self._ws:
                    msg = {"type": "unsubscribe", "channel": f"candle/{market_id}/{tf}"}
                    try:
                        self._ws.send(json.dumps(msg))
                    except Exception as e:
                        log.warning(f"[{asset}] unsubscribe {tf} failed: {e}")

        # Add new assets
        for asset in new_set - current_assets:
            market_id = self._resolve_market_id(asset)
            if market_id is None:
                log.warning(f"[{asset}] no Lighter market — cannot subscribe")
                continue
            for tf in self._intervals:
                self._subscriptions[(asset, tf)] = market_id
                # Seed via REST so o buffer fica disponível antes do primeiro update
                try:
                    df = self._client.get_candles(asset, tf, count=_SEED_COUNT)
                    if df is not None and not df.empty:
                        self._set_buffer((asset, tf), df.copy())
                        self._last_update_ms[(asset, tf)] = int(df.iloc[-1]["timestamp"])
                except Exception as e:
                    log.warning(f"[{asset}] seed on update_assets failed: {e}")
                if self._ws:
                    msg = {"type": "subscribe", "channel": f"candle/{market_id}/{tf}"}
                    try:
                        self._ws.send(json.dumps(msg))
                    except Exception as e:
                        log.warning(f"[{asset}] subscribe {tf} failed: {e}")

        self._assets = list(new_assets)
```

- [ ] **Step 4: Rodar — confirma pass**

Run: `cd hyperliquid-bot && pytest tests/test_lighter_candle_manager.py -v`
Expected: PASS (24 testes)

- [ ] **Step 5: Remover fallback Binance em `LighterExchangeClient.get_candles`**

Em `hyperliquid-bot/bot/exchanges/lighter_exchange.py`, na função `get_candles` (linhas ~249-296):

Substitua o trecho:
```python
        market = self._client.get_market(asset)
        if not market:
            log.warning(f"[{asset}] Market not found on Lighter, falling back to Binance candles")
            from bot.exchanges.base import fetch_binance_candles
            return fetch_binance_candles(asset, interval, count)
        try:
            raw = self._client.get_candles(market["marketId"], interval, fetch_count)
            ...
        except Exception as e:
            log.warning(f"[{asset}] Lighter candles ({interval}) failed, falling back to Binance: {e}")
            from bot.exchanges.base import fetch_binance_candles
            return fetch_binance_candles(asset, interval, count)
```

Por:
```python
        market = self._client.get_market(asset)
        if not market:
            log.warning(f"[{asset}] Market not found on Lighter, returning empty df")
            return pd.DataFrame()
        try:
            raw = self._client.get_candles(market["marketId"], interval, fetch_count)
            ...  # (mantém todo o bloco try inalterado)
        except Exception as e:
            log.warning(f"[{asset}] Lighter candles ({interval}) failed: {e}")
            return pd.DataFrame()
```

(Mantenha todo o miolo do `try` igual: parsing, `_drop_open_candle`, merge no buffer, return.)

- [ ] **Step 6: Rodar suite completa para garantir que nada quebrou**

Run: `cd hyperliquid-bot && pytest tests/ -v`
Expected: PASS em tudo (inclusive testes existentes do Lighter)

- [ ] **Step 7: Commit**

```bash
git add hyperliquid-bot/bot/exchanges/lighter_ws.py hyperliquid-bot/bot/exchanges/lighter_exchange.py hyperliquid-bot/tests/test_lighter_candle_manager.py
git commit -m "feat(lighter): add update_assets to WS manager; remove Binance fallback from get_candles"
```

---

## Task 9: Wire-up em `main.py` com flag `use_lighter_ws_candles`

**Files:**
- Modify: `hyperliquid-bot/main.py`

Switch que escolhe `LighterCandleManager` ou `BinanceCandleManager` baseado em `selected_exchange` + flag temporária pra rollout seguro.

- [ ] **Step 1: Ler o trecho atual do `main.py`**

Run: `cd hyperliquid-bot && grep -n "BinanceCandleManager\|candle_mgr\|selected_exchange" main.py`

Confirme as linhas 23, 35, 108 (e qualquer outra referência).

- [ ] **Step 2: Editar imports e tipagem**

Em `hyperliquid-bot/main.py` linha 24:

Substitua:
```python
from bot.exchanges.binance_ws import BinanceCandleManager
```

Por:
```python
from bot.exchanges.binance_ws import BinanceCandleManager
from bot.exchanges.lighter_ws import LighterCandleManager
```

Em linha 35:

Substitua:
```python
candle_mgr: BinanceCandleManager | None = None
```

Por:
```python
candle_mgr: BinanceCandleManager | LighterCandleManager | None = None
```

- [ ] **Step 3: Adicionar switch no `bot_loop`**

Em volta da linha 108 (a chamada `candle_mgr = BinanceCandleManager(...)`):

Substitua:
```python
    candle_mgr = BinanceCandleManager(initial_assets, on_candle_close=on_candle_close,
                                      intervals=active_intervals)
    candle_mgr.start()
```

Por:
```python
    selected_exchange = cfg.get("selected_exchange", "lighter")
    use_lighter_ws = (cfg.get("use_lighter_ws_candles", "true").lower() == "true")

    if selected_exchange == "lighter" and use_lighter_ws:
        log.info("Using LighterCandleManager (native WS) for candle feed")
        candle_mgr = LighterCandleManager(
            client=client,
            assets=initial_assets,
            intervals=active_intervals,
            on_candle_close=on_candle_close,
        )
    else:
        log.info(f"Using BinanceCandleManager (exchange={selected_exchange}, ws_flag={use_lighter_ws})")
        candle_mgr = BinanceCandleManager(initial_assets, on_candle_close=on_candle_close,
                                          intervals=active_intervals)
    candle_mgr.start()
```

- [ ] **Step 4: Rodar suite e iniciar bot em paper/testnet**

Run: `cd hyperliquid-bot && pytest tests/ -v`
Expected: PASS

Run manual: `cd hyperliquid-bot && python run.py`
Em outra janela, verifique nos logs (`http://localhost:8080/logs`) que aparece:
- `Using LighterCandleManager (native WS) for candle feed`
- `LighterCandleManager: seeding buffer via REST...`
- `LighterCandleManager: started.`

Aguarde 10 minutos e confirme:
- Logs `[ASSET] <FAMILY> SCAN [instance_name] ...` aparecem a cada 5m boundary (igual antes, mas agora vindos do WS Lighter)
- Latência entre boundary (ex: HH:05:00) e log do SCAN deve ser <2s — comparado aos 15-30s anteriores

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/main.py
git commit -m "feat(main): switch to LighterCandleManager when exchange=lighter"
```

---

## Task 10: Documentar mudanças no `CLAUDE.md`

**Files:**
- Modify: `hyperliquid-bot/CLAUDE.md`

Atualiza a seção "Fonte de candles (live)" e a descrição do `main.py` para refletir o novo caminho. Não documenta o caminho Binance como removido (ele continua no código pro HL).

- [ ] **Step 1: Adicionar entrada na seção "Fonte de candles (live)"**

Em `hyperliquid-bot/CLAUDE.md` na seção "Fonte de candles (live)" (~linha 432), substitua o bullet `Lighter` por:

```markdown
- **Lighter (live)**: candles via WebSocket nativo `wss://mainnet.zklighter.elliot.ai/stream`, canal `candle/{market_id}/{resolution}`. Empurra updates em batches de 500ms a cada trade. Detecção de candle close por mudança do campo `t`. Implementado em `bot/exchanges/lighter_ws.py::LighterCandleManager` (threading: ws + worker + watchdog + boundary). Boundary timer + REST `/api/v1/candles` cobrem casos de silêncio (ativos sem trade na janela ou WS caído). Buffer compartilhado com `LighterExchangeClient._candle_buffer` (manager escreve, `get_candles` lê). Flag `use_lighter_ws_candles` (default `true`) controla o switch — se `false`, usa o `BinanceCandleManager` (modo legado, mantido pra rollback).
- **Lighter (REST fallback)**: `LighterExchangeClient.get_candles` chama `/api/v1/candles` direto quando buffer vazio ou WS inativo. Sem fallback Binance — retorna df vazio em caso de erro (`main.py` já tem retry stale de 360s).
- **Hyperliquid**: candles via Binance Spot REST (`api.binance.com/api/v3/klines`) via `fetch_binance_candles()` em `bot/exchanges/base.py`. `BinanceCandleManager` continua sendo o feed live. **TODO quando voltar a usar HL**: migrar para candles nativos do SDK Hyperliquid (`info.candles_snapshot`).
```

- [ ] **Step 2: Atualizar descrição do `main.py` no topo**

Localize a descrição que começa com `main.py                 <- Bot loop orchestration — event-driven via BinanceCandleManager WebSocket;` (~linha do bloco de arquitetura no início).

Substitua a primeira frase:
```
main.py                 <- Bot loop orchestration — event-driven via BinanceCandleManager WebSocket;
```

Por:
```
main.py                 <- Bot loop orchestration — event-driven via `LighterCandleManager` (Lighter WS nativo) ou `BinanceCandleManager` (caminho Hyperliquid / fallback). Switch baseado em `selected_exchange` + flag `use_lighter_ws_candles`;
```

- [ ] **Step 3: Adicionar config flag na tabela de "Parâmetros globais"**

Localize a tabela "Parâmetros globais" (~linha 600). Adicione uma linha:

```markdown
| use_lighter_ws_candles | true | Se `true` (default), usa LighterCandleManager (WS nativo) quando exchange=lighter; `false` força fallback ao BinanceCandleManager (legado, pra rollback) |
```

- [ ] **Step 4: Commit**

```bash
git add hyperliquid-bot/CLAUDE.md
git commit -m "docs(claude): document LighterCandleManager and use_lighter_ws_candles flag"
```

---

## Task 11: Validação manual em produção + remoção da flag

**Files:** nenhum mudado neste task — só observação.

- [ ] **Step 1: Rodar 24h em paper/testnet**

- Inicia o bot com `use_lighter_ws_candles=true` (default já)
- Monitora `logs/bot_YYYY-MM-DD.log` por:
  - `LighterCandleManager: reconnect` — espera 0 ou ≤1 por hora
  - `boundary fallback fired (WS silent)` — espera <5% das janelas
  - Erros não tratados em `lighter-ws` ou `lighter-worker` threads
- Compara timing: cada SCAN log deve estar a <2s do boundary real (ex: vela 14:05 → SCAN log às 14:05:01-02). Anterior era 14:05:15-30.

- [ ] **Step 2: Rodar 24h em mainnet com 1 ativo**

- Em produção real, reduzir `monitored_assets` temporariamente para 1 ativo de alto volume (ex: BTC)
- Verifica que trades executam normalmente e PnL casa com expectativas
- Compara backtest vs live no mesmo período — divergência de sinais deve ser zero (ambos lêem candle fechado via REST Lighter, mas live agora pega o close mais cedo)

- [ ] **Step 3: Expandir para todos os ativos em produção**

- Restaura `monitored_assets` original
- Monitora por mais 24-48h
- Atualiza `Memory/Aprendizados.md` com o resultado:

```
2026-MM-DD | RazorHL | Migração para Lighter WS native candles reduziu latência de ~20s para ~1s; zero divergência live↔backtest em 48h; fallback boundary disparou em <X% dos casos
```

- [ ] **Step 4: Remover a flag (opcional, após 1 semana estável)**

Em `main.py`, simplifica para sempre usar `LighterCandleManager` quando exchange=lighter:

```python
if selected_exchange == "lighter":
    candle_mgr = LighterCandleManager(...)
else:
    candle_mgr = BinanceCandleManager(...)
```

Remove a flag do `CLAUDE.md`. Commit:

```bash
git add hyperliquid-bot/main.py hyperliquid-bot/CLAUDE.md
git commit -m "chore(main): remove use_lighter_ws_candles flag after stable rollout"
```

---

## Resumo de cobertura vs spec

| Requisito spec | Tasks |
|---|---|
| `LighterCandleManager` com threading | 3, 5 |
| Conexão WS única + N subscribes | 5, 8 |
| Parser de canal `candle:X:Y` ou `candle/X/Y` | 1 |
| Detecção de close via mudança de `t` | 2, 4 |
| Dedup de emissão de close | 4 |
| Buffer compartilhado com `LighterExchangeClient` | 6 |
| `_drop_open_candle` mantido no read path | 8 (parte do bloco mantido inalterado em get_candles) |
| Boundary timer + REST fallback | 7 |
| Watchdog + reconnect com backoff | 5 |
| `update_assets` incremental | 8 |
| Remover fallback Binance | 8 |
| Switch em `main.py` por `selected_exchange` + flag | 9 |
| Documentar no `CLAUDE.md` | 10 |
| Rollout faseado | 11 |
| Métrica de sucesso (latência <2s, divergência zero) | 11 |
