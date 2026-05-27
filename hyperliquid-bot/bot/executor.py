"""
Order execution module.
- Market orders via exchange client (exchange-agnostic)
- TP/SL placement
- Position closing
"""

import threading
import time as _time
from datetime import datetime, timezone

from bot.logger import get_logger
from bot import db
from bot.exchanges.base import BaseExchangeClient

log = get_logger("executor")

# Per-asset locks: serialize open_position calls for the same asset so that
# concurrent workers (e.g., WS push + boundary fallback firing the same
# 5m close) can't both pass the "no open position" check and both insert
# a trade — the duplicate-trade bug. Within the lock we re-check the DB
# for an open trade in this asset and abort if one was just inserted.
_open_locks: dict[tuple[int, str], threading.Lock] = {}
_open_locks_guard = threading.Lock()


def _get_asset_lock(profile_id: int, asset: str) -> threading.Lock:
    """Return a lock keyed by (profile_id, asset).

    Two profiles can open the same asset in the same candle close without
    blocking each other (their accounts and positions are independent), but
    two workers trying to open the same asset for the SAME profile race for
    the lock and only one wins.
    """
    with _open_locks_guard:
        key = (profile_id, asset)
        lock = _open_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _open_locks[key] = lock
        return lock


def _get_fill_data(client: BaseExchangeClient, asset: str, oid: str, since_ms: int) -> dict:
    """Fetch fee and closedPnl from fills API using order ID.

    The order response only returns avgPx/totalSz/oid — fee and closedPnl
    are only available through the fills endpoint.  A single order can
    produce multiple partial fills; this function aggregates all of them.
    Returns {"fee": float, "closedPnl": float}.
    """
    try:
        fills = client.get_recent_fills(asset, since_ms)
        total_fee = 0.0
        total_closed_pnl = 0.0
        found = False
        for f in fills:
            if str(f.get("oid", "")) == str(oid):
                total_fee += float(f.get("fee", 0.0))
                total_closed_pnl += float(f.get("closedPnl", 0.0))
                found = True
        if found:
            return {"fee": total_fee, "closedPnl": total_closed_pnl}
    except Exception as e:
        log.warning(f"[{asset}] Failed to fetch fill data: {e}")
    return {"fee": 0.0, "closedPnl": 0.0}


def round_size(size: float, sz_decimals: int) -> float:
    factor = 10 ** sz_decimals
    return round(size * factor) / factor


def round_price(price: float, sz_decimals: int) -> float:
    sig5 = float(f"{price:.5g}")
    max_dec = max(0, 6 - sz_decimals)
    return round(sig5, max_dec)


def open_position(client: BaseExchangeClient, signal: dict, size_usd: float, cfg: dict,
                  *, profile_id: int = 1) -> int | None:
    """
    Execute a market order for the given signal.
    Places TP and SL as trigger orders.
    Returns trade_id or None on failure.

    Serialized per asset via _open_locks so that concurrent workers triggered
    by the same candle close (WS push + boundary fallback, or queue dedup
    race) cannot both pass the "no open position" gate. Inside the lock we
    re-check db.get_open_trades() — if another worker already inserted a
    trade for this asset, abort without placing a second order.
    """
    asset = signal["asset"]
    side = signal["side"]
    is_buy = side == "long"
    atr = signal["atr"]

    tp_mult = float(signal.get("tp_atr_multiplier") or cfg.get("tp_atr_multiplier", 1.5))
    sl_mult = float(signal.get("sl_atr_multiplier") or cfg.get("sl_atr_multiplier", 1.0))
    slippage = float(cfg.get("slippage", 0.005))

    lock = _get_asset_lock(profile_id, asset)
    if not lock.acquire(blocking=False):
        log.warning(f"[{asset}] open_position already in progress on another thread — skipping duplicate")
        return None

    try:
        # Re-check inside the lock — another worker may have just inserted a trade for this asset.
        if any(t["asset"] == asset for t in db.get_open_trades(profile_id=profile_id)):
            log.warning(f"[{asset}] open_position aborted — open trade already exists for this asset")
            return None

        sz_decimals = client.get_asset_sz_decimals(asset)
        mid_price = client.get_mid_price(asset)
        if mid_price <= 0:
            log.error(f"[{asset}] Invalid mid price: {mid_price}")
            return None

        size = size_usd / mid_price
        size = round_size(size, sz_decimals)
        if size <= 0:
            log.error(f"[{asset}] Calculated size is 0 after rounding")
            return None

        # Calculate TP/SL prices
        if is_buy:
            tp_price = round_price(mid_price + (atr * tp_mult), sz_decimals)
            sl_price = round_price(mid_price - (atr * sl_mult), sz_decimals)
        else:
            tp_price = round_price(mid_price - (atr * tp_mult), sz_decimals)
            sl_price = round_price(mid_price + (atr * sl_mult), sz_decimals)

        # Pre-flight: limpa TP/SL órfãs do asset (sobram quando uma trigger executa e a outra
        # não cancela automaticamente — Lighter não tem OCO nativo). Sem isso, novo market_open
        # pode voltar com `canceled-reduce-only` por conflito com triggers reduce-only órfãs.
        try:
            n_orphan = client.cleanup_orphan_triggers(asset)
            if n_orphan:
                log.info(f"[{asset}] Pre-flight cleanup: {n_orphan} orphan trigger(s) canceled")
        except Exception as e:
            log.warning(f"[{asset}] orphan trigger cleanup failed: {e}")

        # Execute market order
        log.info(f"[{asset}] Opening {side.upper()} — size={size} mid={mid_price:.2f}")
        result = client.market_open(asset, is_buy, size, slippage=slippage)

        # Parse result
        statuses = None
        if isinstance(result, dict):
            statuses = (
                result.get("response", {}).get("data", {}).get("statuses")
                or result.get("statuses")
            )

        if not statuses:
            log.error(f"[{asset}] No statuses in order result: {result}")
            return None

        status = statuses[0]
        if "error" in status:
            log.error(f"[{asset}] Order rejected: {status['error']}")
            return None

        filled = status.get("filled")
        if not filled:
            log.warning(f"[{asset}] Order not filled (IOC expired): {status}")
            return None

        avg_px = float(filled["avgPx"])
        total_sz = float(filled["totalSz"])
        fill_oid = str(filled.get("oid", ""))

        # Order response doesn't include fee; fetch from fills API
        since_ms = int(_time.time() * 1000) - 60_000  # 60s window for API indexing lag
        open_data = _get_fill_data(client, asset, fill_oid, since_ms)
        open_fee = open_data["fee"]

        # Recalculate TP/SL based on actual fill price
        sl_hint = signal.get("sl_price_hint")
        tp_pct_val = signal.get("tp_pct")

        if tp_pct_val is not None:
            # Percentage mode (bb_reversion): TP/SL as % of entry
            tp_pct_f = float(tp_pct_val)
            sl_pct_f = float(signal.get("sl_pct", tp_pct_f / 2))
            if is_buy:
                tp_price = round_price(avg_px * (1 + tp_pct_f), sz_decimals)
                sl_price = round_price(avg_px * (1 - sl_pct_f), sz_decimals)
            else:
                tp_price = round_price(avg_px * (1 - tp_pct_f), sz_decimals)
                sl_price = round_price(avg_px * (1 + sl_pct_f), sz_decimals)
        elif sl_hint is not None:
            # Risk/reward mode: SL from candle low/high, TP from rr_ratio
            sl_price = round_price(float(sl_hint), sz_decimals)
            risk = abs(avg_px - sl_price)
            rr = float(signal.get("rr_ratio", 1.5))
            if is_buy:
                tp_price = round_price(avg_px + risk * rr, sz_decimals)
            else:
                tp_price = round_price(avg_px - risk * rr, sz_decimals)
        elif is_buy:
            tp_price = round_price(avg_px + (atr * tp_mult), sz_decimals)
            sl_price = round_price(avg_px - (atr * sl_mult), sz_decimals)
        else:
            tp_price = round_price(avg_px - (atr * tp_mult), sz_decimals)
            sl_price = round_price(avg_px + (atr * sl_mult), sz_decimals)

        signal_price = signal.get("signal_price")
        if signal_price:
            slip_pct = (avg_px - signal_price) / signal_price * 100
            if not is_buy:
                slip_pct = -slip_pct
            log.info(
                f"[{asset}] {side.upper()} filled — size={total_sz} @ {avg_px:.4f} "
                f"signal={signal_price:.4f} slippage={slip_pct:+.4f}% "
                f"TP={tp_price:.4f} SL={sl_price:.4f}"
            )
        else:
            log.info(
                f"[{asset}] {side.upper()} filled — size={total_sz} @ {avg_px:.4f} "
                f"TP={tp_price:.4f} SL={sl_price:.4f}"
            )

        # Place TP and SL trigger orders
        client.place_tp_sl(asset, not is_buy, total_sz, tp_price, sl_price, sz_decimals)

        # Record trade in DB
        now = datetime.now(timezone.utc).isoformat()
        trade_id = db.insert_trade({
            "profile_id": profile_id,
            "asset": asset,
            "side": side,
            "entry_price": avg_px,
            "signal_price": signal.get("signal_price"),
            "size": total_sz,
            "entry_time": now,
            "ema9": signal.get("ema9"),
            "ema21": signal.get("ema21"),
            "rsi2": signal.get("rsi2"),
            "volume": signal.get("volume"),
            "atr": atr,
            "funding_rate": signal.get("funding_rate"),
            "tp_price": tp_price,
            "sl_price": sl_price,
            "order_id": fill_oid,
            "open_fee": open_fee,
            "strategy": signal.get("strategy_name", "mean_reversion"),
        })

        # Mark signal as executed
        signal["executed"] = 1
        signal["profile_id"] = profile_id
        db.insert_signal(signal)

        return trade_id

    except Exception as e:
        log.error(f"[{asset}] Failed to execute {side}: {e}", exc_info=True)
        return None
    finally:
        lock.release()


def _get_close_pnl_fallback(client: BaseExchangeClient, asset: str, since_ms: int) -> dict:
    """Sum closedPnl and fee from all fills since since_ms (no oid filter).
    Used when oid matching fails (e.g. Lighter txHash vs tradeId mismatch).
    """
    try:
        fills = client.get_recent_fills(asset, since_ms)
        total_fee = sum(float(f.get("fee", 0.0)) for f in fills)
        total_pnl = sum(float(f.get("closedPnl", 0.0)) for f in fills)
        if fills:
            return {"fee": total_fee, "closedPnl": total_pnl}
    except Exception as e:
        log.warning(f"[{asset}] Failed to fetch fallback fill data: {e}")
    return {"fee": 0.0, "closedPnl": 0.0}


def close_position(client: BaseExchangeClient, asset: str, trade_id: int,
                   *, profile_id: int = 1):
    """Close a position at market price and update the trade record."""
    try:
        pre_close_ms = int(_time.time() * 1000)
        result = client.market_close(asset)

        statuses = None
        if isinstance(result, dict):
            statuses = (
                result.get("response", {}).get("data", {}).get("statuses")
                or result.get("statuses")
            )

        exit_price = 0.0
        close_oid = ""
        if statuses and statuses[0].get("filled"):
            filled = statuses[0]["filled"]
            exit_price = float(filled["avgPx"])
            close_oid = str(filled.get("oid", ""))

        # Get the trade to compute PnL
        trades = db.get_open_trades(profile_id=profile_id)
        trade = next((t for t in trades if t["id"] == trade_id), None)
        if trade:
            entry_px = trade["entry_price"]
            size = trade["size"]

            # Fetch actual fees + closedPnl from fills API
            try:
                since_ms = int(datetime.fromisoformat(trade["entry_time"]).timestamp() * 1000)
            except (ValueError, TypeError):
                since_ms = int(_time.time() * 1000) - 600_000
            stored_oid = str(trade.get("order_id") or "")
            open_data = _get_fill_data(client, asset, stored_oid, since_ms) if stored_oid else {"fee": 0.0, "closedPnl": 0.0}
            close_data = _get_fill_data(client, asset, close_oid, since_ms) if close_oid else {"fee": 0.0, "closedPnl": 0.0}

            # Fallback: se o oid não casou (ex: Lighter usa tradeId mas market_close
            # retorna txHash), soma o closedPnl de todos os fills da janela do trade
            # (entry → close). Janela ampla é necessária porque o fill na Lighter
            # pode ter ts segundos ANTES do `pre_close_ms` (matching engine time,
            # não o instante em que o bot detectou o close — caso real: bb_mid_exit
            # disparou às 21:20:43 mas o fill já existia na Lighter desde 21:20:23,
            # antes da janela `pre_close_ms - 5s`). O fill de OPEN tem closedPnl=0
            # na Lighter, então somar não distorce.
            if close_data["closedPnl"] == 0.0 and close_data["fee"] == 0.0:
                close_data = _get_close_pnl_fallback(client, asset, since_ms)

            total_fees = open_data["fee"] + close_data["fee"]

            # closedPnl da HL = lucro bruto (sem fees). PnL real = closedPnl - fees
            closed_pnl = close_data["closedPnl"]

            # Fetch funding payments within the trade's hold period
            net_funding = 0.0
            try:
                trade_entry_ms = since_ms
                trade_exit_ms = int(_time.time() * 1000)
                funding_records = client.get_user_funding_history(trade_entry_ms)
                close_side_sign = 1 if trade["side"] == "long" else -1
                for fp in funding_records:
                    d = fp.get("delta", {})
                    fp_time = int(fp.get("time", 0))
                    if (d.get("coin") == asset
                            and float(d.get("szi", 0)) * close_side_sign > 0
                            and trade_entry_ms <= fp_time <= trade_exit_ms):
                        net_funding += float(d.get("usdc", 0))
            except Exception as fe:
                log.warning(f"[{asset}] Could not fetch funding: {fe}")

            pnl = closed_pnl - total_fees + net_funding
            pnl_pct = (pnl / (entry_px * size)) * 100 if entry_px * size > 0 else 0

            db.close_trade(trade_id, exit_price, round(pnl, 4), round(pnl_pct, 2),
                           fees=round(total_fees, 6), funding=round(net_funding, 6))
            log.info(f"[{asset}] Position closed — exit={exit_price:.4f} PnL=${pnl:.2f} ({pnl_pct:.2f}%) fees=${total_fees:.4f} funding=${net_funding:.4f}")
        else:
            log.warning(f"[{asset}] Trade {trade_id} not found in open trades")

    except Exception as e:
        log.error(f"[{asset}] Failed to close position: {e}", exc_info=True)


def check_position_exists(client: BaseExchangeClient, asset: str) -> bool:
    """Check if we have an open position for this asset on the exchange."""
    positions = client.get_open_positions()
    return any(p["coin"] == asset for p in positions)
