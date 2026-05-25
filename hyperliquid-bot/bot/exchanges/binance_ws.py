import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import pandas as pd
import websocket

from bot.exchanges.base import fetch_binance_candles
from bot.logger import get_logger

log = get_logger(__name__)

_WS_BASE = "wss://stream.binance.com:9443/stream"
_DEFAULT_SEED_COUNTS: dict[str, int] = {
    "5m": 500,
    "15m": 300,
    "30m": 300,
    "1h": 300,
    "4h": 300,
    "1d": 300,
}
_QUEUE_MAXSIZE = 50

# Assets whose Binance symbol differs from the standard {asset}USDT pattern.
# Maps internal asset name → Binance base symbol (USDT is appended automatically).
_BINANCE_SYMBOL_MAP: dict[str, str] = {
    "XAU": "XAUT",   # Tether Gold on Binance is XAUTUSDT
}
# Reverse map used to convert Binance symbol back to internal asset name.
_BINANCE_REVERSE_MAP: dict[str, str] = {v: k for k, v in _BINANCE_SYMBOL_MAP.items()}

# Assets with no Binance Spot equivalent — excluded from WS/seed.
# They are triggered by co-piggybacking on the next Binance 5m close event.
# Mutable: assets are auto-promoted here when Binance seed fails (see _try_seed_asset).
_COTRIGGER_ASSETS: set[str] = {"WTI", "HYPE", "LIT"}
_cotrigger_lock_global = threading.Lock()


def _mark_as_cotrigger(asset: str) -> None:
    """Promote an asset to co-trigger after Binance seed fails. Idempotent."""
    key = asset.upper()
    with _cotrigger_lock_global:
        if key in _COTRIGGER_ASSETS:
            return
        _COTRIGGER_ASSETS.add(key)
    log.warning(
        f"[{asset}] No Binance Spot equivalent detected — promoted to co-trigger. "
        f"Will fire once per 5m boundary via piggyback."
    )


def _binance_symbol(asset: str) -> str:
    base = _BINANCE_SYMBOL_MAP.get(asset.upper(), asset.upper())
    return f"{base}USDT"


def _build_stream_url(assets: list[str], intervals: list[str]) -> str:
    """Build Binance WS URL. Co-trigger assets (no Binance equivalent) are excluded."""
    binance_assets = [a for a in assets if a.upper() not in _COTRIGGER_ASSETS]
    streams = "/".join(
        f"{_binance_symbol(a).lower()}@kline_{i}"
        for a in binance_assets
        for i in intervals
    )
    return f"{_WS_BASE}?streams={streams}"


def _parse_kline_event(raw: str) -> tuple | None:
    """
    Parse a Binance kline WebSocket event from combined stream format.

    Returns:
        Tuple of (asset, interval, row_dict, is_closed) or None if parse fails.
        asset is the internal name (e.g. "XAU" for XAUTUSDT).
    """
    try:
        msg = json.loads(raw)
        k = msg["data"]["k"]
        symbol = msg["data"]["s"]              # e.g. "XAUTUSDT"
        base = symbol.replace("USDT", "")      # e.g. "XAUT"
        asset = _BINANCE_REVERSE_MAP.get(base, base)  # "XAUT" → "XAU", else identity
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


class BinanceCandleManager:
    """
    Live candle streaming from Binance Spot WebSocket.

    Threading model: 3 threads manage the lifecycle:
    - _ws_thread:      WebSocket reader, appends raw events to queue
    - _worker_thread:  Processes queue events, updates candle buffers, fires callback
    - _watchdog_thread: Monitors event freshness, reconnects on timeout

    Co-trigger: assets in _COTRIGGER_ASSETS have no Binance equivalent.
    They are fired once per 5m boundary, piggybacking on the first Binance
    5m close of that boundary (typically BTC/ETH/SOL, which are always monitored).

    Callback signature: on_candle_close(asset: str, interval: str) -> None
    """

    def __init__(self, assets: list[str], on_candle_close: Callable[[str, str], None],
                 intervals: list[str] | None = None):
        self._assets = list(assets)
        self._on_candle_close = on_candle_close
        self._intervals: list[str] = list(intervals) if intervals else ["5m"]
        self._seed_counts: dict[str, int] = {
            iv: _DEFAULT_SEED_COUNTS.get(iv, 300) for iv in self._intervals
        }
        self._buffer: dict[str, dict[str, pd.DataFrame]] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._paused = False
        self._stop_event = threading.Event()
        self._ws: websocket.WebSocketApp | None = None
        self._last_event_ts: float = 0.0
        self._ts_lock = threading.Lock()
        # Co-trigger: track last 5m boundary (candle open ts // 300000) already fired
        self._cotrigger_lock = threading.Lock()
        self._last_cotrigger_boundary: int = 0
        self._ws_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="asset-worker")

    @property
    def intervals(self) -> list[str]:
        return list(self._intervals)

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
        with self._ts_lock:
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
                pass
            # Co-trigger: fire assets with no Binance equivalent, once per 5m boundary
            if interval == "5m":
                boundary = row["timestamp"] // 300000
                with self._cotrigger_lock:
                    is_new = boundary != self._last_cotrigger_boundary
                    if is_new:
                        self._last_cotrigger_boundary = boundary
                if is_new:
                    for cotrigger_asset in list(self._assets):
                        if cotrigger_asset.upper() in _COTRIGGER_ASSETS:
                            try:
                                self._queue.put_nowait((cotrigger_asset, "5m"))
                            except queue.Full:
                                pass

    def _on_error(self, ws, error) -> None:
        log.error(f"Binance WS error: {error}")

    def _on_close(self, ws, code, msg) -> None:
        log.warning(f"Binance WS closed (code={code})")

    def _run_ws(self) -> None:
        url = _build_stream_url(self._assets, self._intervals)
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def start(self) -> None:
        """Start WebSocket reader, worker, and watchdog threads."""
        log.candle("BinanceCandleManager: seeding buffer via REST...")
        self._seed_buffer()
        log.candle("BinanceCandleManager: seed complete, opening WebSocket stream...")
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
        """Stop all threads and close WebSocket connection."""
        self._stop_event.set()
        if self._ws:
            self._ws.close()
        self._executor.shutdown(wait=False)
        log.info("BinanceCandleManager: stopped.")

    def _safe_callback(self, asset: str, interval: str) -> None:
        try:
            self._on_candle_close(asset, interval)
        except Exception as e:
            log.error(f"[{asset}] process_asset error: {e}", exc_info=True)

    def _worker_loop(self) -> None:
        """Drain queue and dispatch each 5m close to the thread pool in parallel."""
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
                if interval == "5m":
                    self._executor.submit(self._safe_callback, asset, interval)

    def _reseed_with_overlap(self, asset: str, interval: str) -> None:
        """Fetch fresh candles and merge with existing buffer. Co-trigger assets skipped."""
        if asset.upper() in _COTRIGGER_ASSETS:
            return
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
        """Re-seed buffers, close WS, open new WS thread."""
        log.warning("BinanceCandleManager: reconnecting...")
        for asset in self._assets:
            for interval in self._intervals:
                self._reseed_with_overlap(asset, interval)
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        time.sleep(1)
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True, name="binance-ws")
        self._ws_thread.start()
        with self._ts_lock:
            self._last_event_ts = time.time()
        log.candle("BinanceCandleManager: reconnected.")

    def _watchdog_loop(self) -> None:
        """Reconnect if WS silent for more than 90 seconds."""
        while not self._stop_event.is_set():
            if self._stop_event.wait(30):
                break
            with self._ts_lock:
                elapsed = time.time() - self._last_event_ts
            if elapsed > 90:
                log.warning(f"Binance WS silent for {elapsed:.0f}s — triggering reconnect")
                self._reconnect()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._paused = False

    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
        """Retrieve the last `count` candles for (asset, interval) from buffer."""
        with self._lock:
            asset_buf = self._buffer.get(asset, {})
            df = asset_buf.get(interval, pd.DataFrame())
            if not df.empty:
                return df.iloc[-count:].copy()

        # Buffer was empty — fetch from REST
        df = fetch_binance_candles(asset, interval, count)
        if not df.empty:
            with self._lock:
                asset_buf = self._buffer.setdefault(asset, {})
                if interval not in asset_buf or asset_buf[interval].empty:
                    asset_buf[interval] = df
        return df

    def _try_seed_asset(self, asset: str) -> bool:
        """Seed all intervals for one asset. Returns True if 5m succeeded.

        If 5m seed fails (asset has no Binance Spot equivalent), promotes the
        asset to co-trigger so it fires via piggyback on the next 5m boundary.
        """
        any_success = False
        for interval, count in self._seed_counts.items():
            success = False
            for attempt in range(3):
                try:
                    df = fetch_binance_candles(asset, interval, count)
                    if df.empty:
                        log.warning(f"[{asset}] Seed {interval}: REST returned empty (attempt {attempt+1}/3)")
                    else:
                        with self._lock:
                            self._buffer.setdefault(asset, {})[interval] = df
                        success = True
                        break
                except Exception as e:
                    log.warning(f"[{asset}] Seed {interval} attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
            if not success:
                log.warning(f"[{asset}] Seed {interval}: all 3 attempts failed or returned empty")
            if success and interval == "5m":
                any_success = True
        if not any_success:
            _mark_as_cotrigger(asset)
        return any_success

    def _seed_buffer(self) -> None:
        """Seed buffer with initial candles. Co-trigger assets are skipped.
        Assets whose 5m seed fails are auto-promoted to co-trigger.
        """
        for asset in self._assets:
            if asset.upper() in _COTRIGGER_ASSETS:
                log.candle(f"[{asset}] Co-trigger asset — skipping Binance seed")
                continue
            self._try_seed_asset(asset)

    def update_assets(self, assets: list[str]) -> None:
        """Update monitored assets and resubscribe WebSocket.

        Seeds: (a) brand-new assets; (b) existing assets without any buffer yet
        (recovery for assets that failed seed previously). Auto-promotes to
        co-trigger if Binance has no equivalent symbol.
        """
        prev_assets = set(self._assets)
        self._assets = list(assets)
        for asset in assets:
            if asset.upper() in _COTRIGGER_ASSETS:
                if asset not in prev_assets:
                    log.candle(f"[{asset}] Co-trigger asset added — skipping Binance seed")
                continue
            with self._lock:
                has_buffer = bool(self._buffer.get(asset))
            if asset in prev_assets and has_buffer:
                continue  # already seeded successfully
            self._try_seed_asset(asset)
        self._reconnect()
