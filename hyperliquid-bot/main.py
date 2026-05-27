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
from concurrent.futures import ThreadPoolExecutor
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

# ── Global state ──────────────────────────────────────────────────────────
#
# Each profile owns its own exchange client, risk manager and stop event.
# The candle manager is a shared singleton fed by the union of every running
# profile's assets — restored/updated via `_refresh_candle_manager_assets`.
#
# All mutations of these dicts happen under `_bot_lock`. Readers (e.g. the
# candle callback dispatch) snapshot the keys under the lock and iterate
# outside it so worker threads do not block bot start/stop.

_bot_threads: dict[int, threading.Thread] = {}
_bot_clients: dict[int, BaseExchangeClient] = {}
_risk_mgrs: dict[int, RiskManager] = {}
_stop_events: dict[int, threading.Event] = {}
_bot_lock = threading.Lock()

candle_mgr: BinanceCandleManager | LighterCandleManager | None = None
_candle_mgr_owner_pid: int | None = None  # profile whose client the LighterCandleManager is using
_candle_mgr_lock = threading.Lock()

# Fans `_on_candle_close_dispatch` work out across profiles so a slow
# process_asset on one profile doesn't delay the others. Bounded at 8
# concurrent workers — enough for a handful of profiles, low enough to
# avoid hammering the Lighter REST with simultaneous get_candles bursts.
_dispatch_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dispatch")

# Higher-TF boundaries that closed more than this many seconds ago do NOT
# trigger a strategy evaluation — only the store is updated so the bot
# doesn't keep re-detecting them. Prevents a restart-induced "catch-up"
# where the bot fires signals from candles that closed minutes ago, with
# a stale price baseline that the strategy backtest never saw.
_TF_STALENESS_SEC = 120
_TF_INTERVAL_MS: dict[str, int] = {
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h":  60 * 60 * 1000,
    "4h":  4 * 60 * 60 * 1000,
    "1d":  24 * 60 * 60 * 1000,
}

_asset_live_status: dict[str, dict] = {}
_status_lock = threading.Lock()


def get_asset_live_status() -> dict:
    with _status_lock:
        return dict(_asset_live_status)


def _build_client_for_profile(profile_id: int) -> BaseExchangeClient:
    """Instantiate the exchange client for this profile and call .connect().

    The factory reads credentials from the profile row (Phase 4) — Phase 2's
    fallback to global config still works because the legacy keys were not
    deleted by M8. Callers wrap this in a try/except to surface auth errors
    cleanly back to the dashboard.
    """
    return create_exchange_client(profile_id=profile_id)


def _union_assets() -> list[str]:
    """Sorted union of assets across every subscribed profile (running OR starting).

    Used to drive the shared candle manager's subscription list. Includes
    'starting' so a freshly-spawned profile gets its assets subscribed
    before its own bot_loop flips to "running".
    """
    seen: set[str] = set()
    pids = _subscribed_profile_ids()
    for pid in pids:
        # 1. Profile-scoped global assets list (config UI / sizing tab)
        raw = db.get_profile_config(pid, "assets") or db.get_config("monitored_assets") or "[]"
        try:
            seen.update(json.loads(raw))
        except json.JSONDecodeError:
            pass
        # 2. Plus any assets pinned by the profile's enabled strategies
        try:
            seen.update(get_active_assets(list(seen), profile_id=pid))
        except Exception:
            log.exception("union_assets: failed to read strategies for profile %s", pid)
    return sorted(seen)


def _running_profile_ids() -> list[int]:
    """Profiles whose worker thread is alive AND whose stored status is 'running'.

    Used by `_on_candle_close_dispatch` — only fully-connected workers should
    receive process_asset calls.
    """
    with _bot_lock:
        candidates = [pid for pid, t in _bot_threads.items() if t.is_alive()]
    return [pid for pid in candidates
            if db.get_profile_config(pid, "bot_status") == "running"]


def _subscribed_profile_ids() -> list[int]:
    """Profiles whose assets/intervals the candle manager should subscribe to.

    Includes both 'running' and 'starting' — a profile entering "starting"
    state has its client already registered in `_bot_clients`, and its
    candle subscriptions should be ready by the time `bot_loop` finishes
    connecting and flips status to "running". If the candle manager were
    built only against currently-running profiles, the first `start_bot`
    of a profile would race ahead of its own status flip and the manager
    would end up subscribed to just `{"5m"}` even when the profile has
    15m/1h strategies enabled.
    """
    with _bot_lock:
        candidates = [pid for pid, t in _bot_threads.items() if t.is_alive()]
    return [pid for pid in candidates
            if db.get_profile_config(pid, "bot_status") in ("running", "starting")]


def _required_intervals_union() -> list[str]:
    """Union of timeframes required across all profiles being subscribed. 5m always in."""
    tfs: set[str] = {"5m"}
    for pid in _subscribed_profile_ids():
        try:
            tfs.update(get_required_timeframes(profile_id=pid))
        except Exception:
            log.exception("required_intervals_union: profile %s", pid)
    return sorted(tfs)


def _refresh_candle_manager_assets():
    """Create/update/teardown the singleton candle manager based on active profiles.

    Safe to call concurrently — `_candle_mgr_lock` serializes lifecycle changes.
    Also handles the LighterCandleManager's owner-client lifecycle: if the
    profile whose client the manager is holding gets reaped, we tear the
    manager down and respawn it bound to a still-running profile's client.
    """
    global candle_mgr, _candle_mgr_owner_pid
    with _candle_mgr_lock:
        union = _union_assets()
        intervals = _required_intervals_union()
        if not union:
            if candle_mgr is not None:
                try:
                    candle_mgr.stop()
                except Exception:
                    log.exception("Error stopping candle manager")
                candle_mgr = None
                _candle_mgr_owner_pid = None
                log.info("Candle manager torn down — no running profiles")
            return

        cfg = db.get_all_config()
        selected_exchange = cfg.get("selected_exchange", "lighter")
        use_lighter_ws = (cfg.get("use_lighter_ws_candles", "true").lower() == "true")

        # If the LighterCandleManager's owner client was reaped (no longer in
        # _bot_clients), tear it down so we rebuild with a still-running
        # profile's client below.
        if (
            candle_mgr is not None
            and _candle_mgr_owner_pid is not None
            and _candle_mgr_owner_pid not in _bot_clients
        ):
            log.info(
                "Candle manager owner profile %s was reaped — rebuilding with new owner",
                _candle_mgr_owner_pid,
            )
            try:
                candle_mgr.stop()
            except Exception:
                log.exception("Error stopping stale candle manager")
            candle_mgr = None
            _candle_mgr_owner_pid = None

        # If the active timeframes differ from what the manager subscribed to,
        # tear down and rebuild — update_assets only refreshes the asset list,
        # not the intervals. This happens when a profile enters "starting"
        # before the candle manager was first built and the manager only saw
        # the {"5m"} fallback, then later the profile flips to "running" with
        # 15m/30m/1h strategies that need their own WS subscriptions.
        if candle_mgr is not None:
            current_intervals = set(getattr(candle_mgr, "intervals", ()) or ())
            wanted_intervals = set(intervals)
            if current_intervals and current_intervals != wanted_intervals:
                log.info(
                    "Candle manager intervals changed %s -> %s, rebuilding",
                    sorted(current_intervals), sorted(wanted_intervals),
                )
                try:
                    candle_mgr.stop()
                except Exception:
                    log.exception("Error stopping candle manager for interval rebuild")
                candle_mgr = None
                _candle_mgr_owner_pid = None

        if candle_mgr is None:
            # Lighter REST is auth-less for candle reads — any running profile's
            # client works. Pick the lowest profile_id deterministically so logs
            # stay readable across restarts.
            running_pids = sorted(pid for pid in _bot_clients if pid in _bot_threads)
            sample_client = _bot_clients.get(running_pids[0]) if running_pids else None
            if selected_exchange == "lighter" and use_lighter_ws and sample_client is not None:
                log.info("Spawning shared LighterCandleManager (owner=profile %s) assets=%s intervals=%s",
                         running_pids[0], union, intervals)
                candle_mgr = LighterCandleManager(
                    client=sample_client,
                    assets=union,
                    intervals=intervals,
                    on_candle_close=_on_candle_close_dispatch,
                )
                _candle_mgr_owner_pid = running_pids[0]
            else:
                log.info("Spawning shared BinanceCandleManager assets=%s intervals=%s", union, intervals)
                candle_mgr = BinanceCandleManager(
                    union, on_candle_close=_on_candle_close_dispatch,
                    intervals=intervals,
                )
                _candle_mgr_owner_pid = None  # Binance manager doesn't hold a profile client
            candle_mgr.start()
        else:
            try:
                candle_mgr.update_assets(union)
            except Exception:
                log.exception("update_assets failed")


def _build_cfg_for_profile(profile_id: int) -> dict:
    """Per-profile cfg dict (risk + sizing + global flags) the worker uses."""
    cfg = dict(db.get_all_config())  # base globals (debug flags, fee_rate, etc.)
    for key in ("risk", "sizing"):
        raw = db.get_profile_config(profile_id, key)
        if raw:
            try:
                cfg[key] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return cfg


def _on_candle_close_dispatch(asset: str, interval: str):
    """Singleton callback wired into the shared candle manager.

    Gated to 5m — 15m/30m/1h/4h/1d boundaries always coincide with a 5m close
    and `process_asset` detects each higher TF internally. Fan-outs to every
    running profile that watches this asset.
    """
    if interval != "5m":
        return
    # Snapshot ts caches OUTSIDE the lock to avoid blocking the candle thread.
    # Each profile receives an INDEPENDENT COPY of every dict — otherwise the
    # first profile to process a shared asset (e.g. CRCL watched by both Default
    # and "15 min") would mutate the shared store and the second profile would
    # see latest_ts <= store, so its `new_15m`/`new_30m`/`new_1h` would be False
    # and its 15m+ strategies would never fire on that candle.
    last_15m_ts = db.get_last_candle_ts("15m")
    last_30m_ts = db.get_last_candle_ts("30m")
    last_1h_ts  = db.get_last_candle_ts("1h")
    last_4h_ts  = db.get_last_candle_ts("4h")
    last_1d_ts  = db.get_last_candle_ts("1d")
    # Fan-out per profile via the bounded thread pool. Each profile's
    # process_asset runs concurrently with the others — a slow REST seed on
    # profile 1 no longer delays profile 2's signal evaluation. Within a
    # profile the call is still serial (no per-profile concurrency needed —
    # one candle close = one work unit).
    def _run_for_profile(pid: int):
        client = _bot_clients.get(pid)
        if client is None:
            return
        cfg = _build_cfg_for_profile(pid)
        # Profile filter: only act if this asset is in the profile's universe.
        try:
            assets_raw = db.get_profile_config(pid, "assets") or cfg.get("monitored_assets", "[]")
            profile_assets = json.loads(assets_raw)
        except json.JSONDecodeError:
            profile_assets = []
        active_assets = set(get_active_assets(profile_assets, profile_id=pid))
        if asset not in active_assets:
            return
        try:
            # Pass shallow copies — each profile mutates its own dict.
            # Mutations are still persisted to the shared DB (idempotent: both
            # profiles write the same ts for the same boundary).
            process_asset(
                asset, cfg,
                dict(last_15m_ts), dict(last_30m_ts), dict(last_1h_ts),
                dict(last_4h_ts), dict(last_1d_ts),
                profile_id=pid,
            )
        except Exception:
            log.exception("[%s] process_asset failed for profile %s", asset, pid)

    pids = _running_profile_ids()
    if not pids:
        return
    if len(pids) == 1:
        _run_for_profile(pids[0])
        return
    # Submit and let workers run independently — we don't await results so
    # the candle manager's worker thread returns immediately.
    for pid in pids:
        try:
            _dispatch_pool.submit(_run_for_profile, pid)
        except RuntimeError:
            # Pool was shut down (e.g. during process exit) — fall back to
            # running synchronously so the close isn't dropped.
            _run_for_profile(pid)


def bot_loop(profile_id: int = 1):
    """Per-profile worker loop.

    Connects this profile's exchange client, builds its RiskManager and runs
    the heartbeat that polls bot_status, TP/SL recovery and asset list changes.
    The candle manager is a singleton owned by `_refresh_candle_manager_assets`
    — this function does not create or tear it down; it only requests refreshes
    when its own asset list shifts.
    """
    stop_event = _stop_events.get(profile_id)
    if stop_event is None:
        log.error("bot_loop called for profile %s without a stop_event registered", profile_id)
        return

    cfg = db.get_all_config()
    debug = cfg.get("debug_logging", "false").lower() == "true"
    setup_logger("bot", debug=debug)

    log.info("=" * 50)
    log.info(f"Hyperliquid Scalping Bot starting (profile {profile_id})...")
    log.info("=" * 50)

    client = _bot_clients.get(profile_id)
    if client is None:
        log.error("bot_loop: no client registered for profile %s", profile_id)
        return

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
                db.set_profile_config(profile_id, "bot_status", "error")
                return
            log.warning(f"Retrying connection in {delay}s...")
            stop_event.wait(delay)
            if stop_event.is_set():
                db.set_profile_config(profile_id, "bot_status", "stopped")
                return

    risk_mgr = RiskManager(client, profile_id=profile_id)
    _risk_mgrs[profile_id] = risk_mgr
    db.set_profile_config(profile_id, "bot_status", "running")

    cfg = db.get_all_config()
    assets_raw = cfg.get("monitored_assets", '["BTC","ETH","SOL"]')
    try:
        global_assets = json.loads(assets_raw)
    except json.JSONDecodeError:
        global_assets = ["BTC", "ETH", "SOL"]
    initial_assets = get_active_assets(global_assets, profile_id=profile_id)

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

    # Subscribe this profile's assets/timeframes on the shared candle manager.
    _refresh_candle_manager_assets()

    _heartbeat_counter = 0
    _last_asset_set: set[str] = set(initial_assets)

    while not stop_event.is_set():
        try:
            cfg = db.get_all_config()
            status = db.get_profile_config(profile_id, "bot_status") or "running"

            if status == "stopped":
                log.info("Bot stopped via dashboard")
                break

            if status == "paused":
                # Shared candle manager keeps running for other profiles; this
                # loop just idles until status flips back to "running" or stops.
                stop_event.wait(5)
                continue

            set_debug(cfg.get("debug_logging", "false").lower() == "true")

            _heartbeat_counter += 1
            if _heartbeat_counter % 2 == 0:  # ~60s with wait(30)
                log.info(f"[profile {profile_id}] Bot alive — cycle #{_heartbeat_counter}, "
                         f"monitoring: {sorted(_last_asset_set)}")

            # Check if this profile's asset list changed; refresh the shared candle
            # manager so the union covers the new assets.
            assets_raw = cfg.get("monitored_assets", '["BTC","ETH","SOL"]')
            try:
                global_assets = json.loads(assets_raw)
            except json.JSONDecodeError:
                global_assets = ["BTC", "ETH", "SOL"]
            current_assets = set(get_active_assets(global_assets, profile_id=profile_id))
            if current_assets != _last_asset_set:
                log.info(f"[profile {profile_id}] Asset list changed -> {sorted(current_assets)}")
                _last_asset_set = current_assets
                _refresh_candle_manager_assets()

            # Check TP/SL on open positions
            risk_mgr.check_open_positions_tp_sl()

            stop_event.wait(30)

        except Exception as e:
            log.error(f"Bot loop error: {e}", exc_info=True)
            stop_event.wait(30)

    log.info(f"Bot stopped for profile {profile_id}.")


def check_bb_mid_exit(asset: str, df_5m, profile_id: int = 1) -> None:
    """
    Check open bb_reversion and bb_stoch trades for BB midline exit.
    Called on every new 5m candle close.
    For LONG: exits when candle closes >= BB midline (price returned to centre).
    For SHORT: exits when candle closes <= BB midline.
    The exchange cancels TP/SL trigger orders automatically when position is closed.
    """
    client = _bot_clients.get(profile_id)
    if client is None:
        return
    open_trades = db.get_open_trades(profile_id=profile_id)
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
            cfg = db.get_strategy_config(strategy_name, profile_id=profile_id)
            params = {**base_params, **cfg["params"]}
            period = int(params.get("bb_period", 10))
        else:  # bb_stoch_*
            strategy = STRATEGY_MAP.get(strategy_name)
            base_params = strategy.DEFAULT_PARAMS if strategy else BBStochStrategy.DEFAULT_PARAMS
            cfg = db.get_strategy_config(strategy_name, profile_id=profile_id)
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
            cfg = db.get_strategy_config(strategy_name, profile_id=profile_id)
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
            close_position(client, asset, trade["id"], profile_id=profile_id)


def process_asset(asset: str, cfg: dict,
                  last_15m_ts: dict, last_30m_ts: dict,
                  last_1h_ts: dict, last_4h_ts: dict, last_1d_ts: dict,
                  profile_id: int = 1):
    """Triggered on every 5m candle close by the shared candle manager.

    Looks up the per-profile exchange client and risk manager from the global
    dicts populated by `start_bot(profile_id)`. Returns silently if either is
    missing — this can happen if a bot was stopped between the candle close
    fire and dispatch.
    """
    client = _bot_clients.get(profile_id)
    risk_mgr = _risk_mgrs.get(profile_id)
    if client is None or risk_mgr is None:
        return
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

    active = set(candle_mgr.intervals) if candle_mgr is not None else {"5m"}
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
    # Staleness guard: if the candle closed more than _TF_STALENESS_SEC ago we
    # update the store (so we don't keep re-detecting it on every subsequent
    # 5m close) but return False — no strategy evaluation. This blocks
    # restart-induced "catch-up" signals where a 1h candle closed during the
    # downtime and the first 5m close post-restart would otherwise fire it
    # against a price that's already minutes stale.
    def _detect_new(tf_label: str, tf_key: str, df, store: dict) -> bool:
        if df is None or df.empty:
            return False
        latest_ts = int(df["timestamp"].iloc[-1])
        if latest_ts <= store.get(asset, 0):
            return False
        store[asset] = latest_ts
        db.set_last_candle_ts(tf_key, asset, latest_ts)
        interval_ms = _TF_INTERVAL_MS.get(tf_key, 0)
        close_ts = latest_ts + interval_ms
        age_sec = (now_ms - close_ts) / 1000.0
        if age_sec > _TF_STALENESS_SEC:
            log.warning(
                f"[{asset}] {tf_label} candle close was {age_sec:.0f}s ago "
                f"(> {_TF_STALENESS_SEC}s threshold) — skipping evaluation to avoid stale signal"
            )
            return False
        log.info(f"[{asset}] New {tf_label} candle closed")
        return True

    new_15m = "15m" in active and _detect_new("15m", "15m", df_15m, last_15m_ts)
    new_30m = "30m" in active and _detect_new("30m", "30m", df_30m, last_30m_ts)
    new_1h  = "1h"  in active and _detect_new("1H",  "1h",  df_1h,  last_1h_ts)
    new_4h  = "4h"  in active and _detect_new("4H",  "4h",  df_4h,  last_4h_ts)
    new_1d  = "1d"  in active and _detect_new("1D",  "1d",  df_1d,  last_1d_ts)

    # Merge strategy params into effective cfg
    mr_params = db.get_strategy_config("mean_reversion", profile_id=profile_id).get("params", {})
    vr_params = db.get_strategy_config("vwap_reversion", profile_id=profile_id).get("params", {})
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
    check_bb_mid_exit(asset, df_5m, profile_id=profile_id)

    # Evaluate signals
    signals = evaluate_all(
        asset, indicators, funding_rate, effective_cfg,
        df_1m=df_1m, df_5m=df_5m, df_15m=df_15m, df_30m=df_30m,
        df_4h=df_4h, df_1d=df_1d, df_1h=df_1h,
        new_5m=new_5m, new_15m=new_15m, new_30m=new_30m,
        new_1h=new_1h, new_4h=new_4h, new_1d=new_1d,
        profile_id=profile_id,
    )

    for signal in signals:
        signal["profile_id"] = profile_id
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

        trade_id = open_position(client, signal, size_usd, effective_cfg, profile_id=profile_id)
        if trade_id is None:
            signal["reason"] = "Order execution failed"
            db.insert_signal(signal)


def start_bot(profile_id: int = 1):
    """Spawn this profile's worker thread.

    Idempotent: returns the existing thread if one is already running for the
    given profile_id. Builds a fresh exchange client and stop event for the
    profile, persists them in the global dicts, then triggers a candle manager
    refresh so the new asset universe gets subscribed.

    Sets `bot_status="starting"` before the thread spawns; `bot_loop` flips it
    to `"running"` only after `client.connect()` succeeds. This closes the
    race where `_on_candle_close_dispatch` saw status==running and called
    `process_asset` against a client that hadn't completed auth yet.
    """
    with _bot_lock:
        existing = _bot_threads.get(profile_id)
        if existing is not None and existing.is_alive():
            log.warning("Bot thread already running for profile %s — ignoring start_bot()", profile_id)
            return existing
        try:
            _bot_clients[profile_id] = _build_client_for_profile(profile_id)
        except Exception:
            log.exception("Failed to build exchange client for profile %s", profile_id)
            db.set_profile_config(profile_id, "bot_status", "error")
            return None
        _stop_events[profile_id] = threading.Event()
        # Intermediate state — dispatch filters on "running" so candle fan-out
        # waits for bot_loop's own status flip after a successful connect.
        db.set_profile_config(profile_id, "bot_status", "starting")
        t = threading.Thread(
            target=bot_loop, args=(profile_id,),
            daemon=True, name=f"bot-loop-p{profile_id}",
        )
        _bot_threads[profile_id] = t
        t.start()
    _refresh_candle_manager_assets()
    return t


def stop_bot(profile_id: int = 1):
    """Signal this profile's worker to stop and schedule reaper cleanup.

    The reaper thread joins the worker (with a timeout), tears down the client,
    removes the profile from the dicts and refreshes the candle manager (which
    will stop the singleton if no profile remains).
    """
    with _bot_lock:
        ev = _stop_events.get(profile_id)
    if ev is not None:
        ev.set()
    db.set_profile_config(profile_id, "bot_status", "stopped")
    log.info("Stop signal sent to bot of profile %s", profile_id)
    threading.Thread(
        target=_reap_bot_thread, args=(profile_id,),
        daemon=True, name=f"bot-reaper-p{profile_id}",
    ).start()


def pause_bot(profile_id: int = 1):
    db.set_profile_config(profile_id, "bot_status", "paused")
    log.info("Bot paused (profile %s)", profile_id)


def resume_bot(profile_id: int = 1):
    db.set_profile_config(profile_id, "bot_status", "running")
    log.info("Bot resumed (profile %s)", profile_id)


def get_bot_status(profile_id: int = 1) -> str:
    return db.get_profile_config(profile_id, "bot_status") or "stopped"


def _reap_bot_thread(profile_id: int):
    """Join the worker, disconnect its client and clean up dicts.

    Called from `stop_bot` in a background thread so HTTP /api/.../stop can
    return immediately. Best-effort: a stuck worker is logged but does not
    block the reaper from cleaning the dicts after the join timeout.
    """
    with _bot_lock:
        t = _bot_threads.get(profile_id)
    if t is not None:
        t.join(timeout=15)
        if t.is_alive():
            log.warning("Bot thread for profile %s did not exit within 15s — abandoning", profile_id)
    with _bot_lock:
        client = _bot_clients.pop(profile_id, None)
        _bot_threads.pop(profile_id, None)
        _stop_events.pop(profile_id, None)
        _risk_mgrs.pop(profile_id, None)
    if client is not None:
        try:
            client.disconnect()
        except Exception:
            log.exception("Disconnect failed for profile %s", profile_id)
    _refresh_candle_manager_assets()


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
