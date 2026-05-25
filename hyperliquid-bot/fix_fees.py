"""
Script to fix fees on all closed trades.

Uses order_id (oid) for precise open-fill matching, and dir+time for close-fill.
Recalculates PnL with corrected fees.

Usage:
    cd hyperliquid-bot
    python fix_fees.py          # fix only zero-fee trades (default)
    python fix_fees.py --all    # recalculate fees for ALL closed trades
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bot import db
from bot.exchanges.hyperliquid import HyperliquidClient


def parse_iso_ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def closest(fills: list[dict], target_ms: int) -> dict | None:
    """Return the fill whose time is closest to target_ms."""
    if not fills:
        return None
    return min(fills, key=lambda f: abs(int(f.get("time", 0)) - target_ms))


def fix_fees():
    recalc_all = "--all" in sys.argv

    db.init_db()

    if not db.is_configured():
        print("Bot not configured. Set credentials via the dashboard first.")
        sys.exit(1)

    client = HyperliquidClient()
    try:
        client.connect()
    except Exception as e:
        print(f"Failed to connect to Hyperliquid: {e}")
        sys.exit(1)

    conn = db.get_conn()

    if recalc_all:
        rows = conn.execute("""
            SELECT * FROM trades
            WHERE status = 'closed'
              AND exit_price IS NOT NULL AND exit_price > 0
              AND entry_time IS NOT NULL AND exit_time IS NOT NULL
            ORDER BY entry_time ASC
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM trades
            WHERE status = 'closed'
              AND (fees IS NULL OR fees = 0.0)
              AND exit_price IS NOT NULL AND exit_price > 0
              AND entry_time IS NOT NULL AND exit_time IS NOT NULL
            ORDER BY entry_time ASC
        """).fetchall()

    trades = [dict(r) for r in rows]

    if not trades:
        print("No trades to fix.")
        client.disconnect()
        return

    mode = "ALL closed" if recalc_all else "zero-fee"
    print(f"Found {len(trades)} {mode} trades. Backfilling...\n")

    # Fetch all fills once for the full period
    try:
        since_ms = parse_iso_ms(trades[0]["entry_time"]) - 60_000
        until_ms = parse_iso_ms(trades[-1]["exit_time"]) + 600_000
        all_fills = client.exchange.info.user_fills_by_time(client.address, since_ms, until_ms)
        if not isinstance(all_fills, list):
            all_fills = []
    except Exception as e:
        print(f"Failed to fetch fills: {e}")
        client.disconnect()
        sys.exit(1)

    print(f"Fetched {len(all_fills)} total fills from API.\n")

    updated = 0
    skipped = 0

    for trade in trades:
        asset = trade["asset"]
        trade_id = trade["id"]
        side = trade["side"]
        entry_px = trade["entry_price"]
        exit_px = trade["exit_price"]
        size = trade["size"]
        old_fees = float(trade.get("fees") or 0.0)
        old_pnl = float(trade.get("pnl") or 0.0)

        try:
            entry_ms = parse_iso_ms(trade["entry_time"])
            exit_ms = parse_iso_ms(trade["exit_time"])
        except (ValueError, TypeError) as e:
            print(f"  Trade #{trade_id} [{asset}]: bad timestamp — {e}, skipping")
            skipped += 1
            continue

        open_dir = "Open Long" if side == "long" else "Open Short"
        close_dir = "Close Long" if side == "long" else "Close Short"
        stored_oid = str(trade.get("order_id") or "")

        # --- Open fills: prefer oid matching, fall back to time-based ---
        # Aggregate all partial fills for the same order
        open_fills = []
        if stored_oid:
            open_fills = [
                f for f in all_fills
                if f.get("coin") == asset
                and str(f.get("oid", "")) == stored_oid
                and f.get("dir") == open_dir
            ]

        if not open_fills:
            open_candidates = [
                f for f in all_fills
                if f.get("coin") == asset
                and f.get("dir") == open_dir
                and abs(int(f.get("time", 0)) - entry_ms) <= 120_000
            ]
            if open_candidates:
                open_fills = [closest(open_candidates, entry_ms)]

        # --- Close fills: aggregate all partial fills within time window ---
        close_fills = [
            f for f in all_fills
            if f.get("coin") == asset
            and f.get("dir") == close_dir
            and abs(int(f.get("time", 0)) - exit_ms) <= 900_000
        ]

        open_fee = sum(float(f.get("fee", 0.0)) for f in open_fills)
        close_fee = sum(float(f.get("fee", 0.0)) for f in close_fills)
        total_fees = open_fee + close_fee

        if total_fees == 0.0:
            print(f"  Trade #{trade_id} [{asset}] {side.upper()} {trade['entry_time'][:16]}: "
                  f"no fees found (open={len(open_fills)}, "
                  f"close={len(close_fills)}), skipping")
            skipped += 1
            continue

        # closedPnl da HL = lucro bruto (sem fees). PnL real = closedPnl - fees
        funding = float(trade.get("funding") or 0.0)
        closed_pnl = sum(float(f.get("closedPnl", 0.0)) for f in close_fills) if close_fills else (
            (exit_px - entry_px) * size if side == "long" else (entry_px - exit_px) * size
        )
        pnl = closed_pnl - total_fees + funding
        pnl_pct = (pnl / (entry_px * size)) * 100 if entry_px * size > 0 else 0

        # Recalculate exit_price as weighted average of close fills
        new_exit_px = exit_px
        if close_fills:
            total_sz = sum(float(f.get("sz", 0.0)) for f in close_fills)
            if total_sz > 0:
                new_exit_px = sum(float(f["px"]) * float(f.get("sz", 0.0)) for f in close_fills) / total_sz

        # Skip if nothing changed
        if (round(total_fees, 6) == round(old_fees, 6)
                and round(pnl, 4) == round(old_pnl, 4)
                and round(new_exit_px, 4) == round(exit_px, 4)):
            skipped += 1
            continue

        conn.execute(
            "UPDATE trades SET fees = ?, pnl = ?, pnl_pct = ?, exit_price = ? WHERE id = ?",
            (round(total_fees, 6), round(pnl, 4), round(pnl_pct, 2), round(new_exit_px, 4), trade_id),
        )
        conn.commit()

        print(f"  Trade #{trade_id} [{asset}] {side.upper()} {trade['entry_time'][:16]}: "
              f"fees ${old_fees:.4f} -> ${total_fees:.4f}  "
              f"pnl ${old_pnl:.4f} -> ${pnl:.4f} ({pnl_pct:.2f}%)")
        updated += 1

    print(f"\nDone. Updated: {updated}  |  Skipped: {skipped}")
    client.disconnect()


if __name__ == "__main__":
    fix_fees()
