"""
slippage_calc.py — Calcula slippage real comparando entry_price do DB
com o close do candle de sinal na Lighter.

Lógica:
  - Backtest entra sempre no CLOSE do candle de 5m em que o sinal disparou.
  - O bot entra via market order logo após o fechamento do candle.
  - Slippage = |entry_price_real - candle_close| / candle_close

Uso:
  python slippage_calc.py
  python slippage_calc.py --asset HYPE --days 30
  python slippage_calc.py --strategy bb_stoch_btc
"""

import sqlite3
import requests
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

DB_PATH = Path(__file__).parent / "bot_data.db"
LIGHTER_BASE = "https://mainnet.zklighter.elliot.ai"

# Mapeamento asset → market_id na Lighter
MARKET_IDS = {
    "ETH":   0,  "ETH-USD":   0,
    "BTC":   1,  "BTC-USD":   1,
    "SOL":   2,  "SOL-USD":   2,
    "TON":  12,  "TON-USD":  12,
    "HYPE": 24,  "HYPE-USD": 24,
    "ZEC":  90,  "ZEC-USD":  90,
    "XAU":  92,  "XAU-USD":  92,
    "LIT": 120,  "LIT-USD": 120,
    "WTI": 145,  "WTI-USD": 145,
}


def floor_to_5m(dt: datetime) -> datetime:
    """Arredonda datetime para baixo ao candle de 5m mais próximo."""
    return dt.replace(second=0, microsecond=0,
                      minute=(dt.minute // 5) * 5)


def ts_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_candle_close(market_id: int, candle_ts: datetime) -> float | None:
    """
    Busca o candle de 5m que FECHA em candle_ts.
    Retorna o close, ou None se não encontrado.
    """
    end_ms = ts_to_ms(candle_ts) + 5 * 60 * 1000  # end = ts + 5m
    url = (f"{LIGHTER_BASE}/api/v1/candles"
           f"?market_id={market_id}&resolution=5m"
           f"&count_back=3&start_timestamp=0&end_timestamp={end_ms}")
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                time.sleep(1)
                continue
            candles = r.json().get("c", [])
            if not candles:
                return None
            # procura o candle cujo timestamp (abertura) == candle_ts
            target_ms = ts_to_ms(candle_ts)
            for c in candles:
                t = int(c.get("t", 0))
                if abs(t - target_ms) < 30_000:   # tolerância de 30s
                    return float(c.get("c", 0)) or None
        except Exception as e:
            print(f"  [warn] fetch_candle_close erro: {e}")
        time.sleep(0.5)
    return None


def load_trades(db_path: Path, asset: str | None, strategy: str | None,
                days: int) -> list[dict]:
    """Lê trades fechados do DB com filtros opcionais."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    where = ["status = 'closed'", "entry_time >= ?"]
    params: list = [since]
    if asset:
        where.append("(asset = ? OR asset = ?)")
        params += [asset.upper(), asset.upper() + "-USD"]
    if strategy:
        where.append("strategy = ?")
        params.append(strategy)

    sql = f"SELECT * FROM trades WHERE {' AND '.join(where)} ORDER BY entry_time"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def calc_slippage(trades: list[dict]) -> dict:
    """
    Para cada trade, busca o candle de 5m e calcula slippage.
    Retorna estatísticas por ativo e estratégia.
    """
    by_asset    = defaultdict(list)
    by_strategy = defaultdict(list)
    all_slips   = []
    no_candle   = 0

    total = len(trades)
    print(f"\nProcessando {total} trades...\n")

    for i, t in enumerate(trades, 1):
        asset    = t["asset"].replace("-USD", "")
        strategy = t.get("strategy") or "unknown"
        entry_price = float(t["entry_price"])
        side     = t["side"]

        # Timestamp de entrada real
        entry_dt = datetime.fromisoformat(t["entry_time"].replace("Z", "+00:00"))
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)

        # Candle de sinal = 5m anterior ao entry (o bot entra no fechamento)
        signal_candle_ts = floor_to_5m(entry_dt - timedelta(seconds=1))

        market_id = MARKET_IDS.get(asset) or MARKET_IDS.get(t["asset"])
        if market_id is None:
            print(f"  [skip] asset desconhecido: {t['asset']}")
            continue

        print(f"  [{i}/{total}] {asset} {side} entry={entry_price:.4f} "
              f"candle={signal_candle_ts.strftime('%H:%M')} ", end="", flush=True)

        candle_close = fetch_candle_close(market_id, signal_candle_ts)
        time.sleep(0.35)  # respeita rate limit da Lighter

        if candle_close is None or candle_close == 0:
            print("→ candle não encontrado")
            no_candle += 1
            continue

        # Slippage direcional:
        # Long: comprou mais caro que o close → slippage positivo (custo)
        # Short: vendeu mais barato que o close → slippage positivo (custo)
        if side == "long":
            slip_pct = (entry_price - candle_close) / candle_close * 100
        else:
            slip_pct = (candle_close - entry_price) / candle_close * 100

        slip_abs = abs(slip_pct)

        print(f"→ close={candle_close:.4f} slip={slip_pct:+.4f}%")

        record = {
            "asset":        asset,
            "strategy":     strategy,
            "side":         side,
            "entry_price":  entry_price,
            "candle_close": candle_close,
            "slip_pct":     slip_pct,
            "slip_abs":     slip_abs,
            "entry_time":   t["entry_time"],
        }
        by_asset[asset].append(record)
        by_strategy[strategy].append(record)
        all_slips.append(record)

    return {
        "all":         all_slips,
        "by_asset":    dict(by_asset),
        "by_strategy": dict(by_strategy),
        "no_candle":   no_candle,
    }


def report(data: dict):
    """Imprime relatório de slippage."""
    all_slips = data["all"]
    if not all_slips:
        print("\nNenhum trade com slippage calculado.")
        return

    def stats(slips: list[dict]) -> dict:
        vals = [s["slip_abs"] for s in slips]
        vals_dir = [s["slip_pct"] for s in slips]
        vals.sort()
        n = len(vals)
        return {
            "n":      n,
            "mean":   sum(vals) / n,
            "median": vals[n // 2],
            "max":    max(vals),
            "dir_mean": sum(vals_dir) / n,  # positivo = custo, negativo = favor
            "pct_over_002": sum(1 for v in vals if v > 0.02) / n * 100,
            "pct_over_003": sum(1 for v in vals if v > 0.03) / n * 100,
        }

    sep = "═" * 70

    print(f"\n{sep}")
    print("  SLIPPAGE REAL — COMPARAÇÃO BACKTEST vs LIVE")
    print(sep)

    overall = stats(all_slips)
    print(f"\n  GERAL ({overall['n']} trades)")
    print(f"    Média (abs):    {overall['mean']:.4f}%")
    print(f"    Mediana (abs):  {overall['median']:.4f}%")
    print(f"    Máximo (abs):   {overall['max']:.4f}%")
    print(f"    Média direcional: {overall['dir_mean']:+.4f}% "
          f"({'custo' if overall['dir_mean'] > 0 else 'favor'})")
    print(f"    > 0.02%/lado: {overall['pct_over_002']:.1f}% dos trades")
    print(f"    > 0.03%/lado: {overall['pct_over_003']:.1f}% dos trades")

    print(f"\n  POR ATIVO")
    print(f"  {'Ativo':<12} {'N':>5} {'Média':>8} {'Mediana':>9} {'Max':>8} {'Direcional':>12}")
    print(f"  {'-'*58}")
    for asset, slips in sorted(data["by_asset"].items()):
        s = stats(slips)
        print(f"  {asset:<12} {s['n']:>5} {s['mean']:>7.4f}% {s['median']:>8.4f}% "
              f"{s['max']:>7.4f}% {s['dir_mean']:>+11.4f}%")

    print(f"\n  POR ESTRATÉGIA")
    print(f"  {'Estratégia':<25} {'N':>5} {'Média':>8} {'Mediana':>9} {'Max':>8}")
    print(f"  {'-'*58}")
    for strat, slips in sorted(data["by_strategy"].items()):
        s = stats(slips)
        print(f"  {strat:<25} {s['n']:>5} {s['mean']:>7.4f}% {s['median']:>8.4f}% "
              f"{s['max']:>7.4f}%")

    print(f"\n  Candles não encontrados: {data['no_candle']}")

    # Interpretação
    overall_mean = overall["mean"]
    print(f"\n  INTERPRETAÇÃO:")
    if overall_mean < 0.015:
        status = "✅ EXCELENTE — slippage abaixo de 0.015%/lado"
    elif overall_mean < 0.025:
        status = "✅ BOM — slippage dentro do esperado (0.01–0.025%)"
    elif overall_mean < 0.035:
        status = "⚠️  ATENÇÃO — slippage acima de 0.025%, estratégias de margem fina (LIT) em risco"
    else:
        status = "❌ CRÍTICO — slippage acima de 0.035%, revisar estratégias"
    print(f"    {status}")
    print(f"\n  Break-even por estratégia com slippage médio de {overall_mean:.4f}%/lado:")
    for strategy_name, min_avg in [
        ("WTI Stoch Scalp",  0.130), ("TON Stoch Scalp", 0.090),
        ("XAU Stoch Scalp",  0.085), ("ZEC BB Stoch",    0.076),
        ("HYPE EMA Cross",   0.071), ("LIT EMA Cross",   0.065),
    ]:
        round_trip_cost = overall_mean * 2
        margin = min_avg - round_trip_cost
        ok = "✅" if margin > 0 else "❌"
        print(f"    {ok} {strategy_name:<20} margem/trade: {margin:+.4f}%")

    print(f"\n{sep}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Calcula slippage real vs backtest")
    parser.add_argument("--asset",    default=None, help="Filtrar por ativo (ex: HYPE)")
    parser.add_argument("--strategy", default=None, help="Filtrar por estratégia (ex: bb_stoch_btc)")
    parser.add_argument("--days",     default=30, type=int, help="Janela de dias (default: 30)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"❌ DB não encontrado: {DB_PATH}")
        exit(1)

    print(f"Lendo trades dos últimos {args.days} dias de {DB_PATH}...")
    trades = load_trades(DB_PATH, args.asset, args.strategy, args.days)
    print(f"Encontrados {len(trades)} trades fechados.")

    if not trades:
        print("Nenhum trade encontrado com os filtros aplicados.")
        exit(0)

    data = calc_slippage(trades)
    report(data)
