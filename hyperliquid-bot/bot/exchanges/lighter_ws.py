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
