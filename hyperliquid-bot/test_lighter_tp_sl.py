"""
Teste manual: abre uma posição pequena no ETH na Lighter e coloca TP e SL.
Verifica se os 3 passos funcionam:
  1. market_open (entrada)
  2. place_tp_sl (trigger orders)
  3. get_open_positions (confirmação da posição)

Execute de dentro de hyperliquid-bot/:
    python test_lighter_tp_sl.py

A posição é aberta com tamanho mínimo e TP/SL bem próximos do preço atual
para minimizar risco. Feche manualmente no dashboard após o teste.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from bot import db
from bot.exchanges.lighter_exchange import LighterExchangeClient

db.init_db()

ASSET = "ETH"
SIDE  = "short"   # "long" ou "short"
SIZE_USD = 10.0   # tamanho mínimo em USD

def main():
    print(f"\n=== Teste Lighter TP/SL — {ASSET} {SIDE.upper()} ${SIZE_USD} ===\n")

    client = LighterExchangeClient()
    try:
        client.connect()
    except Exception as e:
        print(f"[ERRO] Falha ao conectar: {e}")
        return

    # 1. Preço atual
    mid = client.get_mid_price(ASSET)
    if mid <= 0:
        print(f"[ERRO] Não foi possível obter preço do {ASSET}")
        return
    print(f"Preço atual {ASSET}: {mid:.4f}")

    # 2. Tamanho mínimo
    sz_dec = client.get_asset_sz_decimals(ASSET)
    size = round(SIZE_USD / mid, sz_dec)
    if size <= 0:
        print("[ERRO] Tamanho calculado é zero")
        return
    print(f"Tamanho: {size} {ASSET} ({SIZE_USD} USD)")

    # 3. TP/SL próximos (0.5% de distância) para minimizar risco
    is_buy = SIDE == "long"
    if is_buy:
        tp_price = round(mid * 1.005, 2)
        sl_price = round(mid * 0.995, 2)
    else:
        tp_price = round(mid * 0.995, 2)
        sl_price = round(mid * 1.005, 2)

    print(f"TP: {tp_price:.4f} | SL: {sl_price:.4f}\n")

    # 4. Abrir posição
    print("--- Passo 1: market_open ---")
    try:
        result = client.market_open(ASSET, is_buy, size, slippage=0.005)
        statuses = result.get("statuses", [])
        if statuses and statuses[0].get("filled"):
            filled = statuses[0]["filled"]
            print(f"[OK] Entrada: avg_px={filled['avgPx']} size={filled['totalSz']} tx={str(filled.get('oid',''))[:12]}")
            avg_px = float(filled["avgPx"])
        else:
            print(f"[ERRO] Ordem não preenchida: {result}")
            return
    except Exception as e:
        print(f"[ERRO] market_open falhou: {e}")
        return

    # Recalcula TP/SL pelo preço real de entrada
    if is_buy:
        tp_price = round(avg_px * 1.005, 2)
        sl_price = round(avg_px * 0.995, 2)
    else:
        tp_price = round(avg_px * 0.995, 2)
        sl_price = round(avg_px * 1.005, 2)
    print(f"TP recalculado: {tp_price:.4f} | SL recalculado: {sl_price:.4f}\n")

    # 5. Colocar TP e SL
    print("--- Passo 2: place_tp_sl ---")
    is_buy_to_close = not is_buy
    try:
        client.place_tp_sl(ASSET, is_buy_to_close, size, tp_price, sl_price, sz_dec)
        print("[OK] place_tp_sl executado sem exceção")
    except Exception as e:
        print(f"[ERRO] place_tp_sl falhou: {e}")

    # 6. Confirmar posição aberta
    print("\n--- Passo 3: get_open_positions ---")
    try:
        positions = client.get_open_positions()
        pos = next((p for p in positions if p["coin"] == ASSET), None)
        if pos:
            print(f"[OK] Posição aberta: {pos}")
        else:
            print(f"[AVISO] Posição não encontrada. Posições abertas: {positions}")
    except Exception as e:
        print(f"[ERRO] get_open_positions falhou: {e}")

    print("\n=== Teste concluído. FECHE A POSIÇÃO MANUALMENTE NO DASHBOARD. ===\n")


if __name__ == "__main__":
    main()
