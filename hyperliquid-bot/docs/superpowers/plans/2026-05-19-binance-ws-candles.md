# Binance WebSocket Candle Manager — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Substituir REST polling de candles por WebSocket event-driven, eliminando até 10s de atraso na detecção de fechamento e garantindo consistência com backtests.

**Architecture:** `BinanceCandleManager` roda 3 threads internas (WS, Worker, Watchdog). WS thread só atualiza buffer + enfileira eventos. Worker thread consome a fila e dispara `process_asset` apenas em closes de 5m. Watchdog reconecta após 90s sem eventos. `main.py` para de chamar `process_asset` no loop — só o worker o faz.

**Tech Stack:** Python 3.10+, `websocket-client>=1.6.0`, `requests`, `pandas`, `threading`, `queue`

---

## Mapa de Arquivos

| Arquivo | Ação | Responsabilidade |
|---------|------|-----------------|
| `requirements.txt` | Modificar | Adicionar websocket-client |
| `bot/exchanges/binance_ws.py` | Criar | BinanceCandleManager completo |
| `tests/test_binance_ws.py` | Criar | Testes unitários do manager |
| `main.py` | Modificar | Integrar manager, remover polling |

---

## Task 1: Dependência + esqueleto

**Files:**
- Modify: `requirements.txt`
- Create: `bot/exchanges/binance_ws.py`

- [ ] **Step 1: Adicionar websocket-client ao requirements.txt**

Arquivo final:
```
hyperliquid-python-sdk>=0.9.0
eth-account>=0.11.0
pandas>=2.0.0
pandas-ta>=0.3.14b
flask>=3.0.0
flask-socketio>=5.3.0
eventlet>=0.35.0
requests>=2.31.0
websocket-client>=1.6.0
```

- [ ] **Step 2: Instalar**

```bash
cd hyperliquid-bot
pip install websocket-client>=1.6.0
```

Expected: instala sem erro, `import websocket` funciona.

- [ ] **Step 3: Criar esqueleto de `bot/exchanges/binance_ws.py`**

```python
import queue
import threading
import time
from typing import Callable

import pandas as pd
import websocket

from bot.exchanges.base import fetch_binance_candles
from bot.logger import get_logger

log = get_logger(__name__)

_WS_BASE = "wss://stream.binance.com:9443/stream"
_SEED_COUNTS: dict[str, int] = {
    "5m": 500,
    "15m": 300,
    "1h": 300,
    "4h": 300,
    "1d": 300,
}
_WS_INTERVALS = list(_SEED_COUNTS.keys())  # ["5m","15m","1h","4h","1d"]


def _binance_symbol(asset: str) -> str:
    return f"{asset.upper()}USDT"


class BinanceCandleManager:
    def __init__(self, assets: list[str], on_candle_close: Callable[[str, str], None]):
        self._assets = list(assets)
        self._on_candle_close = on_candle_close
        self._buffer: dict[str, dict[str, pd.DataFrame]] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._paused = False
        self._stop_event = threading.Event()
        self._ws: websocket.WebSocketApp | None = None
        self._last_event_ts: float = 0.0
        self._ws_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame: ...
    def update_assets(self, assets: list[str]) -> None: ...
```

- [ ] **Step 4: Verificar importação**

```bash
cd hyperliquid-bot
python -c "from bot.exchanges.binance_ws import BinanceCandleManager; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt bot/exchanges/binance_ws.py
git commit -m "feat: add BinanceCandleManager skeleton + websocket-client dep"
```

---

## Task 2: Buffer + get_candles + seed

**Files:**
- Modify: `bot/exchanges/binance_ws.py`
- Create: `tests/test_binance_ws.py`

- [ ] **Step 1: Escrever testes que falham**

Criar `tests/test_binance_ws.py`:

```python
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
        mgr = _make_manager()
        df = _make_df([1000, 2000])

        def fake_fetch(asset, interval, count):
            return _make_df([count, count + 1])

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", side_effect=fake_fetch):
            mgr._seed_buffer()

        for interval in ["5m", "15m", "1h", "4h", "1d"]:
            assert "BTC" in mgr._buffer
            assert interval in mgr._buffer["BTC"]
            assert not mgr._buffer["BTC"][interval].empty

    def test_seed_retries_on_rest_failure(self):
        mgr = _make_manager()
        df = _make_df([1000])
        call_count = {"n": 0}

        def flaky_fetch(asset, interval, count):
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise Exception("network error")
            return df

        with patch("bot.exchanges.binance_ws.fetch_binance_candles", side_effect=flaky_fetch):
            with patch("time.sleep"):
                mgr._seed_buffer()

        assert call_count["n"] == 3
```

- [ ] **Step 2: Rodar testes e confirmar que falham**

```bash
cd hyperliquid-bot
pytest tests/test_binance_ws.py -v
```

Expected: FAIL — `_seed_buffer` e `get_candles` não implementados.

- [ ] **Step 3: Implementar `get_candles` e `_seed_buffer`**

Substituir os stubs de `get_candles` e adicionar `_seed_buffer` em `binance_ws.py`:

```python
def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
    with self._lock:
        asset_buf = self._buffer.get(asset, {})
        df = asset_buf.get(interval, pd.DataFrame())
    if df.empty:
        df = fetch_binance_candles(asset, interval, count)
        if not df.empty:
            with self._lock:
                self._buffer.setdefault(asset, {})[interval] = df
        return df
    return df.iloc[-count:].copy()

def _seed_buffer(self) -> None:
    for asset in self._assets:
        for interval, count in _SEED_COUNTS.items():
            for attempt in range(3):
                try:
                    df = fetch_binance_candles(asset, interval, count)
                    if not df.empty:
                        with self._lock:
                            self._buffer.setdefault(asset, {})[interval] = df
                    break
                except Exception as e:
                    log.warning(f"[{asset}] Seed {interval} attempt {attempt+1}/3 failed: {e}")
                    if attempt < 2:
                        time.sleep(2 ** attempt)
```

- [ ] **Step 4: Rodar testes**

```bash
pytest tests/test_binance_ws.py::TestBuffer tests/test_binance_ws.py::TestSeed -v
```

Expected: todos PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/exchanges/binance_ws.py tests/test_binance_ws.py
git commit -m "feat: buffer management, get_candles, and seed with retry"
```

---

## Task 3: URL builder + parser de mensagem WS

**Files:**
- Modify: `bot/exchanges/binance_ws.py`
- Modify: `tests/test_binance_ws.py`

- [ ] **Step 1: Escrever testes**

Adicionar ao final de `tests/test_binance_ws.py`:

```python
import json
from bot.exchanges.binance_ws import _build_stream_url, _parse_kline_event


class TestStreamUrl:
    def test_single_asset_single_interval(self):
        url = _build_stream_url(["BTC"], ["5m"])
        assert "btcusdt@kline_5m" in url
        assert url.startswith("wss://stream.binance.com")

    def test_multiple_assets_intervals(self):
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
        asset, interval, row, is_closed = _parse_kline_event(self._event())
        assert asset == "BTC"
        assert interval == "5m"
        assert is_closed is True
        assert row["close"] == 76789.0
        assert row["timestamp"] == 1700000000000

    def test_parses_open_candle(self):
        _, _, _, is_closed = _parse_kline_event(self._event(is_closed=False))
        assert is_closed is False

    def test_returns_none_on_bad_message(self):
        result = _parse_kline_event('{"not": "kline"}')
        assert result is None
```

- [ ] **Step 2: Rodar testes e confirmar que falham**

```bash
pytest tests/test_binance_ws.py::TestStreamUrl tests/test_binance_ws.py::TestParseKlineEvent -v
```

Expected: FAIL — funções não existem.

- [ ] **Step 3: Implementar as funções (módulo-level, fora da classe)**

Adicionar em `binance_ws.py` antes da classe:

```python
def _build_stream_url(assets: list[str], intervals: list[str]) -> str:
    streams = "/".join(
        f"{_binance_symbol(a).lower()}@kline_{i}"
        for a in assets
        for i in intervals
    )
    return f"{_WS_BASE}?streams={streams}"


def _parse_kline_event(raw: str) -> tuple | None:
    try:
        msg = json.loads(raw)
        k = msg["data"]["k"]
        symbol = msg["data"]["s"]          # e.g. "BTCUSDT"
        asset = symbol.replace("USDT", "") # e.g. "BTC"
        interval = k["i"]
        is_closed = k["x"]
        row = {
            "timestamp": int(k["t"]),
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }
        return asset, interval, row, is_closed
    except Exception:
        return None
```

Adicionar `import json` no topo do arquivo.

- [ ] **Step 4: Rodar testes**

```bash
pytest tests/test_binance_ws.py::TestStreamUrl tests/test_binance_ws.py::TestParseKlineEvent -v
```

Expected: todos PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/exchanges/binance_ws.py tests/test_binance_ws.py
git commit -m "feat: WS stream URL builder and kline event parser"
```

---

## Task 4: WS thread + on_message

**Files:**
- Modify: `bot/exchanges/binance_ws.py`
- Modify: `tests/test_binance_ws.py`

- [ ] **Step 1: Escrever testes**

Adicionar ao final de `tests/test_binance_ws.py`:

```python
import json


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
```

- [ ] **Step 2: Rodar testes e confirmar que falham**

```bash
pytest tests/test_binance_ws.py::TestOnMessage -v
```

Expected: FAIL — `_on_message` não implementado.

- [ ] **Step 3: Implementar `_on_message` + `_update_buffer` + `start` + `stop`**

```python
def _update_buffer(self, asset: str, interval: str, row: dict) -> None:
    new_row = pd.DataFrame([row])
    new_row["datetime"] = pd.to_datetime(new_row["timestamp"], unit="ms", utc=True)
    new_row.set_index("datetime", inplace=True)
    with self._lock:
        existing = self._buffer.get(asset, {}).get(interval, pd.DataFrame())
        if existing.empty:
            self._buffer.setdefault(asset, {})[interval] = new_row
        else:
            merged = pd.concat([existing, new_row])
            merged = merged[~merged.index.duplicated(keep="last")]
            self._buffer.setdefault(asset, {})[interval] = merged.sort_index()

def _on_message(self, ws, raw: str) -> None:
    self._last_event_ts = time.time()
    result = _parse_kline_event(raw)
    if result is None:
        return
    asset, interval, row, is_closed = result
    if is_closed:
        self._update_buffer(asset, interval, row)
        try:
            self._queue.put_nowait((asset, interval))
        except queue.Full:
            pass  # descarta — próximo candle chegará

def _on_error(self, ws, error) -> None:
    log.error(f"Binance WS error: {error}")

def _on_close(self, ws, code, msg) -> None:
    log.warning(f"Binance WS closed (code={code})")

def _run_ws(self) -> None:
    url = _build_stream_url(self._assets, _WS_INTERVALS)
    self._ws = websocket.WebSocketApp(
        url,
        on_message=self._on_message,
        on_error=self._on_error,
        on_close=self._on_close,
    )
    self._ws.run_forever(ping_interval=20, ping_timeout=10)

def start(self) -> None:
    log.info("BinanceCandleManager: seeding buffer via REST...")
    self._seed_buffer()
    log.info("BinanceCandleManager: seed complete, opening WebSocket stream...")
    self._stop_event.clear()
    self._last_event_ts = time.time()

    self._ws_thread = threading.Thread(target=self._run_ws, daemon=True, name="binance-ws")
    self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="candle-worker")
    self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="candle-watchdog")

    self._ws_thread.start()
    self._worker_thread.start()
    self._watchdog_thread.start()
    log.info("BinanceCandleManager: started.")

def stop(self) -> None:
    self._stop_event.set()
    if self._ws:
        self._ws.close()
    log.info("BinanceCandleManager: stopped.")
```

- [ ] **Step 4: Rodar testes**

```bash
pytest tests/test_binance_ws.py::TestOnMessage -v
```

Expected: todos PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/exchanges/binance_ws.py tests/test_binance_ws.py
git commit -m "feat: WS on_message, buffer update, start/stop"
```

---

## Task 5: Worker thread (pause/resume/drain)

**Files:**
- Modify: `bot/exchanges/binance_ws.py`
- Modify: `tests/test_binance_ws.py`

- [ ] **Step 1: Escrever testes**

Adicionar ao final de `tests/test_binance_ws.py`:

```python
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
```

- [ ] **Step 2: Rodar testes e confirmar que falham**

```bash
pytest tests/test_binance_ws.py::TestWorker -v
```

Expected: FAIL.

- [ ] **Step 3: Implementar `_process_queue_once`, `_worker_loop`, `pause`, `resume`**

```python
def _process_queue_once(self) -> None:
    try:
        asset, interval = self._queue.get(timeout=1)
    except queue.Empty:
        return
    if self._paused:
        return
    if interval != "5m":
        return
    try:
        self._on_candle_close(asset, interval)
    except Exception as e:
        log.error(f"[{asset}] process_asset error: {e}", exc_info=True)

def _worker_loop(self) -> None:
    while not self._stop_event.is_set():
        self._process_queue_once()

def pause(self) -> None:
    self._paused = True

def resume(self) -> None:
    while not self._queue.empty():
        try:
            self._queue.get_nowait()
        except queue.Empty:
            break
    self._paused = False
```

- [ ] **Step 4: Rodar testes**

```bash
pytest tests/test_binance_ws.py::TestWorker -v
```

Expected: todos PASS.

- [ ] **Step 5: Rodar todos os testes**

```bash
pytest tests/test_binance_ws.py -v
```

Expected: todos PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/exchanges/binance_ws.py tests/test_binance_ws.py
git commit -m "feat: worker thread with pause/resume/drain and 5m-only trigger"
```

---

## Task 6: Watchdog + reconnect com overlap

**Files:**
- Modify: `bot/exchanges/binance_ws.py`
- Modify: `tests/test_binance_ws.py`

- [ ] **Step 1: Escrever testes**

Adicionar ao final de `tests/test_binance_ws.py`:

```python
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
```

- [ ] **Step 2: Rodar testes e confirmar que falham**

```bash
pytest tests/test_binance_ws.py::TestReseedOverlap -v
```

Expected: FAIL.

- [ ] **Step 3: Implementar `_reseed_with_overlap`, `_reconnect`, `_watchdog_loop`**

```python
def _reseed_with_overlap(self, asset: str, interval: str) -> None:
    try:
        fresh = fetch_binance_candles(asset, interval, count=50)
        if fresh.empty:
            return
        with self._lock:
            existing = self._buffer.get(asset, {}).get(interval, pd.DataFrame())
            if existing.empty:
                self._buffer.setdefault(asset, {})[interval] = fresh
            else:
                merged = pd.concat([existing, fresh])
                merged = merged[~merged.index.duplicated(keep="last")]
                self._buffer[asset][interval] = merged.sort_index()
    except Exception as e:
        log.warning(f"[{asset}] Re-seed {interval} failed (using existing buffer): {e}")

def _reconnect(self) -> None:
    log.warning("BinanceCandleManager: reconnecting...")
    # Re-seed com overlap antes de reabrir o stream
    for asset in self._assets:
        for interval in _WS_INTERVALS:
            self._reseed_with_overlap(asset, interval)
    # Fecha WS atual
    if self._ws:
        try:
            self._ws.close()
        except Exception:
            pass
    time.sleep(1)
    # Abre nova conexão WS em nova thread
    self._ws_thread = threading.Thread(target=self._run_ws, daemon=True, name="binance-ws")
    self._ws_thread.start()
    self._last_event_ts = time.time()
    log.info("BinanceCandleManager: reconnected.")

def _watchdog_loop(self) -> None:
    while not self._stop_event.is_set():
        time.sleep(30)
        if self._stop_event.is_set():
            break
        elapsed = time.time() - self._last_event_ts
        if elapsed > 90:
            log.warning(f"Binance WS silent for {elapsed:.0f}s — triggering reconnect")
            self._reconnect()
```

- [ ] **Step 4: Rodar testes**

```bash
pytest tests/test_binance_ws.py::TestReseedOverlap -v
```

Expected: todos PASS.

- [ ] **Step 5: Rodar todos os testes**

```bash
pytest tests/test_binance_ws.py -v
```

Expected: todos PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/exchanges/binance_ws.py tests/test_binance_ws.py
git commit -m "feat: watchdog, reconnect with overlap merge, reseed"
```

---

## Task 7: `update_assets`

**Files:**
- Modify: `bot/exchanges/binance_ws.py`
- Modify: `tests/test_binance_ws.py`

- [ ] **Step 1: Escrever teste**

Adicionar ao final de `tests/test_binance_ws.py`:

```python
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
```

- [ ] **Step 2: Rodar teste e confirmar que falha**

```bash
pytest tests/test_binance_ws.py::TestUpdateAssets -v
```

Expected: FAIL.

- [ ] **Step 3: Implementar `update_assets`**

```python
def update_assets(self, assets: list[str]) -> None:
    new_assets = [a for a in assets if a not in self._assets]
    self._assets = list(assets)
    # Semeia novos assets
    for asset in new_assets:
        for interval, count in _SEED_COUNTS.items():
            for attempt in range(3):
                try:
                    df = fetch_binance_candles(asset, interval, count)
                    if not df.empty:
                        with self._lock:
                            self._buffer.setdefault(asset, {})[interval] = df
                    break
                except Exception as e:
                    log.warning(f"[{asset}] Seed {interval} attempt {attempt+1}/3: {e}")
                    if attempt < 2:
                        time.sleep(2 ** attempt)
    # Reconecta stream com nova lista de assets
    self._reconnect()
```

- [ ] **Step 4: Rodar todos os testes**

```bash
pytest tests/test_binance_ws.py -v
```

Expected: todos PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/exchanges/binance_ws.py tests/test_binance_ws.py
git commit -m "feat: update_assets seeds new assets and reconnects stream"
```

---

## Task 8: Integrar no `main.py`

**Files:**
- Modify: `main.py`

Esta task tem três partes: (A) modificar `process_asset`, (B) modificar `bot_loop`, (C) verificação manual.

### Parte A — Modificar `process_asset`

- [ ] **Step 1: Atualizar assinatura e corpo de `process_asset`**

Substituir a função `process_asset` atual (linhas 276–411) por:

```python
def process_asset(asset: str, cfg: dict,
                  last_4h_ts: dict, last_1d_ts: dict, last_1h_ts: dict):
    """Triggered on every 5m candle close by BinanceCandleManager worker thread."""
    from bot.exchanges.base import fetch_binance_candles

    # 1m sempre via REST — não está no buffer WS
    df_1m = fetch_binance_candles(asset, "1m", count=100)
    # Demais timeframes via buffer do manager
    df_5m  = candle_mgr.get_candles(asset, "5m",  count=500)
    df_15m = candle_mgr.get_candles(asset, "15m", count=300)
    df_4h  = candle_mgr.get_candles(asset, "4h",  count=300)
    df_1d  = candle_mgr.get_candles(asset, "1d",  count=300)
    df_1h  = candle_mgr.get_candles(asset, "1h",  count=300)

    if df_1m.empty or df_5m.empty or df_15m.empty:
        log.debug(f"[{asset}] No candle data available")
        return

    # Resample 1m → 2m
    df_2m = (
        df_1m.resample("2min")
        .agg({"open": "first", "high": "max", "low": "min",
              "close": "last", "volume": "sum", "timestamp": "last"})
        .dropna()
        .reset_index()
    )

    log.debug(f"[{asset}] New 5m candle closed — price={df_5m['close'].iloc[-1]:.2f}")

    # new_5m é sempre True — esta função só é chamada em fechamentos de 5m
    new_5m = True

    # Detectar fechamentos de 4h, 1d, 1h por comparação de timestamp
    new_4h = False
    if not df_4h.empty:
        latest_4h_ts = int(df_4h["timestamp"].iloc[-1])
        if latest_4h_ts > last_4h_ts.get(asset, 0):
            last_4h_ts[asset] = latest_4h_ts
            new_4h = True
            log.info(f"[{asset}] New 4H candle closed")

    new_1d = False
    if not df_1d.empty:
        latest_1d_ts = int(df_1d["timestamp"].iloc[-1])
        if latest_1d_ts > last_1d_ts.get(asset, 0):
            last_1d_ts[asset] = latest_1d_ts
            new_1d = True
            log.info(f"[{asset}] New 1D candle closed")

    new_1h = False
    if not df_1h.empty:
        latest_1h_ts = int(df_1h["timestamp"].iloc[-1])
        if latest_1h_ts > last_1h_ts.get(asset, 0):
            last_1h_ts[asset] = latest_1h_ts
            new_1h = True
            log.info(f"[{asset}] New 1H candle closed")

    # Merge params
    mr_params = db.get_strategy_config("mean_reversion").get("params", {})
    vr_params = db.get_strategy_config("vwap_reversion").get("params", {})
    effective_cfg = {**cfg, **mr_params, **vr_params}

    # Compute indicators
    indicators = compute_all(df_1m, df_5m, effective_cfg)
    if indicators is None:
        return

    # Update live fee viability status for dashboard
    fee_rate = float(cfg.get("fee_rate_round_trip") or 0.0009)
    tp_mult = float(mr_params.get("tp_atr_multiplier", 1.5))
    atr = indicators["atr"]
    price = indicators["close_1m"]
    atr_pct = atr / price
    with _status_lock:
        _asset_live_status[asset] = {
            "atr_pct": round(atr_pct, 6),
            "required_pct": round(fee_rate / tp_mult, 6),
            "fee_viable": atr_pct * tp_mult > fee_rate,
        }

    funding_rate = client.get_funding_rate(asset)

    # BB mid exit check — antes da avaliação de sinais
    check_bb_mid_exit(asset, df_5m)

    # Evaluate signals
    signals = evaluate_all(
        asset, indicators, funding_rate, effective_cfg,
        df_1m=df_1m, df_5m=df_5m, df_2m=df_2m, df_15m=df_15m,
        df_4h=df_4h, df_1d=df_1d, df_1h=df_1h,
        new_4h=new_4h, new_1d=new_1d, new_1h=new_1h, new_5m=new_5m,
    )

    for signal in signals:
        allowed, reason = risk_mgr.can_open_trade(asset)
        if not allowed:
            signal["reason"] = reason
            db.insert_signal(signal)
            continue

        size_usd = risk_mgr.calculate_position_size()
        if size_usd <= 0:
            signal["reason"] = "Position size is 0"
            db.insert_signal(signal)
            continue

        trade_id = open_position(client, signal, size_usd, effective_cfg)
        if trade_id is None:
            signal["reason"] = "Order execution failed"
            db.insert_signal(signal)
```

### Parte B — Modificar `bot_loop`

- [ ] **Step 2: Adicionar import e global `candle_mgr` no topo de `main.py`**

Após as imports existentes, adicionar:
```python
from bot.exchanges.binance_ws import BinanceCandleManager
```

Após `client: BaseExchangeClient = create_exchange_client()`, adicionar:
```python
candle_mgr: BinanceCandleManager | None = None
```

- [ ] **Step 3: Substituir `bot_loop` pelo novo loop event-driven**

Substituir `def bot_loop():` até o final por:

```python
def bot_loop():
    global risk_mgr, candle_mgr

    # Connect to exchange with retries (lógica existente — não alterar)
    _CONNECT_MAX_RETRIES = 5
    _CONNECT_BASE_DELAY = 5
    for attempt in range(_CONNECT_MAX_RETRIES):
        try:
            client.connect()
            log.info("Connected to Hyperliquid successfully")
            break
        except Exception as e:
            delay = _CONNECT_BASE_DELAY * (2 ** attempt)
            log.error(f"Failed to connect (attempt {attempt+1}/{_CONNECT_MAX_RETRIES}): {e}", exc_info=True)
            if attempt == _CONNECT_MAX_RETRIES - 1:
                log.error("All connection attempts exhausted — bot stopping")
                db.set_config("bot_status", "error")
                return
            log.warning(f"Retrying connection in {delay}s...")
            _stop_event.wait(delay)
            if _stop_event.is_set():
                db.set_config("bot_status", "stopped")
                return

    risk_mgr = RiskManager(client)
    db.set_config("bot_status", "running")

    cfg = db.get_all_config()
    assets_raw = cfg.get("monitored_assets", '["BTC","ETH","SOL"]')
    try:
        initial_assets = json.loads(assets_raw)
    except json.JSONDecodeError:
        initial_assets = ["BTC", "ETH", "SOL"]

    # Timestamps para detecção de novos candles 4h/1d/1h
    last_4h_ts: dict[str, int] = {}
    last_1d_ts: dict[str, int] = {}
    last_1h_ts: dict[str, int] = {}

    def on_candle_close(asset: str, interval: str):
        try:
            current_cfg = db.get_all_config()
            process_asset(asset, current_cfg, last_4h_ts, last_1d_ts, last_1h_ts)
        except Exception as e:
            log.error(f"[{asset}] on_candle_close error: {e}", exc_info=True)

    candle_mgr = BinanceCandleManager(initial_assets, on_candle_close=on_candle_close)
    candle_mgr.start()

    _heartbeat_counter = 0

    while not _stop_event.is_set():
        try:
            cfg = db.get_all_config()
            status = cfg.get("bot_status", "running")

            if status == "stopped":
                log.info("Bot stopped via dashboard")
                break

            if status == "paused":
                candle_mgr.pause()
                _stop_event.wait(5)
                continue
            else:
                if candle_mgr._paused:
                    candle_mgr.resume()

            set_debug(cfg.get("debug_logging", "false").lower() == "true")

            _heartbeat_counter += 1
            if _heartbeat_counter % 2 == 0:  # ~60s com wait(30)
                log.info(f"Bot alive — cycle #{_heartbeat_counter}, monitoring assets")

            # Checar se lista de ativos mudou
            assets_raw = cfg.get("monitored_assets", '["BTC","ETH","SOL"]')
            try:
                current_assets = json.loads(assets_raw)
            except json.JSONDecodeError:
                current_assets = ["BTC", "ETH", "SOL"]
            if set(current_assets) != set(candle_mgr._assets):
                log.info(f"Asset list changed → {current_assets}")
                candle_mgr.update_assets(current_assets)

            # Checar TP/SL de posições abertas
            risk_mgr.check_open_positions_tp_sl()

            _stop_event.wait(30)

        except Exception as e:
            log.error(f"Bot loop error: {e}", exc_info=True)
            _stop_event.wait(30)

    candle_mgr.stop()
    log.info("Bot stopped.")
```

### Parte C — Verificação

- [ ] **Step 4: Verificar importações e sintaxe**

```bash
cd hyperliquid-bot
python -c "import main; print('OK')"
```

Expected: `OK` sem erros de import.

- [ ] **Step 5: Rodar todos os testes**

```bash
pytest tests/test_binance_ws.py -v
```

Expected: todos PASS.

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: integrate BinanceCandleManager into main loop — event-driven on 5m close"
```

---

## Task 9: Verificação end-to-end

- [ ] **Step 1: Instalar dependências e iniciar o bot**

```bash
cd hyperliquid-bot
pip install -r requirements.txt
python run.py
```

Acessar `http://localhost:8080`, configurar credenciais se necessário.

- [ ] **Step 2: Verificar logs de startup**

Nos logs do dashboard ou em `logs/bot_YYYY-MM-DD.log`, confirmar sequência:

```
BinanceCandleManager: seeding buffer via REST...
BinanceCandleManager: seed complete, opening WebSocket stream...
BinanceCandleManager: started.
```

- [ ] **Step 3: Aguardar primeiro fechamento de candle 5m e verificar**

Aguardar até o próximo fechamento de candle de 5m (próxima marca :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55). Nos logs, confirmar:

```
[BTC] New 5m candle closed — price=XXXXX.XX
```

O log deve aparecer em menos de 2 segundos após o fechamento real da Binance.

- [ ] **Step 4: Verificar ausência de erros**

Nos logs, confirmar ausência de:
- `Failed to fetch Binance candles`
- `Binance WS error`
- `on_candle_close error`

- [ ] **Step 5: Commit final**

```bash
git add .
git commit -m "feat: Binance WebSocket candle manager — event-driven 5m close trigger"
```
