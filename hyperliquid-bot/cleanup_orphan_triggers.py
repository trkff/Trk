"""
Script utilitário one-shot: varre todos os mercados perp da Lighter, identifica
trigger orders (TP/SL) órfãs (sem posição correspondente) e cancela.

Acúmulo de órfãs causa `canceled-reduce-only` em novos market_open. Esse problema
surge porque a Lighter não tem OCO nativo — quando TP dispara, SL fica e vice-versa.

Uso:
    python cleanup_orphan_triggers.py             # dry-run (só lista)
    python cleanup_orphan_triggers.py --apply     # cancela de fato

Requer que o bot esteja configurado (credenciais Lighter no bot_data.db).
"""

import argparse
import sys

from bot.exchanges.factory import create_exchange_client
from bot import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Cancela de fato (default: dry-run)")
    ap.add_argument("--asset", help="Limita a um asset específico (ex: LIT, BRENTOIL)")
    args = ap.parse_args()

    cfg = db.get_all_config()
    if cfg.get("selected_exchange", "").lower() != "lighter":
        print("ERRO: este script é para Lighter (selected_exchange != 'lighter')")
        sys.exit(1)

    client = create_exchange_client()
    client.connect()
    # Force init
    client._ensure_init()

    # Carrega lista de mercados perp da Lighter (já cacheada após connect)
    markets = list(client._client._market_cache.values())
    positions = client.get_open_positions()
    pos_by_coin = {p["coin"]: p for p in positions}

    if args.asset:
        markets = [m for m in markets if m["symbol"].upper() == args.asset.upper()]
        if not markets:
            print(f"Asset {args.asset} não encontrado nos mercados Lighter")
            sys.exit(1)

    total_orphans = 0
    total_canceled = 0
    print(f"{'Asset':<12} {'Pos?':<6} {'Type':<20} {'OrderIndex':<14} {'TriggerPx':<14} {'Action'}")
    print("-" * 90)

    for m in sorted(markets, key=lambda x: x["symbol"]):
        asset = m["symbol"]
        has_position = asset in pos_by_coin and pos_by_coin[asset]["size"] > 0

        try:
            triggers = client.list_active_trigger_orders(asset)
        except Exception as e:
            print(f"{asset:<12} ERR    fetch failed: {e}")
            continue

        if not triggers:
            continue

        for t in triggers:
            is_orphan = not has_position
            oid = t.get("order_index")
            action = "—"
            if is_orphan:
                total_orphans += 1
                if args.apply and oid is not None:
                    ok = client.cancel_order(asset, oid)
                    action = "CANCELED" if ok else "FAILED"
                    if ok:
                        total_canceled += 1
                else:
                    action = "(dry-run) would cancel"
            print(
                f"{asset:<12} {'YES' if has_position else 'no':<6} "
                f"{t['type']:<20} {str(oid):<14} {str(t.get('trigger_price')):<14} {action}"
            )

    print("-" * 90)
    print(f"Total órfãs detectadas: {total_orphans}")
    if args.apply:
        print(f"Total canceladas: {total_canceled}")
    else:
        print("Re-rode com --apply para cancelar.")


if __name__ == "__main__":
    main()
