"""LighterCandleManager — live candle streaming from Lighter WebSocket.

Substitui o BinanceCandleManager no caminho Lighter. Usa o canal nativo
`candle/{market_id}/{resolution}` que empurra updates em batches de 500ms
a cada trade. Detecta candle close por mudança do campo `t` (timestamp).
"""

import json
import re
import time
import websocket  # websocket-client (already a dependency, used by binance_ws)

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


import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from bot.logger import get_logger

log = get_logger(__name__)

_QUEUE_MAXSIZE = 50
_SEED_COUNT = 500
_BOUNDARY_MARGIN_MS = 5000   # wait 5s past boundary then fire REST fallback for any
                              # (asset, tf) whose close wasn't yet emitted by WS. Caps
                              # latency at ~5s; WS dedup avoids duplicate emits.


class LighterCandleManager:
    """Live candle streaming from Lighter native WebSocket.

    Threading model (matches BinanceCandleManager):
    - _ws_thread:        WebSocket reader, parses messages, updates buffer
    - _worker_thread:    drains queue and dispatches to thread pool
    - _watchdog_thread:  monitors global silence, reconnects
    - _boundary_thread:  fires per-TF boundary REST fallback for silent channels
    - _ping_thread:      sends JSON `{"type":"ping"}` every 60s (Lighter requires
                         client-initiated app-level keepalive; WebSocket PING
                         control frames are silently ignored)

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

        # buffer is owned by self._client (LighterExchangeClient._candle_buffer)

        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._paused = False
        self._stop_event = threading.Event()

        self._ws = None
        self._last_msg_ts: float = 0.0
        self._ts_lock = threading.Lock()
        self._last_update_ms: dict[tuple[str, str], int] = {}

        # Per-close dedup: tracks the `t` of the last candle whose close we
        # already announced for each (asset, tf). Accessed from both the WS
        # message thread (_on_message) and the boundary fallback thread
        # (_check_boundary_fallback); without _emit_lock, both can read the
        # same `last_emitted`, both decide to emit, and the same close gets
        # enqueued twice → duplicate process_asset → duplicate trades.
        self._last_emitted_t: dict[tuple[str, str], int] = {}
        self._emit_lock = threading.Lock()

        # subscriptions: (asset, tf) → market_id
        self._subscriptions: dict[tuple[str, str], int] = {}

        self._ws_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._boundary_thread: threading.Thread | None = None
        self._ping_thread: threading.Thread | None = None
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
        log.candle("LighterCandleManager: seeding buffer via REST...")
        self._seed_buffer()
        log.candle(f"LighterCandleManager: seed complete ({len(self._subscriptions)} subscriptions). Opening WS...")
        self._stop_event.clear()
        with self._ts_lock:
            self._last_msg_ts = time.time()

        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True, name="lighter-ws")
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="lighter-worker")
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="lighter-watchdog")
        self._boundary_thread = threading.Thread(target=self._boundary_loop, daemon=True, name="lighter-boundary")
        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True, name="lighter-ping")

        self._ws_thread.start()
        self._worker_thread.start()
        self._watchdog_thread.start()
        self._boundary_thread.start()
        self._ping_thread.start()
        log.info("LighterCandleManager: started.")

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

    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
        """Read last `count` candles from the shared client buffer."""
        key = (asset.upper(), interval)
        df = self._get_buffer(key)
        if df.empty:
            return df
        return df.iloc[-count:].copy()

    def _get_buffer(self, key: tuple[str, str]) -> pd.DataFrame:
        with self._client._candle_buffer_lock:
            return self._client._candle_buffer.get(key, pd.DataFrame())

    def _set_buffer(self, key: tuple[str, str], df: pd.DataFrame) -> None:
        with self._client._candle_buffer_lock:
            self._client._candle_buffer[key] = df
        # Mark this buffer as WS-fresh so LighterExchangeClient.get_candles can
        # skip the REST roundtrip on next read.
        fresh_ts_map = getattr(self._client, "_candle_buffer_fresh_ts", None)
        if fresh_ts_map is not None:
            fresh_ts_map[key] = time.time()

    def _market_to_asset(self, market_id: int, interval: str) -> str | None:
        """Reverse lookup market_id → asset via _subscriptions map."""
        for (asset, tf), mid in self._subscriptions.items():
            if mid == market_id and tf == interval:
                return asset
        return None

    def _on_message(self, ws, raw: str) -> None:
        with self._ts_lock:
            self._last_msg_ts = time.time()
        self._msg_count = getattr(self, "_msg_count", 0) + 1
        if self._msg_count <= 5 or self._msg_count % 100 == 0:
            log.candle(f"Lighter WS msg #{self._msg_count}: {raw[:200]}")

        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Surface server errors (e.g. 30010 "Too Many Inflight Messages")
        if "error" in msg:
            log.warning(f"Lighter WS error msg: {msg.get('error')}")
            return

        msg_type = msg.get("type", "")

        # Lighter sends application-level JSON ping; reply with pong or the
        # server closes the connection after ~2min of "inactivity" (it does
        # NOT honor WebSocket control PING frames). See SDK ws_client.py.
        if msg_type == "ping":
            try:
                ws.send(json.dumps({"type": "pong"}))
            except Exception as e:
                log.warning(f"Lighter WS pong send failed: {e}")
            return

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
            buf = self._get_buffer(key)
            new_buf, emitted_close = _apply_candle_update(buf, c)
            self._set_buffer(key, new_buf)

            self._last_update_ms[key] = int(c["t"])

            if is_snapshot:
                continue  # snapshot nunca emite

            if emitted_close and not self._paused:
                # dedup: só emite se ainda não emitimos esse close.
                # O close é da vela ANTERIOR. _last_emitted_t guarda o t da
                # vela cujo close já anunciamos. Check-and-set é feito sob
                # _emit_lock para impedir que esta thread e _check_boundary_fallback
                # leiam o mesmo last_emitted e ambas enfileirem o mesmo close.
                prev_t = int(c["t"]) - _INTERVAL_MS[interval]
                with self._emit_lock:
                    last_emitted = self._last_emitted_t.get(key, 0)
                    if prev_t <= last_emitted:
                        continue  # outro caminho já emitiu este close
                    self._last_emitted_t[key] = prev_t
                try:
                    self._queue.put_nowait((asset, interval))
                except queue.Full:
                    pass

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
                        self._set_buffer((asset, tf), df.copy())
                        last_ts = int(df.iloc[-1]["timestamp"]) if "timestamp" in df.columns else 0
                        self._last_update_ms[(asset, tf)] = last_ts
                except Exception as e:
                    log.warning(f"[{asset}] seed {tf} failed: {e}")

    def _on_open(self, ws) -> None:
        """Send subscribes for all (asset, tf) pairs, spaced to dodge rate limit.

        Lighter returns error 30010 'Too Many Inflight Messages' if subscribes
        arrive too fast. 20ms spacing keeps us under the threshold.
        """
        total = len(self._subscriptions)
        log.candle(f"Lighter WS open: sending {total} subscribes (20ms apart)...")
        sent = 0
        for (asset, tf), market_id in self._subscriptions.items():
            sub = {"type": "subscribe", "channel": f"candle/{market_id}/{tf}"}
            try:
                ws.send(json.dumps(sub))
                sent += 1
            except Exception as e:
                log.warning(f"[{asset}] subscribe {tf} failed: {e}")
            time.sleep(0.02)
        log.candle(f"Lighter WS open: sent {sent}/{total} subscribes")

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

    def _ping_loop(self) -> None:
        """Send application-level JSON ping every 60s.

        Lighter REQUIRES the client to send a frame within every 2-min window
        or it closes the connection (code=1000). WebSocket control PING frames
        from websocket-client's ping_interval are NOT honored — we must send
        `{"type":"ping"}` JSON ourselves.
        """
        while not self._stop_event.is_set():
            if self._stop_event.wait(60):
                break
            if self._ws is None:
                continue
            try:
                self._ws.send(json.dumps({"type": "ping"}))
            except Exception as e:
                log.warning(f"Lighter WS ping send failed: {e}")

    def _boundary_loop(self) -> None:
        """For each interval, sleep until next boundary + margin, then check fallback."""
        next_check: dict[str, int] = {}
        for tf in self._intervals:
            now_ms = int(time.time() * 1000)
            next_check[tf] = _next_boundary_ms(now_ms, tf)

        while not self._stop_event.is_set():
            now_ms = int(time.time() * 1000)
            # Pick the soonest upcoming boundary
            tf, boundary = min(next_check.items(), key=lambda x: x[1])
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
        log.candle("LighterCandleManager: reconnected.")

    def _check_boundary_fallback(self, boundary_ms: int, interval: str) -> None:
        """For every subscribed (asset, interval), fire REST + emit close if it
        wasn't already emitted via the WS push path.

        Why we DON'T try to be clever about "is WS active for this channel":
        We tried gating on `_channel_last_msg_ts` (skip if any WS msg arrived
        within 10s). Result: low-volume assets that had stray old-candle
        updates within the threshold got their close emitted via WS at +30-70s
        (PUMP at +58s, WLD at +67s observed) — unacceptable for a scalping bot.
        Cap latency by firing fallback at `_BOUNDARY_MARGIN_MS` post-boundary
        for ANY (asset, tf) whose close is not yet emitted. WS dedup
        (`_last_emitted_t`) ensures we never emit the same close twice.
        """
        prev_t = boundary_ms - _INTERVAL_MS[interval]
        for (asset, tf), _ in list(self._subscriptions.items()):
            if tf != interval:
                continue

            # Atomic check-and-set under _emit_lock — claim the right to emit
            # this close BEFORE doing the REST roundtrip. If we did the
            # roundtrip first and then claimed, _on_message could slip in
            # between and we'd emit the same close twice.
            with self._emit_lock:
                if self._last_emitted_t.get((asset, tf), 0) >= prev_t:
                    continue  # WS path (or a previous fallback tick) already emitted
                self._last_emitted_t[(asset, tf)] = prev_t

            try:
                df = self._client.get_candles(asset, tf, count=2)
            except Exception as e:
                log.warning(f"[{asset}] boundary REST fallback failed ({tf}): {e}")
                continue
            if df is None or df.empty:
                continue

            new_last = int(df.iloc[-1]["timestamp"])
            self._set_buffer((asset, tf), df.copy())
            self._last_update_ms[(asset, tf)] = new_last

            try:
                self._queue.put_nowait((asset, tf))
                log.candle(f"[{asset}] {tf} boundary fallback fired")
            except queue.Full:
                pass
