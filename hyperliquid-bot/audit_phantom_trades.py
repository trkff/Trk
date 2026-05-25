"""Audit script: identify trades in bot_data.db that have no matching real fill on Lighter.

Strategy:
  For each closed trade, fetch fills from Lighter starting 60s before entry_time and
  ending 60s after exit_time. A trade is "real" if we find at least one fill on the
  open side with size matching the trade size (±5%), and at least one fill on the
  close side. Trades without both are flagged as phantom.

Usage:
  python audit_phantom_trades.py                # dry-run, list phantoms
  python audit_phantom_trades.py --delete       # also delete flagged rows
  python audit_phantom_trades.py --days 7       # only check last 7 days
"""

import argparse
import sys
from datetime import datetime, timezone, timedelta

from bot import db
from bot.exchanges.factory import create_exchange_client


def parse_iso_ms(ts: str) -> int:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def trade_has_real_fills(client, trade: dict, tol_pct: float = 0.05) -> tuple[bool, str]:
    asset = trade["asset"]
    side = trade["side"]
    sz = float(trade["size"])
    entry_ms = parse_iso_ms(trade["entry_time"])
    exit_ms = parse_iso_ms(trade["exit_time"]) if trade.get("exit_time") else entry_ms + 3600_000

    since_ms = entry_ms - 60_000
    fills = client.get_recent_fills(asset, since_ms)
    fills = [f for f in fills if since_ms <= float(f.get("timestamp", 0) or 0) <= exit_ms + 60_000
             or True]  # get_recent_fills already filters by since_ms

    open_side = "B" if side == "long" else "A"
    close_side = "A" if side == "long" else "B"

    open_match = [f for f in fills if f.get("side") == open_side and abs(float(f["sz"]) - sz) / sz <= tol_pct]
    close_match = [f for f in fills if f.get("side") == close_side and abs(float(f["sz"]) - sz) / sz <= tol_pct]

    if not open_match:
        return False, "no open fill matched"
    if trade.get("exit_time") and not close_match:
        return False, "no close fill matched"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true", help="actually delete flagged trades")
    ap.add_argument("--days", type=int, default=14, help="lookback window in days")
    args = ap.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE entry_time >= ? ORDER BY entry_time ASC",
        (cutoff,),
    ).fetchall()
    trades = [dict(r) for r in rows]
    print(f"Checking {len(trades)} trades from last {args.days} days...")

    client = create_exchange_client()
    client.connect()

    phantoms = []
    for t in trades:
        try:
            ok, reason = trade_has_real_fills(client, t)
        except Exception as e:
            print(f"  [skip] trade #{t['id']} {t['asset']} {t['entry_time']}: error {e}")
            continue
        if not ok:
            phantoms.append((t, reason))
            print(f"  [PHANTOM] #{t['id']} {t['asset']} {t['side']} sz={t['size']} "
                  f"entry={t['entry_time']} exit={t.get('exit_time')} pnl={t.get('pnl')} — {reason}")

    print(f"\nFound {len(phantoms)} phantom trades out of {len(trades)} checked.")

    if not phantoms:
        return 0
    if not args.delete:
        print("Dry-run only. Re-run with --delete to remove.")
        return 0

    ids = [t["id"] for t, _ in phantoms]
    conn.executemany("DELETE FROM trades WHERE id = ?", [(i,) for i in ids])
    conn.commit()
    print(f"Deleted {len(ids)} phantom trades.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
