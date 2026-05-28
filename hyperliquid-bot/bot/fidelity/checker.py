"""Strategy fidelity checker.

Reruns the canonical backtest (engine._run_backtest with return_signals=True)
over the same period the live bot operated and compares 3 layers:
signals, trades, metrics. Persists diffs into fidelity_runs + fidelity_diffs.
"""
from __future__ import annotations

import json
import math


# Tolerance defaults (overridable via config table keys
# `fidelity.price_tol_pct` and `fidelity.indicator_tol_pct`)
PRICE_TOL = 0.0005   # 0.05% relative
IND_TOL = 0.01       # 1% relative


def _parse_live_indicators(live_sig: dict) -> dict | None:
    raw = live_sig.get("indicators_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _rel(a: float, b: float) -> float:
    """Relative difference |a-b| / max(|b|, eps)."""
    return abs(a - b) / max(abs(b), 1e-9)


def diff_signals(live_signals: list[dict], bt_signals: list[dict],
                 price_tol: float = PRICE_TOL,
                 ind_tol: float = IND_TOL) -> dict:
    """Compare live signals vs backtest signals candle-by-candle.

    Both lists must use ts_ms as the unique key for a candle close. Returns:
        {
            "matched": int, "phantom": int, "missed": int,
            "side_mismatch": int, "price_drift": int, "indicator_drift": int,
            "diffs": [{layer, diff_type, ts_ms, side, live_json, bt_json,
                       delta_pct, notes}, ...]
        }
    """
    live_by_ts: dict[int, dict] = {int(s["ts_ms"]): s for s in live_signals}
    bt_by_ts: dict[int, dict] = {int(s["ts_ms"]): s for s in bt_signals}

    out: dict = {
        "matched": 0, "phantom": 0, "missed": 0, "side_mismatch": 0,
        "price_drift": 0, "indicator_drift": 0,
        "diffs": [],
    }

    for ts in sorted(set(live_by_ts) | set(bt_by_ts)):
        l = live_by_ts.get(ts)
        b = bt_by_ts.get(ts)

        if l and not b:
            out["phantom"] += 1
            out["diffs"].append({
                "layer": "signal", "diff_type": "phantom",
                "ts_ms": ts, "side": l.get("side"),
                "live_json": json.dumps(l, default=str),
                "bt_json": None,
                "delta_pct": None, "notes": None,
            })
            continue
        if b and not l:
            out["missed"] += 1
            out["diffs"].append({
                "layer": "signal", "diff_type": "missed",
                "ts_ms": ts, "side": b.get("side"),
                "live_json": None,
                "bt_json": json.dumps(b, default=str),
                "delta_pct": None, "notes": None,
            })
            continue

        if l["side"] != b["side"]:
            out["side_mismatch"] += 1
            out["diffs"].append({
                "layer": "signal", "diff_type": "side",
                "ts_ms": ts, "side": f'{l["side"]}/{b["side"]}',
                "live_json": json.dumps(l, default=str),
                "bt_json": json.dumps(b, default=str),
                "delta_pct": None, "notes": None,
            })
            continue

        # Same side — check drifts
        had_drift = False

        lp = float(l.get("signal_price") or 0)
        bp = float(b.get("signal_price") or 0)
        if bp > 0:
            pd_rel = _rel(lp, bp)
            if pd_rel > price_tol:
                out["price_drift"] += 1
                had_drift = True
                out["diffs"].append({
                    "layer": "signal", "diff_type": "price",
                    "ts_ms": ts, "side": l["side"],
                    "live_json": json.dumps(l, default=str),
                    "bt_json": json.dumps(b, default=str),
                    "delta_pct": pd_rel, "notes": None,
                })

        live_inds = _parse_live_indicators(l)
        bt_inds = b.get("indicators") or {}
        if live_inds and bt_inds:
            for k, lv in live_inds.items():
                if k not in bt_inds:
                    continue
                try:
                    lvf = float(lv); bvf = float(bt_inds[k])
                except (TypeError, ValueError):
                    continue
                if math.isnan(lvf) or math.isnan(bvf):
                    continue
                rd = _rel(lvf, bvf)
                if rd > ind_tol:
                    out["indicator_drift"] += 1
                    had_drift = True
                    out["diffs"].append({
                        "layer": "signal", "diff_type": "indicator",
                        "ts_ms": ts, "side": l["side"],
                        "live_json": json.dumps({k: lvf}),
                        "bt_json": json.dumps({k: bvf}),
                        "delta_pct": rd,
                        "notes": f"indicator={k}",
                    })

        if not had_drift:
            out["matched"] += 1

    return out


def diff_trades(live_trades: list[dict], bt_trades: list[dict],
                tf_ms: int, price_tol: float = PRICE_TOL) -> dict:
    """Match live trades to backtest trades by entry_ts proximity (±1 candle).

    Each live trade dict must include: entry_ts_ms, side, entry_price,
        exit_price, exit_type (may be None), pnl.
    Each bt trade dict must include: entry_ts_ms, side, entry_price,
        exit_price, exit_type, duration_candles.

    Returns counters + list of diff dicts.
    """
    out: dict = {
        "matched": 0, "extra_live": 0, "missed_trade": 0,
        "entry_px_drift": 0, "exit_px_drift": 0,
        "exit_type_mismatch": 0, "duration_drift": 0,
        "diffs": [],
    }

    used_bt: set[int] = set()
    bt_sorted = sorted(enumerate(bt_trades), key=lambda x: x[1]["entry_ts_ms"])

    for lt in live_trades:
        target = lt["entry_ts_ms"]
        best_idx = None
        best_dt = None
        for idx, b in bt_sorted:
            if idx in used_bt:
                continue
            if b["side"] != lt["side"]:
                continue
            dt = abs(int(b["entry_ts_ms"]) - int(target))
            if dt <= tf_ms and (best_dt is None or dt < best_dt):
                best_idx, best_dt = idx, dt
        if best_idx is None:
            out["extra_live"] += 1
            out["diffs"].append({
                "layer": "trade", "diff_type": "extra_live",
                "ts_ms": target, "side": lt["side"],
                "live_json": json.dumps(lt, default=str),
                "bt_json": None, "delta_pct": None, "notes": None,
            })
            continue

        used_bt.add(best_idx)
        b = bt_trades[best_idx]
        any_drift = False

        if b["entry_price"] > 0:
            d = _rel(lt["entry_price"], b["entry_price"])
            if d > price_tol:
                out["entry_px_drift"] += 1
                any_drift = True
                out["diffs"].append({
                    "layer": "trade", "diff_type": "entry_px",
                    "ts_ms": target, "side": lt["side"],
                    "live_json": json.dumps(lt, default=str),
                    "bt_json": json.dumps(b, default=str),
                    "delta_pct": d, "notes": None,
                })

        if b.get("exit_price") and lt.get("exit_price") and float(b["exit_price"]) > 0:
            d = _rel(float(lt["exit_price"]), float(b["exit_price"]))
            if d > price_tol:
                out["exit_px_drift"] += 1
                any_drift = True
                out["diffs"].append({
                    "layer": "trade", "diff_type": "exit_px",
                    "ts_ms": target, "side": lt["side"],
                    "live_json": json.dumps(lt, default=str),
                    "bt_json": json.dumps(b, default=str),
                    "delta_pct": d, "notes": None,
                })

        if lt.get("exit_type") and b.get("exit_type") and lt["exit_type"] != b["exit_type"]:
            out["exit_type_mismatch"] += 1
            any_drift = True
            out["diffs"].append({
                "layer": "trade", "diff_type": "exit_type",
                "ts_ms": target, "side": lt["side"],
                "live_json": json.dumps(lt, default=str),
                "bt_json": json.dumps(b, default=str),
                "delta_pct": None,
                "notes": f"live={lt['exit_type']} bt={b['exit_type']}",
            })

        if not any_drift:
            out["matched"] += 1

    for idx, b in enumerate(bt_trades):
        if idx in used_bt:
            continue
        out["missed_trade"] += 1
        out["diffs"].append({
            "layer": "trade", "diff_type": "missed_trade",
            "ts_ms": b["entry_ts_ms"], "side": b["side"],
            "live_json": None,
            "bt_json": json.dumps(b, default=str),
            "delta_pct": None, "notes": None,
        })

    return out


def fidelity_score(*, signal_counts: dict, trade_outcome_match_rate: float) -> float:
    """Composite 0..1 score combining match rate, price drift, indicator drift,
    and trade outcome match (see spec section 5)."""
    total_signals = max(signal_counts["live_signals"], signal_counts["bt_signals"], 1)
    matched = signal_counts["matched"]
    matched_div = max(matched, 1)

    match_score = matched / total_signals
    price_score = 1 - (signal_counts["price_drift"] / matched_div)
    ind_score = 1 - (signal_counts["indicator_drift"] / matched_div)
    trade_score = max(0.0, min(1.0, trade_outcome_match_rate))

    return round(
        0.50 * max(0.0, match_score)
        + 0.20 * max(0.0, price_score)
        + 0.15 * max(0.0, ind_score)
        + 0.15 * trade_score,
        4,
    )


def attribute_cause(diff: dict, siblings: list[dict],
                    live_signal: dict | None = None) -> str:
    """Return a short Portuguese sentence with the probable cause of the diff.

    Heuristics per spec section 6.4.
    """
    t = diff["diff_type"]
    ts = diff.get("ts_ms")

    if t == "price":
        return "Vela aberta vazando para o close (verificar _drop_open_candle)."

    if t == "phantom":
        near = [s for s in siblings if s.get("ts_ms") == ts and s["diff_type"] == "indicator"]
        if near:
            keys = ", ".join(sorted({(s.get("notes") or "").split("=")[-1] for s in near}))
            return f"Indicador divergente no mesmo candle ({keys})."
        return "Live disparou antes do close real (timing)."

    if t == "missed":
        reason = (live_signal or {}).get("reason")
        if reason:
            return f"Filtro de risco bloqueou no live: {reason}."
        return "Candle não chegou no live (WS gap ou REST atrasado)."

    if t == "indicator":
        ind = (diff.get("notes") or "indicator=?").split("=")[-1]
        return f"Indicador {ind} fora da tolerância — possível warmup ou fórmula diferente."

    if t == "exit_type":
        return ("Prioridade per-candle do engine (SL>TP>bb_mid) divergiu da ordem real "
                "de trigger na exchange.")

    if t == "side":
        return "Estratégia disparou direção oposta — verificar params no DB vs. usados ao vivo."

    if t == "missed_trade":
        return ("Backtest abriu trade que o live não abriu — provável bloqueio por filtro "
                "de risco ou max_positions.")

    if t == "extra_live":
        return "Live abriu trade que o backtest não abriu — possível sinal espúrio."

    return "Causa não classificada."


def diff_metrics(live_metrics: dict, bt_metrics: dict,
                 abs_tol: float = 0.05) -> dict:
    """Compare aggregate metrics; flag fields differing by > abs_tol (absolute,
    on the metric's natural scale). Returns {"diffs": [...]} only — caller
    decides whether to persist."""
    out: dict = {"diffs": []}
    for k in ("win_rate", "profit_factor", "roi", "total_pnl", "max_drawdown",
              "trades_per_day"):
        if k not in live_metrics or k not in bt_metrics:
            continue
        try:
            lv = float(live_metrics[k] or 0)
            bv = float(bt_metrics[k] or 0)
        except (TypeError, ValueError):
            continue
        if abs(lv - bv) > abs_tol:
            out["diffs"].append({
                "layer": "metric", "diff_type": k,
                "ts_ms": None, "side": None,
                "live_json": json.dumps({k: lv}),
                "bt_json": json.dumps({k: bv}),
                "delta_pct": abs(lv - bv),
                "notes": None,
            })
    return out


# ── Orchestrator ──────────────────────────────────────────────────────────

_TF_TO_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


def _load_live_signals(strategy: str, asset: str, profile_id: int,
                       start_ms: int, end_ms: int) -> list[dict]:
    """Read live signals for the period and normalize to {ts_ms, side,
    signal_price, indicators_json, reason, executed}."""
    from datetime import datetime
    from bot import db as bot_db

    rows = bot_db.get_conn().execute(
        """
        SELECT id, timestamp, side, executed, reason, strategy_name,
               indicators_json
        FROM signals
        WHERE strategy_name = ? AND asset = ? AND profile_id = ?
        ORDER BY timestamp ASC
        """,
        (strategy, asset, profile_id),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        try:
            ts_ms = int(datetime.fromisoformat(r["timestamp"]).timestamp() * 1000)
        except Exception:
            continue
        if ts_ms < start_ms or ts_ms > end_ms:
            continue
        signal_price = None
        if r["indicators_json"]:
            try:
                ind = json.loads(r["indicators_json"])
                signal_price = ind.get("close")
            except (json.JSONDecodeError, TypeError):
                pass
        out.append({
            "ts_ms": ts_ms,
            "side": r["side"],
            "signal_price": signal_price,
            "indicators_json": r["indicators_json"],
            "reason": r["reason"],
            "executed": r["executed"],
        })
    return out


def _load_live_trades(strategy: str, asset: str, profile_id: int,
                      start_ms: int, end_ms: int) -> list[dict]:
    from datetime import datetime
    from bot import db as bot_db

    rows = bot_db.get_conn().execute(
        """
        SELECT id, entry_time, exit_time, side, entry_price, exit_price,
               pnl, signal_price, status
        FROM trades
        WHERE strategy = ? AND asset = ? AND profile_id = ? AND status = 'closed'
        ORDER BY entry_time ASC
        """,
        (strategy, asset, profile_id),
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        try:
            entry_ts_ms = int(datetime.fromisoformat(r["entry_time"]).timestamp() * 1000)
        except Exception:
            continue
        if entry_ts_ms < start_ms or entry_ts_ms > end_ms:
            continue
        out.append({
            "entry_ts_ms": entry_ts_ms,
            "side": r["side"],
            "entry_price": float(r["entry_price"]),
            "exit_price": float(r["exit_price"] or 0),
            "pnl": float(r["pnl"] or 0),
            "exit_type": None,    # Not currently persisted on live trades
            "signal_price": float(r["signal_price"] or 0),
        })
    return out


def _normalize_bt_trade(t: dict) -> dict:
    """Convert engine trade dict (entry_time ISO) to entry_ts_ms-keyed dict."""
    from datetime import datetime
    return {
        "entry_ts_ms": int(datetime.fromisoformat(t["entry_time"]).timestamp() * 1000),
        "side": t["side"],
        "entry_price": float(t["entry_price"]),
        "exit_price": float(t.get("exit_price") or 0),
        "exit_type": t.get("outcome"),    # "tp" / "sl" / "bb_mid"
        "duration_candles": int(t.get("candles_held", 0)),
    }


def run_check(strategy: str, asset: str, days: int,
              profile_id: int = 1, trade_size_usd: float = 1000.0,
              fee_rate: float = 0.0) -> int:
    """Run a full 3-layer fidelity check and persist results.

    Returns the run_id of the persisted row.
    """
    import time
    from datetime import datetime, timezone
    from bot.backtest import engine as bt_engine
    from bot.backtest.report import compute_metrics
    from bot import db as bot_db
    from bot.strategies.manager import STRATEGY_MAP

    resolved = bt_engine._resolve_strategy_instance(strategy, asset)
    strat_obj = STRATEGY_MAP[resolved]
    params_db = bot_db.get_strategy_config(resolved, profile_id=profile_id)["params"]
    params_full = {**strat_obj.DEFAULT_PARAMS, **params_db}
    tf = str(params_full.get("timeframe", "5m"))
    tf_ms = _TF_TO_MS.get(tf, 300_000)

    now_ms = int(time.time() * 1000)
    period_end_ms = now_ms - tf_ms                      # clamp to last closed candle
    period_start_ms = period_end_ms - days * 86_400_000

    # 1. Backtest with signals
    bt_result = bt_engine._run_backtest(
        resolved, asset, days,
        trade_size_usd=trade_size_usd, fee_rate=fee_rate,
        profile_id=profile_id, return_signals=True,
    )
    bt_signals = bt_result.get("signals", [])
    bt_trades_raw = bt_result.get("trades", [])
    bt_trades = [_normalize_bt_trade(t) for t in bt_trades_raw]
    bt_metrics = bt_result.get("metrics", {})

    # 2. Live snapshot
    live_signals = _load_live_signals(resolved, asset, profile_id,
                                      period_start_ms, period_end_ms)
    live_trades_raw = _load_live_trades(resolved, asset, profile_id,
                                        period_start_ms, period_end_ms)
    live_metrics = compute_metrics(live_trades_raw, initial_capital=trade_size_usd)

    # 3. Tolerances from config (with defaults)
    try:
        price_tol = float(bot_db.get_config("fidelity.price_tol_pct") or PRICE_TOL)
    except (TypeError, ValueError):
        price_tol = PRICE_TOL
    try:
        ind_tol = float(bot_db.get_config("fidelity.indicator_tol_pct") or IND_TOL)
    except (TypeError, ValueError):
        ind_tol = IND_TOL

    sig_diff = diff_signals(live_signals, bt_signals,
                            price_tol=price_tol, ind_tol=ind_tol)
    trade_diff = diff_trades(live_trades_raw, bt_trades, tf_ms=tf_ms,
                             price_tol=price_tol)
    metric_diff = diff_metrics(live_metrics, bt_metrics)

    # 4. Score
    outcomes_total = trade_diff["matched"] + trade_diff["exit_type_mismatch"]
    outcome_rate = (trade_diff["matched"] / outcomes_total) if outcomes_total > 0 else 1.0
    counts = {
        "live_signals": len(live_signals),
        "bt_signals":   len(bt_signals),
        "matched":      sig_diff["matched"],
        "price_drift":  sig_diff["price_drift"],
        "indicator_drift": sig_diff["indicator_drift"],
    }
    score = fidelity_score(signal_counts=counts, trade_outcome_match_rate=outcome_rate)

    # 5. Persist run header
    run_id = bot_db.insert_fidelity_run({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile_id": profile_id,
        "strategy": resolved, "asset": asset.upper(), "timeframe": tf,
        "period_start_ms": period_start_ms, "period_end_ms": period_end_ms,
        "params_json": json.dumps(params_full, default=str),
        "live_signals": len(live_signals),
        "bt_signals":   len(bt_signals),
        "matched":      sig_diff["matched"],
        "phantom":      sig_diff["phantom"],
        "missed":       sig_diff["missed"],
        "side_mismatch": sig_diff["side_mismatch"],
        "price_drift":   sig_diff["price_drift"],
        "indicator_drift": sig_diff["indicator_drift"],
        "fidelity_score": score,
        "live_metrics_json": json.dumps(live_metrics, default=str),
        "bt_metrics_json":   json.dumps(bt_metrics, default=str),
    })

    # 6. Persist diffs with attributed cause
    all_diffs = sig_diff["diffs"] + trade_diff["diffs"] + metric_diff["diffs"]
    live_by_ts = {s["ts_ms"]: s for s in live_signals}
    enriched: list[dict] = []
    for d in all_diffs:
        siblings = [x for x in all_diffs
                    if x is not d and x.get("ts_ms") == d.get("ts_ms")]
        cause = attribute_cause(d, siblings, live_by_ts.get(d.get("ts_ms")))
        enriched.append({**d, "run_id": run_id, "notes": d.get("notes") or cause})
    bot_db.insert_fidelity_diffs_bulk(enriched)

    return run_id
