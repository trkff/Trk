"""
One-shot de diagnóstico: lista as últimas N ordens inativas (canceladas/filled) por asset
e mostra o `status` real de cada uma. Útil para verificar se órfãs históricas foram
canceladas pela Lighter ou ainda estão "esquecidas" em algum estado intermediário.
"""

import argparse

from bot.exchanges.factory import create_exchange_client
from bot import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", nargs="+", default=["LIT", "BRENTOIL", "VVV", "NEAR"],
                    help="Assets para verificar")
    ap.add_argument("--limit", type=int, default=30, help="Quantas ordens recentes por asset")
    args = ap.parse_args()

    cfg = db.get_all_config()
    if cfg.get("selected_exchange", "").lower() != "lighter":
        print("ERRO: este script é para Lighter")
        return

    client = create_exchange_client()
    client.connect()
    client._ensure_init()
    auth = client._ensure_auth_token()

    status_counts: dict[str, int] = {}
    print(f"{'Asset':<10} {'Type':<22} {'Status':<32} {'Px':<10} {'CreatedAt'}")
    print("-" * 100)

    for asset in args.assets:
        market = client._client.get_market(asset)
        if not market:
            print(f"{asset:<10} (market not found)")
            continue
        try:
            inactive = client._client.get_inactive_orders(
                client._account_index, auth, market_id=market["marketId"], limit=args.limit
            )
        except Exception as e:
            print(f"{asset:<10} ERR: {e}")
            continue

        if not inactive:
            print(f"{asset:<10} (nenhuma ordem inativa nas últimas {args.limit})")
            continue

        for o in inactive:
            t = o.get("type", "?")
            status = o.get("status", "?")
            px = o.get("trigger_price") or o.get("price") or ""
            created = o.get("created_at") or o.get("timestamp") or ""
            status_counts[status] = status_counts.get(status, 0) + 1
            # destaca triggers
            is_trigger = t in ("take-profit", "take-profit-limit", "stop-loss", "stop-loss-limit")
            marker = "* " if is_trigger else "  "
            print(f"{marker}{asset:<8} {t:<22} {status:<32} {str(px):<10} {created}")

    print("-" * 100)
    print("Resumo de status agregado:")
    for s, n in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {n:>4}  {s}")


if __name__ == "__main__":
    main()
