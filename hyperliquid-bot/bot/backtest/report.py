"""
Backtest metrics computation.
"""
from datetime import datetime


def _trades_per_day(trades: list[dict]) -> float:
    """Average trades per day across the simulated period.

    Spans from first to last entry. Returns 0 for empty/single-day cases
    rather than producing misleading single-trade extrapolations.
    """
    if len(trades) < 2:
        return float(len(trades))
    try:
        first = datetime.fromisoformat(trades[0]["entry_time"].replace("Z", "+00:00"))
        last  = datetime.fromisoformat(trades[-1]["entry_time"].replace("Z", "+00:00"))
    except Exception:
        return 0.0
    span_days = (last - first).total_seconds() / 86400.0
    if span_days <= 0:
        return float(len(trades))
    return len(trades) / span_days


def compute_metrics(trades: list[dict], initial_capital: float = 1000.0) -> dict:
    """
    Compute performance metrics from a list of simulated trades.

    Each trade dict is expected to have at minimum:
        side, entry_price, exit_price, pnl, pnl_pct, entry_time, exit_time, outcome
    """
    if not trades:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "timeouts": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
            "roi": 0.0,
            "avg_pnl": 0.0,
            "best_trade": 0.0,
            "worst_trade": 0.0,
            "max_drawdown": 0.0,
            "profit_factor": 0.0,
            "cumulative_pnl": [],
        }

    wins = sum(1 for t in trades if t.get("outcome") == "tp" or (t.get("outcome") == "bb_mid" and t.get("pnl", 0) > 0))
    losses = sum(1 for t in trades if t.get("outcome") == "sl" or (t.get("outcome") == "bb_mid" and t.get("pnl", 0) <= 0))
    timeouts = sum(1 for t in trades if t.get("outcome") == "timeout")
    total = len(trades)

    win_rate = (wins / total * 100) if total > 0 else 0.0

    pnls = [t.get("pnl", 0.0) for t in trades]
    total_pnl = sum(pnls)
    total_pnl_pct = total_pnl / initial_capital * 100
    roi = total_pnl_pct
    avg_pnl = total_pnl / total if total > 0 else 0.0
    best_trade = max(pnls) if pnls else 0.0
    worst_trade = min(pnls) if pnls else 0.0

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = sum(p for p in pnls if p < 0)
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss != 0 else 0.0

    # Max drawdown — peak-to-trough in USD
    equity = initial_capital
    peak = equity
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # Cumulative PnL series (sorted chronologically)
    sorted_trades = sorted(trades, key=lambda t: t.get("entry_time", ""))
    cum = 0.0
    cumulative_pnl = []
    for t in sorted_trades:
        cum += t.get("pnl", 0.0)
        cumulative_pnl.append({
            "entry_time": t.get("entry_time"),
            "cumulative_pnl": round(cum, 4),
        })

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 4),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "roi": round(roi, 2),
        "avg_pnl": round(avg_pnl, 4),
        "best_trade": round(best_trade, 4),
        "worst_trade": round(worst_trade, 4),
        "max_drawdown": round(max_dd, 4),
        "profit_factor": round(profit_factor, 4),
        "trades_per_day": round(_trades_per_day(sorted_trades), 2),
        "cumulative_pnl": cumulative_pnl,
    }
