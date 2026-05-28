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
