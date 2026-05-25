"""Standalone Lighter WS sanity test.

Connects to mainnet stream, subscribes to BTC 5m candles, prints every raw
message received. Runs for 150s (long enough to cross the 2-min keepalive
threshold and catch a close if one happens).

Usage: python test_lighter_ws_raw.py
"""

import json
import time
import threading
import websocket


WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
MARKET_ID = 0  # BTC on Lighter mainnet
RESOLUTION = "5m"

_msg_count = 0
_start_ts = time.time()


def _ts() -> str:
    return f"[{time.time() - _start_ts:6.2f}s]"


def on_open(ws):
    """Send 63 subscribes back-to-back like the manager does — stress test."""
    print(f"{_ts()} OPEN — sending 63 subscribes (3 TFs × 21 markets)…", flush=True)
    sent = 0
    for market in range(21):  # market_ids 0..20
        for tf in ("5m", "30m", "1h"):
            sub = {"type": "subscribe", "channel": f"candle/{market}/{tf}"}
            ws.send(json.dumps(sub))
            sent += 1
    print(f"{_ts()} sent {sent} subscribes", flush=True)


def on_message(ws, raw):
    global _msg_count
    _msg_count += 1
    # Print first 500 chars of every message
    snippet = raw[:500] + ("…" if len(raw) > 500 else "")
    print(f"{_ts()} MSG #{_msg_count}: {snippet}", flush=True)

    # Try to handle ping → pong
    try:
        msg = json.loads(raw)
        if msg.get("type") == "ping":
            ws.send(json.dumps({"type": "pong"}))
            print(f"{_ts()} → sent pong", flush=True)
    except Exception as e:
        print(f"{_ts()} parse err: {e}", flush=True)


def on_error(ws, err):
    print(f"{_ts()} ERROR: {err}", flush=True)


def on_close(ws, code, msg):
    print(f"{_ts()} CLOSED code={code} msg={msg!r} (received {_msg_count} msgs total)", flush=True)


def main():
    print(f"{_ts()} connecting to {WS_URL}…", flush=True)
    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    # Run for 150 seconds then close
    def kill():
        time.sleep(150)
        print(f"{_ts()} 150s elapsed — closing manually", flush=True)
        ws.close()

    threading.Thread(target=kill, daemon=True).start()
    ws.run_forever(ping_interval=30, ping_timeout=10)
    print(f"{_ts()} done. total msgs: {_msg_count}", flush=True)


if __name__ == "__main__":
    main()
