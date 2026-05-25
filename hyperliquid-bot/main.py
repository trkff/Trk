"""
Main bot orchestration.
Runs the scalping bot loop:
  1. Fetch candles (1m, 5m, 15m) for each monitored asset
  2. Compute indicators
  3. Evaluate signals
  4. Execute orders if risk allows
  5. Monitor open positions for TP/SL fills
"""

import json
import time
import sys
import threading
import traceback
from datetime import datetime, timezone

import pandas as pd

from bot import db
from bot.logger import setup_logger, set_debug, get_logger
from bot.exchanges.base import BaseExchangeClient
from bot.exchanges.factory import create_exchange_client
from bot.exchanges.binance_ws import BinanceCandleManager
from bot.exchanges.lighter_ws import LighterCandleManager
from bot.indicators import compute_all
from bot.strategies.manager import evaluate_all, get_required_timeframes, get_active_assets
from bot.executor import open_position, close_position
from bot.risk import RiskManager

log = get_logger("main")

# Global state
client: BaseExchangeClient = create_exchange_client()
risk_mgr: RiskManager | None = None
candle_mgr: BinanceCandleManager | LighterCandleManager | None = None
_stop_event = threading.Event()
_bot_thread: threading.Thread | None = None
_asset_live_status: dict[str, dict] = {}
_status_lock = threading.Lock()


def get_asset_live_status() -> dict:
    with _status_lock:
        return dict(_asset_live_status)


def bot_loop():
    """Main bot loop — event-driven via BinanceCandleManager WebSocket."""
    global risk_mgr, candle_mgr

    cfg = db.get_all_config()
    debug = cfg.get("debug_logging", "false").lower() == "true"
    setup_logger("bot", debug=debug)

    log.info("=" * 50)
    log.info("Hyperliquid Scalping Bot starting...")
    log.info("=" * 50)

    # Connect to exchange with retries
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
        global_assets = json.loads(assets_raw)
    except json.JSONDecodeError:
        global_assets = ["BTC", "ETH", "SOL"]
    initial_assets = get_active_assets(global_assets)

    # Timestamps for 15m/30m/1h/4h/1d candle close detection.
    # Persistidos em SQLite (config table com prefix `last_ts.<tf>.<asset>`)
    # para sobreviver a restart — sem isso, primeira detecção pós-restart era
    # sempre falso positivo (dict vazio → `latest_ts > 0` sempre True), o que
    # disparava estratégias 1h/30m/4h fora do boundary correto.
    last_15m_ts: dict[str, int] = db.get_last_candle_ts("15m")
    last_30m_ts: dict[str, int] = db.get_last_candle_ts("30m")
    last_1h_ts:  dict[str, int] = db.get_last_candle_ts("1h")
    last_4h_ts:  dict[str, int] = db.get_last_candle_ts("4h")
    last_1d_ts:  dict[str, int] = db.get_last_candle_ts("1d")
    if last_1h_ts or last_4h_ts or last_1d_ts:
        log.info(
            f"Restored candle ts cache: 15m={len(last_15m_ts)} 30m={len(last_30m_ts)} "
            f"1h={len(last_1h_ts)} 4h={len(last_4h_ts)} 1d={len(last_1d_ts)} assets"
        )

    def on_candle_close(asset: str, interval: str):
        # process_asset detecta TODOS os TFs internamente via _detect_new (5m, 15m,
        # 30m, 1h, 4h, 1d). Como 15m/30m/1h/4h/1d boundaries são MÚLTIPLOS de 5m,
        # eles sempre coincidem com um 5m boundary — basta disparar no 5m close pra
        # processar tudo. Sem esse gate, o LighterCandleManager (que emite close
        # para CADA tf subscrito) chama process_asset N vezes no mesmo boundary
        # (ex: 23:30 dispara 5m + 30m → duas chamadas → "New 5m candle closed"
        # duplicado por ativo).
        if interval != "5m":
            return
        try:
            current_cfg = db.get_all_config()
            process_asset(asset, current_cfg,
                          last_15m_ts, last_30m_ts, last_1h_ts, last_4h_ts, last_1d_ts)
        except Exception as e:
            log.error(f"[{asset}] on_candle_close error: {e}", exc_info=True)

    active_intervals = get_required_timeframes()
    log.info(f"Active strategy timeframes: {active_intervals}")
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
            if _heartbeat_counter % 2 == 0:  # ~60s with wait(30)
                log.info(f"Bot alive — cycle #{_heartbeat_counter}, monitoring: {candle_mgr._assets}")

            # Check if asset list changed
            assets_raw = cfg.get("monitored_assets", '["BTC","ETH","SOL"]')
            try:
                global_assets = json.loads(assets_raw)
            except json.JSONDecodeError:
                global_assets = ["BTC", "ETH", "SOL"]
            current_assets = get_active_assets(global_assets)
            if set(current_assets) != set(candle_mgr._assets):
                log.info(f"Asset list changed -> {current_assets}")
                candle_mgr.update_assets(current_assets)

            # Check TP/SL on open positions
            risk_mgr.check_open_positions_tp_sl()

            _stop_event.wait(30)

        except Exception as e:
            log.error(f"Bot loop error: {e}", exc_info=True)
            _stop_event.wait(30)

    candle_mgr.stop()
    log.info("Bot stopped.")


def check_bb_mid_exit(asset: str, df_5m) -> None:
    """
    Check open bb_reversion and bb_stoch trades for BB midline exit.
    Called on every new 5m candle close.
    For LONG: exits when candle closes >= BB midline (price returned to centre).
    For SHORT: exits when candle closes <= BB midline.
    The exchange cancels TP/SL trigger orders automatically when position is closed.
    """
    open_trades = db.get_open_trades()
    bb_trades = [t for t in open_trades
                 if t["asset"] == asset and (
                     t.get("strategy", "").startswith("bb_reversion") or
                     t.get("strategy", "").startswith("bb_stoch")
                 )]
    if not bb_trades:
        return

    close = float(df_5m["close"].iloc[-1])

    from bot.strategies.bb_reversion import BBReversionStrategy
    from bot.strategies.bb_stoch import BBStochStrategy
    from bot.strategies.manager import STRATEGY_MAP

    _period_cache: dict[str, int] = {}

    def _get_bb_period(strategy_name: str) -> int:
        if strategy_name in _period_cache:
            return _period_cache[strategy_name]
        if strategy_name.startswith("bb_reversion"):
            strategy = STRATEGY_MAP.get(strategy_name)
            base_params = strategy.DEFAULT_PARAMS if strategy else BBReversionStrategy.DEFAULT_PARAMS
            cfg = db.get_strategy_config(strategy_name)
            params = {**base_params, **cfg["params"]}
            period = int(params.get("bb_period", 10))
        else:  # bb_stoch_*
            strategy = STRATEGY_MAP.get(strategy_name)
            base_params = strategy.DEFAULT_PARAMS if strategy else BBStochStrategy.DEFAULT_PARAMS
            cfg = db.get_strategy_config(strategy_name)
            params = {**base_params, **cfg["params"]}
            _bme = params.get("bb_mid_exit", True)
            if str(_bme).lower() in ("false", "0", "no"):
                _period_cache[strategy_name] = 0
                return 0
            period = int(params.get("bb_period", 15))
        _period_cache[strategy_name] = period
        return period

    for trade in bb_trades:
        strategy_name = trade.get("strategy", "bb_reversion_btc")

        # Check bb_mid_exit flag
        if strategy_name.startswith("bb_reversion"):
            strategy = STRATEGY_MAP.get(strategy_name)
            base_params = strategy.DEFAULT_PARAMS if strategy else BBReversionStrategy.DEFAULT_PARAMS
            cfg = db.get_strategy_config(strategy_name)
            params = {**base_params, **cfg["params"]}
            _bme = params.get("bb_mid_exit", True)
            if str(_bme).lower() in ("false", "0", "no"):
                continue

        bb_period = _get_bb_period(strategy_name)
        if bb_period == 0 or len(df_5m) < bb_period:
            continue

        bb_mid = float(df_5m["close"].rolling(bb_period).mean().iloc[-1])
        side = trade["side"]
        triggered = False

        if side == "long" and close >= bb_mid:
            triggered = True
            log.info(
                f"[{asset}] BB mid exit — LONG close={close:.4f} >= mid={bb_mid:.4f} "
                f"(BB{bb_period} strategy={strategy_name})"
            )
        elif side == "short" and close <= bb_mid:
            triggered = True
            log.info(
                f"[{asset}] BB mid exit — SHORT close={close:.4f} <= mid={bb_mid:.4f} "
                f"(BB{bb_period} strategy={strategy_name})"
            )

        if triggered:
            close_position(client, asset, trade["id"])


def process_asset(asset: str, cfg: dict,
                  last_15m_ts: dict, last_30m_ts: dict,
                  last_1h_ts: dict, last_4h_ts: dict, last_1d_ts: dict):
    """Triggered on every 5m candle close by BinanceCandleManager worker thread."""
    # All timeframes fetched via exchange client (Lighter uses its own REST feed;
    # Hyperliquid delegates to Binance REST — same source as before)
    df_1m = client.get_candles(asset, "1m", count=100)
    df_5m = client.get_candles(asset, "5m", count=500)

    if df_1m.empty or df_5m.empty:
        log.candle(f"[{asset}] No candle data available")
        return

    # Retry once if 5m candle is stale — co-triggered assets (WTI, HYPE) may lag
    # a few seconds behind the Lighter REST after the boundary fires.
    now_ms = int(time.time() * 1000)
    if (now_ms - int(df_5m["timestamp"].iloc[-1])) > 360_000:
        log.candle(f"[{asset}] 5m candle stale (>{(now_ms - int(df_5m['timestamp'].iloc[-1]))//1000}s old), retrying in 5s")
        time.sleep(5)
        df_5m = client.get_candles(asset, "5m", count=500)
        if df_5m.empty:
            log.candle(f"[{asset}] No candle data after retry")
            return

    active = set(candle_mgr.intervals)
    df_15m = client.get_candles(asset, "15m", count=300) if "15m" in active else pd.DataFrame()
    df_30m = client.get_candles(asset, "30m", count=300) if "30m" in active else pd.DataFrame()
    df_1h  = client.get_candles(asset, "1h",  count=300) if "1h"  in active else pd.DataFrame()
    df_4h  = client.get_candles(asset, "4h",  count=300) if "4h"  in active else pd.DataFrame()
    df_1d  = client.get_candles(asset, "1d",  count=300) if "1d"  in active else pd.DataFrame()

    log.candle(f"[{asset}] New 5m candle closed — price={df_5m['close'].iloc[-1]:.2f}")

    # new_5m is always True — called only on 5m candle close
    new_5m = True

    # Detect boundary close de 15m/30m/1h/4h/1d via timestamp do último candle.
    # `tf_key` é o nome usado no DB (15m/30m/1h/4h/1d) — `tf_label` é só pro log.
    def _detect_new(tf_label: str, tf_key: str, df, store: dict) -> bool:
        if df is None or df.empty:
            return False
        latest_ts = int(df["timestamp"].iloc[-1])
        if latest_ts > store.get(asset, 0):
            store[asset] = latest_ts
            db.set_last_candle_ts(tf_key, asset, latest_ts)
            log.info(f"[{asset}] New {tf_label} candle closed")
            return True
        return False

    new_15m = "15m" in active and _detect_new("15m", "15m", df_15m, last_15m_ts)
    new_30m = "30m" in active and _detect_new("30m", "30m", df_30m, last_30m_ts)
    new_1h  = "1h"  in active and _detect_new("1H",  "1h",  df_1h,  last_1h_ts)
    new_4h  = "4h"  in active and _detect_new("4H",  "4h",  df_4h,  last_4h_ts)
    new_1d  = "1d"  in active and _detect_new("1D",  "1d",  df_1d,  last_1d_ts)

    # Merge strategy params into effective cfg
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

    # BB mid exit check — always runs on 5m close
    check_bb_mid_exit(asset, df_5m)

    # Evaluate signals
    signals = evaluate_all(
        asset, indicators, funding_rate, effective_cfg,
        df_1m=df_1m, df_5m=df_5m, df_15m=df_15m, df_30m=df_30m,
        df_4h=df_4h, df_1d=df_1d, df_1h=df_1h,
        new_5m=new_5m, new_15m=new_15m, new_30m=new_30m,
        new_1h=new_1h, new_4h=new_4h, new_1d=new_1d,
    )

    for signal in signals:
        allowed, reason = risk_mgr.can_open_trade(asset)
        if not allowed:
            signal["reason"] = reason
            db.insert_signal(signal)
            continue

        size_usd = risk_mgr.calculate_position_size(asset)
        if size_usd <= 0:
            signal["reason"] = "Position size is 0"
            db.insert_signal(signal)
            continue

        trade_id = open_position(client, signal, size_usd, effective_cfg)
        if trade_id is None:
            signal["reason"] = "Order execution failed"
            db.insert_signal(signal)


def start_bot():
    """Start the bot in a background thread (guards against duplicate threads)."""
    global _bot_thread
    if _bot_thread is not None and _bot_thread.is_alive():
        log.warning("Bot thread already running — ignoring duplicate start_bot() call")
        return _bot_thread
    _stop_event.clear()
    _bot_thread = threading.Thread(target=bot_loop, daemon=True, name="bot-loop")
    _bot_thread.start()
    return _bot_thread


def stop_bot():
    """Signal the bot to stop."""
    _stop_event.set()
    db.set_config("bot_status", "stopped")
    log.info("Stop signal sent to bot")


def pause_bot():
    db.set_config("bot_status", "paused")
    log.info("Bot paused")


def resume_bot():
    db.set_config("bot_status", "running")
    log.info("Bot resumed")


def get_bot_status() -> str:
    return db.get_config("bot_status") or "stopped"


# Allow running standalone
if __name__ == "__main__":
    db.init_db()

    if not db.is_configured():
        print("Bot not configured. Please set credentials via the dashboard (localhost:8080).")
        print("Starting dashboard only...")
        from dashboard.app import create_app
        app, socketio = create_app()
        socketio.run(app, host="0.0.0.0", port=8080, debug=False)
    else:
        setup_logger("bot")
        bot_loop()
