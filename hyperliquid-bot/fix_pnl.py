"""
fix_pnl.py -- Corrige trades fechados com pnl incorreto na Lighter.

Problemas corrigidos:
1. Mismatch de oid (txHash vs tradeId) — closedPnl ficava 0.0
2. Timestamp em ms vs segundos — get_recent_fills nao filtrava por tempo

Lighter tem taxa zero — o closedPnl da API ja e o PnL liquido.

Query alvo: todos os trades Lighter fechados nas ultimas 24h.
Rode com --dry-run para conferir antes de gravar.

Uso:
    cd hyperliquid-bot
    python fix_pnl.py [--dry-run]
"""

import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from bot import db
from bot.exchanges.lighter_exchange import LighterExchangeClient

DRY_RUN = "--dry-run" in sys.argv


def ts_to_ms(iso_str: str) -> int:
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def fetch_close_fills(client: LighterExchangeClient, asset: str,
                      entry_ms: int, exit_ms: int) -> list[dict]:
    """
    Busca fills de fechamento dentro de [entry_ms, exit_ms + 2min].
    Timestamps da Lighter sao em ms. Retorna apenas fills com closedPnl != 0
    (fills de abertura tem closedPnl=0).
    """
    auth = client._ensure_auth_token()
    market = client._client.get_market(asset)
    if not market:
        print(f"  [AVISO] Mercado {asset} nao encontrado na Lighter")
        return []

    buffer_ms = 120_000
    result = []
    cursor = None

    while True:
        trades, cursor = client._client.get_trades_page(
            client._account_index, market["marketId"], auth,
            limit=50, cursor=cursor, aggregate=False,
        )
        stop = False
        for t in trades:
            ts_ms = float(t["timestamp"])
            if ts_ms < entry_ms:
                stop = True
                break
            if ts_ms > exit_ms + buffer_ms:
                continue

            is_buyer = str(t["bidAccountId"]) == str(client._account_index)
            pnl = float(t["bidAccountPnl"] if is_buyer else t["askAccountPnl"])

            if pnl != 0.0:
                result.append({
                    "tradeId": t["tradeId"],
                    "ts_ms": ts_ms,
                    "pnl": pnl,
                    "px": float(t["price"]),
                    "sz": float(t["size"]),
                })

        if stop or cursor is None:
            break
        time.sleep(0.3)

    return result


def main():
    db.init_db()
    cfg = db.get_all_config()

    if cfg.get("selected_exchange", "hyperliquid") != "lighter":
        print("Exchange configurada nao e Lighter. Encerrando.")
        sys.exit(1)

    conn = db.get_conn()
    # Alvo: todos os trades fechados nas ultimas 24h (corrige pnl=0 e re-corrige com fee errada)
    rows = conn.execute("""
        SELECT * FROM trades
        WHERE status = 'closed'
          AND exit_time IS NOT NULL
          AND exit_time >= datetime('now', '-1 day')
        ORDER BY entry_time ASC
    """).fetchall()

    if not rows:
        print("Nenhum trade alvo encontrado.")
        return

    print(f"{'[DRY-RUN] ' if DRY_RUN else ''}Trades alvo: {len(rows)}\n")

    lighter = LighterExchangeClient()
    print("Conectando na Lighter...")
    lighter.connect()
    print(f"Conectado -- account_index={lighter._account_index}\n")

    corrected = 0
    skipped = 0

    for row in rows:
        trade = dict(row)
        trade_id = trade["id"]
        asset = trade["asset"]
        side = trade["side"]
        entry_px = trade["entry_price"]
        size = trade["size"]
        entry_ms = ts_to_ms(trade["entry_time"])
        exit_ms = ts_to_ms(trade["exit_time"])

        print(f"Trade #{trade_id} | {asset} {side.upper()} | "
              f"entry={entry_px} exit={trade.get('exit_price', '?')} | "
              f"{trade['entry_time'][:19]} -> {trade['exit_time'][:19]}")

        if entry_ms == 0 or exit_ms == 0:
            print("  Timestamp invalido, pulando.\n")
            skipped += 1
            continue

        fills = fetch_close_fills(lighter, asset, entry_ms, exit_ms)

        if not fills:
            print("  Nenhum fill de fechamento na janela. Pulando.\n")
            skipped += 1
            continue

        net_pnl = sum(f["pnl"] for f in fills)
        pnl_pct = (net_pnl / (entry_px * size)) * 100 if entry_px * size > 0 else 0

        for f in fills:
            ts_str = datetime.fromtimestamp(f["ts_ms"] / 1000, tz=timezone.utc).strftime("%H:%M:%S")
            print(f"    tradeId={f['tradeId']} ts={ts_str} "
                  f"pnl={f['pnl']:.4f} px={f['px']} sz={f['sz']}")

        print(f"  PnL={net_pnl:.4f} ({pnl_pct:.2f}%)")

        if not DRY_RUN:
            conn.execute(
                "UPDATE trades SET pnl = ?, pnl_pct = ?, fees = ? WHERE id = ?",
                (round(net_pnl, 4), round(pnl_pct, 2), 0.0, trade_id),
            )
            conn.commit()
            print("  [OK] Banco atualizado.")
        else:
            print("  [DRY-RUN] Nao gravado.")

        corrected += 1
        print()

    print("-" * 50)
    print(f"Corrigidos: {corrected} | Pulados: {skipped} | Total: {len(rows)}")
    if DRY_RUN:
        print("Rode sem --dry-run para aplicar as correcoes.")


if __name__ == "__main__":
    main()
