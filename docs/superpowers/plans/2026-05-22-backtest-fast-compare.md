# Backtest Fast + Compare — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a vectorized backtest engine (`engine_fast.py`) and a side-by-side comparison page that runs both engines on the same strategy/params and surfaces divergences.

**Architecture:** New module `bot/backtest/engine_fast.py` mirrors `engine.py`'s public API (`start_backtest_job`/`get_job`) but computes signals as numpy boolean masks over precomputed indicators and uses `numpy.argmax` for TP/SL/BB-mid first-hit lookups. Reuses `_load_candles_csv`, `_update_csv`, and the manager's strategy resolution. New page `/backtest-compare` dispatches both engines in parallel and renders results in two columns plus a trade-diff panel.

**Tech Stack:** Python 3.10+, numpy, pandas, pandas_ta, Flask, Chart.js.

**Repo note:** This project (`C:/Users/User/Documents/Vibe Code/RazorHL`) is not a git repo. Where the plan says "Commit checkpoint", just confirm tests pass and move on — no `git commit`.

**Spec reference:** [2026-05-22-backtest-fast-compare-design.md](../specs/2026-05-22-backtest-fast-compare-design.md)

---

## File Structure

**Created:**

- `hyperliquid-bot/bot/backtest/engine_fast.py` — Vectorized backtest engine. Public API: `start_backtest_job`, `get_job`. Internal: signal functions per family, `_simulate_fast`, `_add_pnl`.
- `hyperliquid-bot/dashboard/templates/backtest_compare.html` — Side-by-side comparison page.
- `hyperliquid-bot/tests/backtest/__init__.py` — Empty init.
- `hyperliquid-bot/tests/backtest/test_engine_fast_smoke.py` — Smoke test per family on synthetic candles.
- `hyperliquid-bot/tests/backtest/test_engine_fast_vs_legacy.py` — Side-by-side fidelity test (bb_stoch_btc, 30 days).

**Modified:**

- `hyperliquid-bot/dashboard/app.py` — Add `/backtest-compare` page route + `/api/backtest/compare/run` + `/api/backtest/compare/status/<legacy_id>/<fast_id>`.
- `hyperliquid-bot/dashboard/templates/base.html` — Add nav link "Backtest ⚡".
- `hyperliquid-bot/CLAUDE.md` — Document `engine_fast.py` and the compare page (final step).

---

## Task 1: Scaffold engine_fast.py with job system

**Files:**
- Create: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 1.1: Write the file skeleton**

Create `hyperliquid-bot/bot/backtest/engine_fast.py`:

```python
"""
Vectorized backtest engine — fast counterpart to engine.py.

Mirrors the public API (start_backtest_job/get_job) and the trade-dict shape
of engine.py so report.compute_metrics and the dashboard table work unchanged.

Speed gains come from:
  - Indicators precomputed once over the full series (not rolling 600-window)
  - Signals as numpy boolean masks (no strategy.evaluate per candle)
  - TP/SL/BB-mid first-hit found via numpy.argmax on boolean slices
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta

from bot.logger import get_logger
from bot.backtest.engine import _load_candles_csv, _update_csv
from bot.backtest.report import compute_metrics

log = get_logger("backtest.fast")

_jobs: dict = {}
_jobs_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────

def _apply_ema_filter(sig_long: np.ndarray, sig_short: np.ndarray,
                     close: np.ndarray, ema: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    if ema is None:
        return sig_long, sig_short
    valid = ~np.isnan(ema)
    return sig_long & valid & (close > ema), sig_short & valid & (close < ema)


def _add_pnl(raw_trades: list[dict], trade_size_usd: float, fee_rate: float) -> list[dict]:
    """Identical to engine._add_pnl — duplicated to keep the modules decoupled."""
    result = []
    for t in raw_trades:
        ep = t["entry_price"]
        xp = t["exit_price"]
        size = trade_size_usd / ep
        gross = (xp - ep) * size if t["side"] == "long" else (ep - xp) * size
        fees = trade_size_usd * fee_rate
        pnl = gross - fees
        result.append({**t, "pnl": round(pnl, 4), "pnl_pct": round(pnl / trade_size_usd * 100, 4)})
    return result


# ── Public API ─────────────────────────────────────────────────────────────

def start_backtest_job(strategy_name: str, asset: str, days: int,
                       trade_size_usd: float, fee_rate: float) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": "Na fila...",
            "strategy": strategy_name,
            "asset": asset,
            "days": days,
            "result": None,
            "error": None,
            "elapsed_s": None,
        }
    t = threading.Thread(
        target=_run_job,
        args=(job_id, strategy_name, asset, days, trade_size_usd, fee_rate),
        daemon=True,
    )
    t.start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _run_job(job_id: str, strategy_name: str, asset: str, days: int,
             trade_size_usd: float, fee_rate: float):
    started = time.time()
    try:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["progress"] = "Iniciando..."

        result = _run_backtest_fast(strategy_name, asset, days, trade_size_usd, fee_rate,
                                    progress_cb=lambda m: _set_progress(job_id, m))
        elapsed = round(time.time() - started, 3)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["progress"] = f"Concluído em {elapsed}s"
            _jobs[job_id]["result"] = result
            _jobs[job_id]["elapsed_s"] = elapsed
    except Exception as e:
        log.error(f"[backtest-fast job {job_id}] {e}", exc_info=True)
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            _jobs[job_id]["progress"] = f"Erro: {e}"
            _jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)


def _set_progress(job_id: str, msg: str):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["progress"] = msg


# Placeholder — implemented in later tasks
def _run_backtest_fast(strategy_name, asset, days, trade_size_usd, fee_rate, progress_cb=None):
    raise NotImplementedError("Implemented in Task 6")
```

- [ ] **Step 1.2: Verify file imports cleanly**

Run from `hyperliquid-bot/`:
```
python -c "from bot.backtest import engine_fast; print(engine_fast.start_backtest_job)"
```
Expected: prints function repr, no errors.

- [ ] **Step 1.3: Commit checkpoint** — module skeleton in place.

---

## Task 2: Trade simulation core `_simulate_fast` (no BB mid)

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`
- Test: `hyperliquid-bot/tests/backtest/test_engine_fast_smoke.py`

- [ ] **Step 2.1: Create test file with simulation unit test**

Create `hyperliquid-bot/tests/backtest/__init__.py` (empty).

Create `hyperliquid-bot/tests/backtest/test_engine_fast_smoke.py`:

```python
import numpy as np
import pytest

from bot.backtest.engine_fast import _simulate_fast


def _make_ts(n: int, start_ms: int = 1_700_000_000_000) -> np.ndarray:
    return start_ms + np.arange(n, dtype=np.int64) * 300_000  # 5m candles


def test_simulate_long_tp_hit():
    # 5 candles. Signal at index 0 (long), TP hit at index 2.
    close = np.array([100.0, 100.5, 102.0, 101.0, 100.0])
    high  = np.array([100.5, 101.0, 102.5, 101.5, 100.5])
    low   = np.array([ 99.5,  99.8, 100.5, 100.0,  99.0])
    ts    = _make_ts(5)
    sig_long  = np.array([True, False, False, False, False])
    sig_short = np.zeros(5, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=2.0, sl_pct=2.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "long"
    assert t["outcome"] == "tp"
    assert t["entry_price"] == 100.0
    assert t["tp"] == pytest.approx(102.0)
    assert t["sl"] == pytest.approx(98.0)
    assert t["exit_price"] == pytest.approx(102.0)
    assert t["candles_held"] == 2


def test_simulate_short_sl_hit():
    close = np.array([100.0, 100.5, 102.5, 102.0, 101.0])
    high  = np.array([100.5, 101.0, 102.8, 102.5, 101.5])
    low   = np.array([ 99.8,  99.9, 102.2, 101.5, 100.5])
    ts    = _make_ts(5)
    sig_long  = np.zeros(5, dtype=bool)
    sig_short = np.array([True, False, False, False, False])

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=5.0, sl_pct=2.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "short"
    assert t["outcome"] == "sl"
    assert t["sl"] == pytest.approx(102.0)
    assert t["candles_held"] == 2


def test_simulate_no_hit_discarded():
    # Trade entered but neither TP nor SL ever hit — must be discarded.
    close = np.array([100.0, 100.1, 100.05, 99.95, 100.02])
    high  = np.array([100.2, 100.3, 100.15, 100.05, 100.10])
    low   = np.array([ 99.8,  99.9, 99.95, 99.85, 99.95])
    ts    = _make_ts(5)
    sig_long  = np.array([True, False, False, False, False])
    sig_short = np.zeros(5, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=5.0, sl_pct=5.0)
    assert trades == []


def test_simulate_no_overlap():
    # Two signals; second is inside first trade's holding window — must be ignored.
    close = np.array([100.0, 100.5, 102.0, 101.0, 103.0, 105.0])
    high  = np.array([100.5, 101.0, 102.5, 101.5, 103.5, 105.5])
    low   = np.array([ 99.5,  99.8, 100.5, 100.0, 102.5, 104.0])
    ts    = _make_ts(6)
    sig_long  = np.array([True, True, False, False, False, False])
    sig_short = np.zeros(6, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=2.0, sl_pct=2.0)
    assert len(trades) == 1  # second signal at i=1 falls inside first trade
```

- [ ] **Step 2.2: Run tests — verify they fail**

Run from `hyperliquid-bot/`:
```
pytest tests/backtest/test_engine_fast_smoke.py -v
```
Expected: 4 failures with `ImportError` (function not defined yet).

- [ ] **Step 2.3: Implement `_simulate_fast`**

Append to `bot/backtest/engine_fast.py`:

```python
# ── Simulation ─────────────────────────────────────────────────────────────

def _first_true(mask: np.ndarray) -> int | None:
    """Index of first True, or None if no True. np.argmax returns 0 either way,
    so we must check .any() explicitly."""
    if not mask.any():
        return None
    return int(np.argmax(mask))


def _simulate_fast(
    sig_long: np.ndarray,
    sig_short: np.ndarray,
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    ts: np.ndarray,
    tp_pct: float,
    sl_pct: float,
    bb_mid: np.ndarray | None = None,
    sl_dist: np.ndarray | None = None,
    strategy_name: str = "",
) -> list[dict]:
    """
    Walk signal-by-signal. For each entry, find first TP, SL, and (optionally)
    BB-mid hit via numpy.argmax. Pick earliest outcome. No overlap between trades.
    Trades where neither TP nor SL ever hits are discarded (matches engine.py).

    bb_mid: float array of BB midline values, or None to skip BB mid exit.
    sl_dist: per-candle absolute SL distance in price (for ATR-based SL),
             or None to use sl_pct.
    """
    trades: list[dict] = []
    N = len(close)
    i = 0
    while i < N - 1:
        is_long = bool(sig_long[i])
        is_short = bool(sig_short[i])
        if not is_long and not is_short:
            i += 1
            continue
        side = "long" if is_long else "short"  # long takes precedence
        entry = float(close[i])

        if sl_dist is not None and not np.isnan(sl_dist[i]):
            sl_abs = float(sl_dist[i])
        else:
            sl_abs = entry * sl_pct / 100.0
        tp_abs = entry * tp_pct / 100.0

        if side == "long":
            tp = entry + tp_abs
            sl = entry - sl_abs
        else:
            tp = entry - tp_abs
            sl = entry + sl_abs

        # Slices starting at i+1
        h = high[i + 1:]
        lo = low[i + 1:]
        c = close[i + 1:]

        if side == "long":
            tp_mask = h >= tp
            sl_mask = lo <= sl
        else:
            tp_mask = lo <= tp
            sl_mask = h >= sl

        jt = _first_true(tp_mask)
        js = _first_true(sl_mask)

        outcome_idx: int | None = None
        outcome_label: str | None = None
        exit_price: float | None = None

        # Handle BB mid exit if enabled
        jm: int | None = None
        if bb_mid is not None:
            mid_slice = bb_mid[i + 1:]
            with np.errstate(invalid="ignore"):
                if side == "long":
                    mid_mask = (~np.isnan(mid_slice)) & (c >= mid_slice)
                else:
                    mid_mask = (~np.isnan(mid_slice)) & (c <= mid_slice)
            jm = _first_true(mid_mask)

        # Pick earliest
        candidates = [(jt, "tp"), (js, "sl")]
        if jm is not None:
            candidates.append((jm, "bb_mid"))
        candidates = [(j, label) for j, label in candidates if j is not None]
        if not candidates:
            i += 1
            continue
        outcome_idx, outcome_label = min(candidates, key=lambda x: x[0])

        # Tie-break inside same candle (TP and SL hit on same bar): use close vs entry
        if outcome_label in ("tp", "sl") and jt is not None and js is not None and jt == js:
            j_abs = i + 1 + jt
            cj = float(close[j_abs])
            if side == "long":
                outcome_label = "tp" if cj >= entry else "sl"
            else:
                outcome_label = "tp" if cj <= entry else "sl"

        j_abs = i + 1 + outcome_idx
        if outcome_label == "tp":
            exit_price = tp
        elif outcome_label == "sl":
            exit_price = sl
        else:  # bb_mid
            exit_price = float(close[j_abs])

        entry_ts = int(ts[i])
        exit_ts = int(ts[j_abs])

        trades.append({
            "entry_time": datetime.fromtimestamp(entry_ts / 1000, tz=timezone.utc).isoformat(),
            "exit_time":  datetime.fromtimestamp(exit_ts / 1000, tz=timezone.utc).isoformat(),
            "side":       side,
            "entry_price": round(entry, 4),
            "exit_price":  round(exit_price, 4),
            "tp":          round(tp, 4),
            "sl":          round(sl, 4),
            "outcome":     outcome_label,
            "candles_held": outcome_idx + 1,
            "strategy":    strategy_name,
        })
        i = j_abs + 1

    return trades
```

- [ ] **Step 2.4: Run tests — verify all pass**

```
pytest tests/backtest/test_engine_fast_smoke.py -v
```
Expected: 4 passed.

- [ ] **Step 2.5: Commit checkpoint** — simulation core works on synthetic data.

---

## Task 3: BB mid exit smoke test

**Files:**
- Modify: `hyperliquid-bot/tests/backtest/test_engine_fast_smoke.py`

- [ ] **Step 3.1: Add BB mid exit test**

Append to `tests/backtest/test_engine_fast_smoke.py`:

```python
def test_simulate_bb_mid_exit_long_wins():
    # Long entry at 100, bb_mid starts at 102. Price climbs to 101 (not TP), then closes
    # above bb_mid (which has fallen to 100.5). BB mid hits before TP.
    close = np.array([100.0, 100.5, 100.8, 101.2, 102.0])
    high  = np.array([100.5, 100.8, 101.0, 101.5, 102.2])
    low   = np.array([ 99.5,  99.9, 100.3, 100.8, 101.5])
    ts    = _make_ts(5)
    bb_mid = np.array([102.0, 101.5, 101.0, 100.5, 100.5])
    sig_long  = np.array([True, False, False, False, False])
    sig_short = np.zeros(5, dtype=bool)

    trades = _simulate_fast(sig_long, sig_short, close, high, low, ts,
                            tp_pct=5.0, sl_pct=5.0, bb_mid=bb_mid)
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "bb_mid"
    # At candle index 3, close=101.2 >= bb_mid=100.5 → bb_mid exit fires
    assert t["candles_held"] == 3
    assert t["exit_price"] == pytest.approx(101.2)
```

- [ ] **Step 3.2: Run test — verify it passes**

```
pytest tests/backtest/test_engine_fast_smoke.py::test_simulate_bb_mid_exit_long_wins -v
```
Expected: PASS.

- [ ] **Step 3.3: Commit checkpoint** — BB mid exit verified.

---

## Task 4: Signal computation — bb_stoch family

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`
- Modify: `hyperliquid-bot/tests/backtest/test_engine_fast_smoke.py`

- [ ] **Step 4.1: Add signal function for bb_stoch**

Append to `engine_fast.py`:

```python
# ── Indicator helpers ──────────────────────────────────────────────────────

def _ema(close_s: pd.Series, period: int) -> np.ndarray | None:
    if period <= 0:
        return None
    return ta.ema(close_s, length=period).values.astype(float)


def _bb_arrays(close_s: pd.Series, close: np.ndarray, period: int, std: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (BBP, BBM) arrays."""
    bb = ta.bbands(close_s, length=period, std=std)
    bbu = bb[[c for c in bb.columns if c.startswith("BBU_")][0]].values.astype(float)
    bbl = bb[[c for c in bb.columns if c.startswith("BBL_")][0]].values.astype(float)
    bbm = bb[[c for c in bb.columns if c.startswith("BBM_")][0]].values.astype(float)
    span = bbu - bbl
    with np.errstate(invalid="ignore", divide="ignore"):
        bbp = np.where(span > 0, (close - bbl) / span, np.nan)
    return bbp, bbm


def _stoch_arrays(high_s, low_s, close_s, k: int, d: int) -> tuple[np.ndarray, np.ndarray]:
    df = ta.stoch(high_s, low_s, close_s, k=k, d=d, smooth_k=3)
    K = df[[c for c in df.columns if c.startswith("STOCHk_")][0]].values.astype(float)
    D = df[[c for c in df.columns if c.startswith("STOCHd_")][0]].values.astype(float)
    return K, D


# ── Signal functions per family ────────────────────────────────────────────

def _signals_bb_stoch(close, high, low, close_s, high_s, low_s, params):
    bb_period   = int(params["bb_period"])
    bb_std      = float(params["bb_std"])
    stoch_k     = int(params["stoch_k"])
    stoch_d     = int(params["stoch_d"])
    stoch_long  = float(params["stoch_long"])    # oversold threshold
    stoch_short = float(params["stoch_short"])   # overbought threshold
    bbp_long_th  = float(params["bbp_long_threshold"])
    bbp_short_th = float(params["bbp_short_threshold"])
    ema_period   = int(params.get("ema_period", 0))
    bb_mid_exit  = bool(params.get("bb_mid_exit", False))

    BBP, BBM = _bb_arrays(close_s, close, bb_period, bb_std)
    K, D = _stoch_arrays(high_s, low_s, close_s, stoch_k, stoch_d)
    ema = _ema(close_s, ema_period)

    valid = ~np.isnan(BBP) & ~np.isnan(K) & ~np.isnan(D)
    sig_long  = valid & (BBP < bbp_long_th)  & (K < stoch_long)  & (D < stoch_long)
    sig_short = valid & (BBP > bbp_short_th) & (K > stoch_short) & (D > stoch_short)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)

    bb_mid_out = BBM if bb_mid_exit else None
    return sig_long, sig_short, bb_mid_out, None
```

- [ ] **Step 4.2: Add unit test for bb_stoch signal**

Append to `tests/backtest/test_engine_fast_smoke.py`:

```python
def test_signals_bb_stoch_basic():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_bb_stoch

    # Build 100 candles with a synthetic oversold dip
    n = 100
    close = np.full(n, 100.0)
    close[50:55] = [98.0, 96.0, 94.0, 92.0, 90.0]   # sharp drop
    high = close + 0.5
    low = close - 0.5
    close_s = pd.Series(close)
    high_s = pd.Series(high)
    low_s = pd.Series(low)

    params = {
        "bb_period": 15, "bb_std": 1.5,
        "stoch_k": 14, "stoch_d": 3,
        "stoch_long": 25, "stoch_short": 75,
        "bbp_long_threshold": 0.1, "bbp_short_threshold": 0.9,
        "ema_period": 0, "bb_mid_exit": False,
    }
    sig_long, sig_short, bb_mid, sl_dist = _signals_bb_stoch(
        close, high, low, close_s, high_s, low_s, params)

    assert sig_long.shape == (n,)
    assert sig_short.shape == (n,)
    assert bb_mid is None
    assert sl_dist is None
    # Drop region should trigger at least one long
    assert sig_long[50:60].any()


def test_signals_bb_stoch_returns_bb_mid_when_enabled():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_bb_stoch
    close = np.linspace(100, 110, 100)
    close_s = pd.Series(close)
    params = {
        "bb_period": 15, "bb_std": 1.5, "stoch_k": 14, "stoch_d": 3,
        "stoch_long": 25, "stoch_short": 75,
        "bbp_long_threshold": 0.1, "bbp_short_threshold": 0.9,
        "ema_period": 0, "bb_mid_exit": True,
    }
    _, _, bb_mid, _ = _signals_bb_stoch(
        close, close + 0.5, close - 0.5, close_s, pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert bb_mid is not None
    assert bb_mid.shape == (100,)
```

- [ ] **Step 4.3: Run tests**

```
pytest tests/backtest/test_engine_fast_smoke.py -v
```
Expected: all pass.

- [ ] **Step 4.4: Commit checkpoint** — first family signal logic done.

---

## Task 5: End-to-end wiring for bb_stoch — `_run_backtest_fast`

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 5.1: Add family dispatch and runner**

Replace the `_run_backtest_fast` stub in `engine_fast.py` with:

```python
# ── Family dispatch ────────────────────────────────────────────────────────

_FAMILY_FNS: dict = {}  # populated below as families are added


def _resolve_family(strategy_name: str) -> str:
    # Longest prefix match (so "bb_reversion" beats "bb_")
    for fam in sorted(_FAMILY_FNS.keys(), key=len, reverse=True):
        if strategy_name.startswith(fam):
            return fam
    raise ValueError(f"No fast family for strategy: {strategy_name}")


def _resolve_strategy_instance(strategy_name: str, asset: str) -> str:
    """Mirror engine._run_backtest's logic for resolving generic → specific name."""
    from bot.strategies.manager import STRATEGY_MAP
    if strategy_name in STRATEGY_MAP:
        return strategy_name
    candidates = [(n, s) for n, s in STRATEGY_MAP.items() if n.startswith(strategy_name + "_")]
    if not candidates:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    asset_matches = [(n, s) for n, s in candidates
                     if asset.upper() in (s.DEFAULT_PARAMS.get("assets") or [])]
    resolved, _ = asset_matches[0] if asset_matches else candidates[0]
    return resolved


def _run_backtest_fast(strategy_name, asset, days, trade_size_usd, fee_rate, progress_cb=None):
    from bot import db as bot_db
    from bot.strategies.manager import STRATEGY_MAP

    strategy_name = _resolve_strategy_instance(strategy_name, asset)
    strategy = STRATEGY_MAP[strategy_name]
    family = _resolve_family(strategy_name)
    fn = _FAMILY_FNS[family]

    params = {**strategy.DEFAULT_PARAMS, **bot_db.get_strategy_config(strategy_name)["params"]}

    if progress_cb: progress_cb("Atualizando CSV...")
    _update_csv(asset, progress_cb)

    if progress_cb: progress_cb("Carregando candles...")
    df = _load_candles_csv(asset, "5m", days=None)
    if df.empty:
        raise ValueError(f"No 5m candles available for {asset}")

    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    ts    = df["timestamp"].values.astype(np.int64)
    close_s = pd.Series(close)
    high_s  = pd.Series(high)
    low_s   = pd.Series(low)

    if progress_cb: progress_cb(f"Computando sinais ({family})...")
    sig_long, sig_short, bb_mid, sl_dist = fn(close, high, low, close_s, high_s, low_s, params)

    tp_pct = float(params["tp_pct"])
    sl_pct = float(params["sl_pct"])

    if progress_cb: progress_cb("Simulando trades...")
    raw_trades = _simulate_fast(
        sig_long, sig_short, close, high, low, ts,
        tp_pct=tp_pct, sl_pct=sl_pct,
        bb_mid=bb_mid, sl_dist=sl_dist,
        strategy_name=strategy_name,
    )

    # Filter by requested period
    now_ms = int(time.time() * 1000)
    cutoff_iso = datetime.fromtimestamp((now_ms - days * 86_400_000) / 1000, tz=timezone.utc).isoformat()
    filtered = [t for t in raw_trades if t["entry_time"] >= cutoff_iso]

    if progress_cb: progress_cb(f"Calculando métricas ({len(filtered)} trades)...")
    trades_with_pnl = _add_pnl(filtered, trade_size_usd, fee_rate)
    metrics = compute_metrics(trades_with_pnl, initial_capital=trade_size_usd)

    return {
        "trades": trades_with_pnl,
        "metrics": metrics,
        "strategy_resolved": strategy_name,
    }


# Register bb_stoch family (other families appended in later tasks)
_FAMILY_FNS["bb_stoch"] = _signals_bb_stoch
```

- [ ] **Step 5.2: Add integration smoke test for bb_stoch_btc**

Append to `tests/backtest/test_engine_fast_smoke.py`:

```python
def test_run_backtest_fast_bb_stoch_btc_smoke():
    """Runs the full fast engine on bb_stoch_btc, 30 days. Asserts shape only."""
    import os
    from pathlib import Path
    csv_path = Path(__file__).parents[2].parent / "candles" / "btc_5m.csv"
    if not csv_path.exists():
        pytest.skip("btc_5m.csv not present in candles/")

    from bot.backtest.engine_fast import _run_backtest_fast
    result = _run_backtest_fast("bb_stoch_btc", "BTC", days=30,
                                trade_size_usd=1000.0, fee_rate=0.0)
    assert "trades" in result
    assert "metrics" in result
    assert isinstance(result["trades"], list)
    assert result["strategy_resolved"] == "bb_stoch_btc"
    # Sanity: every trade has the required keys
    for t in result["trades"]:
        for k in ("entry_time","exit_time","side","entry_price","exit_price",
                  "tp","sl","outcome","candles_held","pnl","pnl_pct"):
            assert k in t, f"Trade missing key: {k}"
```

- [ ] **Step 5.3: Run all backtest tests**

```
pytest tests/backtest/ -v
```
Expected: all pass (the smoke test may skip if CSV is absent — that's fine).

- [ ] **Step 5.4: Commit checkpoint** — fast engine runs end-to-end for bb_stoch.

---

## Task 6: Signal function — bb_reversion family

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 6.1: Add bb_reversion signal function**

Append to `engine_fast.py` (after `_signals_bb_stoch`):

```python
def _signals_bb_reversion(close, high, low, close_s, high_s, low_s, params):
    bb_period  = int(params["bb_period"])
    bb_std     = float(params["bb_std"])
    bbp_long_th  = float(params["bbp_long_threshold"])
    bbp_short_th = float(params["bbp_short_threshold"])
    ema_period   = int(params.get("ema_period", 0))
    rsi_long_max  = float(params.get("rsi_long_max", 100))
    rsi_short_min = float(params.get("rsi_short_min", 0))
    bb_mid_exit   = bool(params.get("bb_mid_exit", True))

    BBP, BBM = _bb_arrays(close_s, close, bb_period, bb_std)
    # BBU/BBL needed for the close-within-band check
    bb = ta.bbands(close_s, length=bb_period, std=bb_std)
    BBU = bb[[c for c in bb.columns if c.startswith("BBU_")][0]].values.astype(float)
    BBL = bb[[c for c in bb.columns if c.startswith("BBL_")][0]].values.astype(float)

    ema = _ema(close_s, ema_period)
    rsi = ta.rsi(close_s, length=14).values.astype(float)

    BBP_prev = np.roll(BBP, 1); BBP_prev[0] = np.nan
    valid = ~np.isnan(BBP_prev) & ~np.isnan(BBM) & ~np.isnan(rsi)

    sig_long  = (valid
                 & (BBP_prev < bbp_long_th)
                 & (close > BBL) & (close < BBM)
                 & (rsi < rsi_long_max))
    sig_short = (valid
                 & (BBP_prev > bbp_short_th)
                 & (close < BBU) & (close > BBM)
                 & (rsi > rsi_short_min))
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)

    return sig_long, sig_short, (BBM if bb_mid_exit else None), None


_FAMILY_FNS["bb_reversion"] = _signals_bb_reversion
```

- [ ] **Step 6.2: Add unit test**

Append to `tests/backtest/test_engine_fast_smoke.py`:

```python
def test_signals_bb_reversion_shape():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_bb_reversion
    n = 100
    close = np.full(n, 100.0)
    close[50:55] = [98.0, 96.0, 94.0, 92.0, 90.0]
    close[55:60] = [92.0, 94.0, 96.0, 98.0, 99.5]  # mean reversion
    close_s = pd.Series(close)
    params = {
        "bb_period": 10, "bb_std": 2.0,
        "bbp_long_threshold": 0.10, "bbp_short_threshold": 0.90,
        "ema_period": 0, "rsi_long_max": 65, "rsi_short_min": 35,
        "bb_mid_exit": True,
    }
    sl, ss, bbm, sd = _signals_bb_reversion(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,) and ss.shape == (n,)
    assert bbm is not None  # bb_mid_exit=True returns BBM array
    assert sd is None
```

- [ ] **Step 6.3: Run tests** — `pytest tests/backtest/ -v`. Expected: all pass.

- [ ] **Step 6.4: Commit checkpoint**.

---

## Task 7: Signal function — stoch_scalp family

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 7.1: Add stoch_scalp signal function**

Append:

```python
def _signals_stoch_scalp(close, high, low, close_s, high_s, low_s, params):
    stoch_k    = int(params["stoch_k"])
    stoch_d    = int(params["stoch_d"])
    stoch_os   = float(params["stoch_os"])
    stoch_ob   = 100.0 - stoch_os
    ema_period = int(params.get("ema_period", 0))

    K, D = _stoch_arrays(high_s, low_s, close_s, stoch_k, stoch_d)
    ema = _ema(close_s, ema_period)

    pK = np.roll(K, 1); pK[0] = np.nan
    pD = np.roll(D, 1); pD[0] = np.nan
    valid = ~np.isnan(K) & ~np.isnan(D) & ~np.isnan(pK) & ~np.isnan(pD)

    sig_long  = valid & (pK < stoch_os) & (pD < stoch_os) & (K > D) & (pK <= pD)
    sig_short = valid & (pK > stoch_ob) & (pD > stoch_ob) & (K < D) & (pK >= pD)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, None, None


_FAMILY_FNS["stoch_scalp"] = _signals_stoch_scalp
```

- [ ] **Step 7.2: Add minimal shape test**

Append:

```python
def test_signals_stoch_scalp_shape():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_stoch_scalp
    n = 200
    rng = np.random.default_rng(0)
    close = 100 + rng.normal(0, 1, n).cumsum() * 0.1
    close_s = pd.Series(close)
    params = {"stoch_k": 9, "stoch_d": 3, "stoch_os": 40, "ema_period": 0}
    sl, ss, bbm, sd = _signals_stoch_scalp(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)
    assert bbm is None and sd is None
```

- [ ] **Step 7.3: Run tests, commit checkpoint.**

---

## Task 8: Signal function — ema_cross family (with ATR-based SL)

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 8.1: Add ema_cross signal function**

Append:

```python
def _signals_ema_cross(close, high, low, close_s, high_s, low_s, params):
    ema_fast   = int(params["ema_fast"])
    ema_slow   = int(params["ema_slow"])
    ema_trend  = int(params.get("ema_trend", 0))
    use_atr_sl = bool(params.get("use_atr_sl", False))
    atr_period = int(params.get("atr_period", 14))
    atr_mult   = float(params.get("atr_mult", 1.0))

    FAST = _ema(close_s, ema_fast)
    SLOW = _ema(close_s, ema_slow)
    TREND = _ema(close_s, ema_trend) if ema_trend > 0 else None

    pFAST = np.roll(FAST, 1); pFAST[0] = np.nan
    pSLOW = np.roll(SLOW, 1); pSLOW[0] = np.nan
    valid = ~np.isnan(FAST) & ~np.isnan(SLOW) & ~np.isnan(pFAST) & ~np.isnan(pSLOW)

    sig_long  = valid & (FAST > SLOW) & (pFAST <= pSLOW)
    sig_short = valid & (FAST < SLOW) & (pFAST >= pSLOW)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, TREND)

    sl_dist = None
    if use_atr_sl:
        atr = ta.atr(high_s, low_s, close_s, length=atr_period).values.astype(float)
        sl_dist = atr * atr_mult

    return sig_long, sig_short, None, sl_dist


_FAMILY_FNS["ema_cross"] = _signals_ema_cross
```

- [ ] **Step 8.2: Test — both modes**

Append to test file:

```python
def test_signals_ema_cross_atr_sl():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_ema_cross
    n = 200
    rng = np.random.default_rng(1)
    close = 100 + rng.normal(0, 1, n).cumsum() * 0.1
    high = close + 0.5
    low = close - 0.5
    close_s = pd.Series(close); high_s = pd.Series(high); low_s = pd.Series(low)
    params = {"ema_fast": 9, "ema_slow": 21, "ema_trend": 0,
              "use_atr_sl": True, "atr_period": 14, "atr_mult": 1.0}
    sl, ss, bbm, sd = _signals_ema_cross(close, high, low, close_s, high_s, low_s, params)
    assert sd is not None and sd.shape == (n,)
```

- [ ] **Step 8.3: Run tests, commit checkpoint.**

---

## Task 9: Signal function — rsi_scalp family

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 9.1: Add rsi_scalp signal function**

Append:

```python
def _signals_rsi_scalp(close, high, low, close_s, high_s, low_s, params):
    rsi_period = int(params["rsi_period"])
    rsi_os     = float(params["rsi_os"])
    rsi_ob     = 100.0 - rsi_os
    ema_period = int(params.get("ema_period", 0))

    RSI = ta.rsi(close_s, length=rsi_period).values.astype(float)
    ema = _ema(close_s, ema_period)
    pRSI = np.roll(RSI, 1); pRSI[0] = np.nan
    valid = ~np.isnan(RSI) & ~np.isnan(pRSI)

    sig_long  = valid & (pRSI < rsi_os) & (RSI >= rsi_os)
    sig_short = valid & (pRSI > rsi_ob) & (RSI <= rsi_ob)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, None, None


_FAMILY_FNS["rsi_scalp"] = _signals_rsi_scalp
```

- [ ] **Step 9.2: Shape test**

Append:

```python
def test_signals_rsi_scalp_shape():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_rsi_scalp
    n = 100
    close = np.linspace(100, 90, n)
    close_s = pd.Series(close)
    params = {"rsi_period": 14, "rsi_os": 30, "ema_period": 0}
    sl, ss, bbm, sd = _signals_rsi_scalp(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)
```

- [ ] **Step 9.3: Run tests, commit checkpoint.**

---

## Task 10: Signal function — bb_rsi family

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 10.1: Add bb_rsi signal function**

Append:

```python
def _signals_bb_rsi(close, high, low, close_s, high_s, low_s, params):
    bb_period   = int(params["bb_period"])
    bb_std      = float(params["bb_std"])
    bbp_long_th  = float(params["bbp_long_threshold"])
    bbp_short_th = float(params["bbp_short_threshold"])
    rsi_period   = int(params["rsi_period"])
    rsi_os       = float(params["rsi_os"])
    rsi_ob       = 100.0 - rsi_os
    ema_period   = int(params.get("ema_period", 0))
    bb_mid_exit  = bool(params.get("bb_mid_exit", False))

    BBP, BBM = _bb_arrays(close_s, close, bb_period, bb_std)
    RSI = ta.rsi(close_s, length=rsi_period).values.astype(float)
    ema = _ema(close_s, ema_period)

    valid = ~np.isnan(BBP) & ~np.isnan(RSI)
    sig_long  = valid & (BBP < bbp_long_th)  & (RSI < rsi_os)
    sig_short = valid & (BBP > bbp_short_th) & (RSI > rsi_ob)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, (BBM if bb_mid_exit else None), None


_FAMILY_FNS["bb_rsi"] = _signals_bb_rsi
```

- [ ] **Step 10.2: Shape test**

Append:

```python
def test_signals_bb_rsi_shape():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_bb_rsi
    n = 100
    close = np.full(n, 100.0); close[60:65] = [95, 92, 90, 88, 85]
    close_s = pd.Series(close)
    params = {"bb_period": 15, "bb_std": 1.5,
              "bbp_long_threshold": 0.10, "bbp_short_threshold": 0.90,
              "rsi_period": 14, "rsi_os": 30,
              "ema_period": 0, "bb_mid_exit": False}
    sl, ss, bbm, sd = _signals_bb_rsi(close, close + 0.5, close - 0.5,
                                       close_s, pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,) and bbm is None
```

- [ ] **Step 10.3: Run tests, commit checkpoint.**

---

## Task 11: Signal function — macd_cross family

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 11.1: Add macd_cross signal function**

Append:

```python
def _signals_macd_cross(close, high, low, close_s, high_s, low_s, params):
    fast = int(params["macd_fast"])
    slow = int(params["macd_slow"])
    sig  = int(params["macd_signal"])
    ema_trend = int(params.get("ema_trend", 0))

    df = ta.macd(close_s, fast=fast, slow=slow, signal=sig)
    MACD = df[[c for c in df.columns if c.startswith("MACD_")][0]].values.astype(float)
    SIG  = df[[c for c in df.columns if c.startswith("MACDs_")][0]].values.astype(float)
    trend = _ema(close_s, ema_trend) if ema_trend > 0 else None

    pM = np.roll(MACD, 1); pM[0] = np.nan
    pS = np.roll(SIG, 1);  pS[0] = np.nan
    valid = ~np.isnan(MACD) & ~np.isnan(SIG) & ~np.isnan(pM) & ~np.isnan(pS)

    sig_long  = valid & (MACD > SIG) & (pM <= pS)
    sig_short = valid & (MACD < SIG) & (pM >= pS)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, trend)
    return sig_long, sig_short, None, None


_FAMILY_FNS["macd_cross"] = _signals_macd_cross
```

- [ ] **Step 11.2: Shape test**

```python
def test_signals_macd_cross_shape():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_macd_cross
    n = 200
    rng = np.random.default_rng(2)
    close = 100 + rng.normal(0, 1, n).cumsum() * 0.1
    close_s = pd.Series(close)
    params = {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "ema_trend": 0}
    sl, ss, bbm, sd = _signals_macd_cross(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)
```

- [ ] **Step 11.3: Run tests, commit checkpoint.**

---

## Task 12: Signal function — williams_r family

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine_fast.py`

- [ ] **Step 12.1: Add williams_r signal function**

Append:

```python
def _signals_williams_r(close, high, low, close_s, high_s, low_s, params):
    wr_period  = int(params["wr_period"])
    wr_os      = float(params["wr_os"])           # negative, e.g. -80
    wr_ob      = wr_os + 100.0                    # e.g. -20
    ema_period = int(params.get("ema_period", 0))

    WR = ta.willr(high_s, low_s, close_s, length=wr_period).values.astype(float)
    ema = _ema(close_s, ema_period)
    pWR = np.roll(WR, 1); pWR[0] = np.nan
    valid = ~np.isnan(WR) & ~np.isnan(pWR)

    sig_long  = valid & (pWR < wr_os) & (WR >= wr_os)
    sig_short = valid & (pWR > wr_ob) & (WR <= wr_ob)
    sig_long, sig_short = _apply_ema_filter(sig_long, sig_short, close, ema)
    return sig_long, sig_short, None, None


_FAMILY_FNS["williams_r"] = _signals_williams_r
```

- [ ] **Step 12.2: Shape test**

```python
def test_signals_williams_r_shape():
    import pandas as pd
    from bot.backtest.engine_fast import _signals_williams_r
    n = 100
    close = np.linspace(100, 105, n)
    close_s = pd.Series(close)
    params = {"wr_period": 14, "wr_os": -80, "ema_period": 0}
    sl, ss, bbm, sd = _signals_williams_r(
        close, close + 0.5, close - 0.5, close_s,
        pd.Series(close + 0.5), pd.Series(close - 0.5), params)
    assert sl.shape == (n,)
```

- [ ] **Step 12.3: Run tests, commit checkpoint.**

---

## Task 13: Fidelity test — fast vs legacy on bb_stoch_btc

**Files:**
- Create: `hyperliquid-bot/tests/backtest/test_engine_fast_vs_legacy.py`

- [ ] **Step 13.1: Write fidelity test**

Create the file:

```python
"""
Fidelity check: fast engine vs legacy engine on bb_stoch_btc, 30 days.
Spec allows ≤ 2 trade-count delta and ≤ 1% total-PnL delta (warmup drift tolerance).
Skips if btc_5m.csv is absent.
"""
from pathlib import Path
import pytest

CSV = Path(__file__).parents[2].parent / "candles" / "btc_5m.csv"


@pytest.mark.skipif(not CSV.exists(), reason="btc_5m.csv not present")
def test_bb_stoch_btc_fast_matches_legacy_30d():
    from bot.backtest.engine import _run_backtest as _legacy
    from bot.backtest.engine_fast import _run_backtest_fast as _fast

    legacy_res = _legacy("bb_stoch_btc", "BTC", days=30,
                         trade_size_usd=1000.0, fee_rate=0.0)
    fast_res = _fast("bb_stoch_btc", "BTC", days=30,
                     trade_size_usd=1000.0, fee_rate=0.0)

    n_legacy = len(legacy_res["trades"])
    n_fast = len(fast_res["trades"])
    assert abs(n_legacy - n_fast) <= 2, f"trade-count delta {n_legacy} vs {n_fast}"

    pnl_legacy = legacy_res["metrics"]["total_pnl"]
    pnl_fast = fast_res["metrics"]["total_pnl"]
    if abs(pnl_legacy) > 0.01:
        rel = abs(pnl_legacy - pnl_fast) / abs(pnl_legacy)
        assert rel <= 0.01, f"PnL delta {rel:.4f} (legacy={pnl_legacy}, fast={pnl_fast})"
```

- [ ] **Step 13.2: Run the test**

```
pytest tests/backtest/test_engine_fast_vs_legacy.py -v -s
```
Expected: PASS or SKIP (skip is fine if CSV is absent locally). If FAIL on trade-count by more than 2, investigate before continuing — usually a signal-logic mismatch in `_signals_bb_stoch`.

- [ ] **Step 13.3: Commit checkpoint.**

---

## Task 14: Backend routes — `/backtest-compare`

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`

- [ ] **Step 14.1: Read app.py to find where backtest routes live**

```
grep -n "backtest" hyperliquid-bot/dashboard/app.py
```

You should see the existing `/backtest` page route, `/api/backtest/run`, and `/api/backtest/status/<job_id>`. Add the new routes adjacent to them.

- [ ] **Step 14.2: Add imports and routes**

Near the existing `from bot.backtest import engine` import in `app.py`, add:

```python
from bot.backtest import engine_fast
```

Add to `CHECK_CONFIGURED_EXCLUDED` (or its equivalent — look for `"backtest_page"` in the file):

```python
"backtest_compare_page",
```

Add three route handlers near the existing backtest routes:

```python
@app.route("/backtest-compare")
def backtest_compare_page():
    return render_template("backtest_compare.html")


@app.route("/api/backtest/compare/run", methods=["POST"])
def api_backtest_compare_run():
    data = request.get_json(force=True) or {}
    strategy = data.get("strategy")
    asset = data.get("asset")
    days = int(data.get("days", 90))
    trade_size_usd = float(data.get("trade_size_usd", 1000.0))
    fee_rate = float(data.get("fee_rate", 0.0))
    if not strategy or not asset:
        return jsonify({"error": "strategy e asset são obrigatórios"}), 400
    legacy_id = engine.start_backtest_job(strategy, asset, days, trade_size_usd, fee_rate)
    fast_id   = engine_fast.start_backtest_job(strategy, asset, days, trade_size_usd, fee_rate)
    return jsonify({"legacy_id": legacy_id, "fast_id": fast_id})


@app.route("/api/backtest/compare/status/<legacy_id>/<fast_id>")
def api_backtest_compare_status(legacy_id, fast_id):
    legacy = engine.get_job(legacy_id)
    fast = engine_fast.get_job(fast_id)
    if legacy is None and fast is None:
        return jsonify({"error": "Jobs não encontrados"}), 404
    return jsonify({"legacy": legacy, "fast": fast})
```

- [ ] **Step 14.3: Add `elapsed_s` to the legacy engine's job dict**

Reason: the comparison panel needs elapsed time from both engines.

Modify `bot/backtest/engine.py` — in `start_backtest_job`, add `"elapsed_s": None` to the job dict. In `_run_job`, record `started = time.time()` at the top, and on `done`/`error` set `_jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)`. Find the existing `_run_job` (around line 630) and apply this edit:

```python
def _run_job(job_id: str, strategy_name: str, asset: str, days: int,
             trade_size_usd: float, fee_rate: float):
    started = time.time()                       # NEW
    def progress_cb(msg: str):
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["progress"] = msg

    set_backtest_mode(True)
    try:
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["progress"] = "Iniciando..."

        result = _run_backtest(strategy_name, asset, days, trade_size_usd, fee_rate, progress_cb)

        elapsed = round(time.time() - started, 3)   # NEW
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["progress"] = "Concluído"
            _jobs[job_id]["result"] = result
            _jobs[job_id]["elapsed_s"] = elapsed     # NEW

    except Exception as e:
        log.error(f"[backtest job {job_id}] Error: {e}", exc_info=True)
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            _jobs[job_id]["progress"] = f"Erro: {e}"
            _jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)   # NEW
    finally:
        set_backtest_mode(False)
```

Also add `"elapsed_s": None,` to the dict initialization in `start_backtest_job` (around line 668-678).

Verify `import time` already exists at the top of `engine.py` (it does — used by `_run_backtest`).

- [ ] **Step 14.4: Smoke-test the routes via curl**

Start the dashboard (`python run.py` in `hyperliquid-bot/`) in a separate terminal. Then:

```
curl -X POST http://localhost:8080/api/backtest/compare/run \
  -H "Content-Type: application/json" \
  -d '{"strategy":"bb_stoch_btc","asset":"BTC","days":30,"trade_size_usd":1000,"fee_rate":0}'
```
Expected: `{"legacy_id":"<uuid>","fast_id":"<uuid>"}`.

Then:
```
curl http://localhost:8080/api/backtest/compare/status/<legacy_id>/<fast_id>
```
Expected: JSON with `legacy.status`, `fast.status` both eventually `done`.

- [ ] **Step 14.5: Commit checkpoint.**

---

## Task 15: Frontend — `backtest_compare.html`

**Files:**
- Create: `hyperliquid-bot/dashboard/templates/backtest_compare.html`

- [ ] **Step 15.1: Create the template**

Create `hyperliquid-bot/dashboard/templates/backtest_compare.html`:

```html
{% extends "base.html" %}
{% block title %}Backtest Compare{% endblock %}
{% block head %}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
{% endblock %}

{% block content %}
<div class="page-header">
  <h1 class="page-title">Backtest Compare &#9889;</h1>
  <p class="page-subtitle">Roda Legacy e Fast em paralelo e compara os resultados</p>
</div>

<!-- Filters (single set, drives both engines) -->
<div class="filters">
  <div class="filter-group">
    <span class="filter-label">Estrat&eacute;gia</span>
    <select id="bt-strategy" style="width:260px">
      <optgroup label="BB Reversion">
        <option value="bb_reversion_btc">BB Reversion BTC</option>
        <option value="bb_reversion_eth">BB Reversion ETH</option>
        <option value="bb_reversion_sol">BB Reversion SOL</option>
      </optgroup>
      <optgroup label="BB + Stoch">
        <option value="bb_stoch_btc">BB Stoch BTC</option>
        <option value="bb_stoch_eth">BB Stoch ETH</option>
        <option value="bb_stoch_sol">BB Stoch SOL</option>
        <option value="bb_stoch_zec">BB Stoch ZEC</option>
        <option value="bb_stoch_ton">BB Stoch TON</option>
      </optgroup>
      <optgroup label="Stoch Scalp">
        <option value="stoch_scalp_xau">Stoch Scalp XAU</option>
        <option value="stoch_scalp_wti">Stoch Scalp WTI</option>
        <option value="stoch_scalp_ton">Stoch Scalp TON</option>
      </optgroup>
      <optgroup label="EMA Cross">
        <option value="ema_cross_hype">EMA Cross HYPE</option>
        <option value="ema_cross_lit">EMA Cross LIT</option>
      </optgroup>
      <optgroup label="RSI Scalp">
        <option value="rsi_scalp_btc">RSI Scalp BTC</option>
        <option value="rsi_scalp_eth">RSI Scalp ETH</option>
        <option value="rsi_scalp_sol">RSI Scalp SOL</option>
        <option value="rsi_scalp_ton">RSI Scalp TON</option>
      </optgroup>
      <optgroup label="BB RSI">
        <option value="bb_rsi_btc">BB RSI BTC</option>
        <option value="bb_rsi_eth">BB RSI ETH</option>
        <option value="bb_rsi_sol">BB RSI SOL</option>
        <option value="bb_rsi_zec">BB RSI ZEC</option>
        <option value="bb_rsi_ton">BB RSI TON</option>
      </optgroup>
      <optgroup label="MACD Cross">
        <option value="macd_cross_btc">MACD Cross BTC</option>
        <option value="macd_cross_eth">MACD Cross ETH</option>
        <option value="macd_cross_sol">MACD Cross SOL</option>
      </optgroup>
      <optgroup label="Williams %R">
        <option value="williams_r_xau">Williams %R XAU</option>
        <option value="williams_r_wti">Williams %R WTI</option>
        <option value="williams_r_ton">Williams %R TON</option>
      </optgroup>
    </select>
  </div>
  <div class="filter-group">
    <span class="filter-label">Per&iacute;odo</span>
    <select id="bt-days" style="width:120px">
      <option value="30">30 dias</option>
      <option value="60">60 dias</option>
      <option value="90" selected>90 dias</option>
      <option value="180">180 dias</option>
    </select>
  </div>
  <div class="filter-group">
    <span class="filter-label">Tamanho ($)</span>
    <input type="number" id="bt-size" value="1000" min="10" step="100" style="width:110px">
  </div>
  <div class="filter-group">
    <span class="filter-label">Fee Rate</span>
    <input type="number" id="bt-fee" value="0" min="0" step="0.0001" style="width:100px">
  </div>
  <div class="filter-group" style="align-self:flex-end;">
    <button class="btn btn-primary" onclick="runCompare()" style="cursor:pointer">&#9654; Rodar Ambos</button>
  </div>
</div>

<!-- Loading -->
<div id="bt-loading" style="display:none; padding:16px 0; text-align:center;">
  <div class="spinner" style="display:inline-block; width:32px; height:32px; border:3px solid #1E3A5F; border-top-color:#3B82F6; border-radius:50%; animation:spin 0.8s linear infinite; margin-bottom:12px;"></div>
  <div id="bt-progress" style="color:#94A3B8; font-size:0.875rem;">Iniciando...</div>
</div>

<!-- Error -->
<div id="bt-error" style="display:none; padding:16px; background:rgba(239,68,68,0.1); border:1px solid #EF4444; border-radius:8px; color:#EF4444; margin-bottom:16px;"></div>

<!-- Comparison summary card -->
<div id="bt-summary" style="display:none; padding:16px; background:rgba(59,130,246,0.06); border:1px solid #1E3A5F; border-radius:8px; margin-bottom:24px;">
  <div style="display:flex; gap:24px; flex-wrap:wrap;">
    <div><strong>Legacy:</strong> <span id="sum-legacy">--</span></div>
    <div><strong>Fast:</strong> <span id="sum-fast">--</span></div>
    <div><strong>&Delta; trades:</strong> <span id="sum-dtrades">--</span></div>
    <div><strong>&Delta; PnL:</strong> <span id="sum-dpnl">--</span></div>
    <div><strong>Diverg&ecirc;ncias:</strong> <span id="sum-divs">--</span></div>
  </div>
</div>

<!-- Two-column panels -->
<div id="bt-panels" style="display:none; display-when-ready:grid; grid-template-columns: 1fr 1fr; gap:24px;">
  <div>
    <h3 style="margin:0 0 12px 0;">Legacy</h3>
    <div class="card-grid" id="kpi-legacy"></div>
    <div class="chart-container" style="margin-top:16px;"><div class="chart-wrap"><canvas id="chart-legacy"></canvas></div></div>
  </div>
  <div>
    <h3 style="margin:0 0 12px 0;">Fast &#9889;</h3>
    <div class="card-grid" id="kpi-fast"></div>
    <div class="chart-container" style="margin-top:16px;"><div class="chart-wrap"><canvas id="chart-fast"></canvas></div></div>
  </div>
</div>

<!-- Trade-by-trade diff -->
<div id="bt-diff" style="display:none; margin-top:24px;">
  <h3>Trade-by-trade diff</h3>
  <div class="table-container">
    <table>
      <thead>
        <tr>
          <th>Entry Time</th><th>Side</th>
          <th>Outcome (L / F)</th><th>PnL Legacy</th><th>PnL Fast</th><th>Match?</th>
        </tr>
      </thead>
      <tbody id="bt-diff-body"></tbody>
    </table>
  </div>
</div>

<style>
@keyframes spin { to { transform: rotate(360deg); } }
#bt-panels[data-ready="1"] { display: grid !important; }
.diff-match { color: #10B981; }
.diff-mismatch { color: #EF4444; }
</style>
{% endblock %}

{% block scripts %}
<script>
let pollInterval = null;
let chartLegacy = null, chartFast = null;

const STRATEGY_ASSET = {
  bb_reversion_btc:'BTC', bb_reversion_eth:'ETH', bb_reversion_sol:'SOL',
  bb_stoch_btc:'BTC', bb_stoch_eth:'ETH', bb_stoch_sol:'SOL', bb_stoch_zec:'ZEC', bb_stoch_ton:'TON',
  stoch_scalp_xau:'XAU', stoch_scalp_wti:'WTI', stoch_scalp_ton:'TON',
  ema_cross_hype:'HYPE', ema_cross_lit:'LIT',
  rsi_scalp_btc:'BTC', rsi_scalp_eth:'ETH', rsi_scalp_sol:'SOL', rsi_scalp_ton:'TON',
  bb_rsi_btc:'BTC', bb_rsi_eth:'ETH', bb_rsi_sol:'SOL', bb_rsi_zec:'ZEC', bb_rsi_ton:'TON',
  macd_cross_btc:'BTC', macd_cross_eth:'ETH', macd_cross_sol:'SOL',
  williams_r_xau:'XAU', williams_r_wti:'WTI', williams_r_ton:'TON',
};

async function runCompare() {
  clearInterval(pollInterval);
  document.getElementById('bt-summary').style.display = 'none';
  document.getElementById('bt-panels').setAttribute('data-ready','0');
  document.getElementById('bt-diff').style.display = 'none';
  document.getElementById('bt-error').style.display = 'none';
  document.getElementById('bt-loading').style.display = 'block';
  document.getElementById('bt-progress').textContent = 'Iniciando...';

  const strategy = document.getElementById('bt-strategy').value;
  const payload = {
    strategy,
    asset: STRATEGY_ASSET[strategy] || 'BTC',
    days: parseInt(document.getElementById('bt-days').value),
    trade_size_usd: parseFloat(document.getElementById('bt-size').value) || 1000,
    fee_rate: parseFloat(document.getElementById('bt-fee').value) || 0,
  };

  try {
    const res = await fetch('/api/backtest/compare/run', {
      method: 'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.legacy_id || !data.fast_id) {
      showError(data.error || 'Falha ao iniciar'); return;
    }
    pollStatus(data.legacy_id, data.fast_id);
  } catch (e) {
    showError('Erro de rede: ' + e.message);
  }
}

function pollStatus(legacyId, fastId) {
  pollInterval = setInterval(async () => {
    const res = await fetch(`/api/backtest/compare/status/${legacyId}/${fastId}`);
    const data = await res.json();
    if (!data) return;
    const legacy = data.legacy || {};
    const fast = data.fast || {};
    const progEl = document.getElementById('bt-progress');
    progEl.textContent = `Legacy: ${legacy.progress || '--'} | Fast: ${fast.progress || '--'}`;

    const bothDone = (legacy.status === 'done' || legacy.status === 'error')
                  && (fast.status === 'done' || fast.status === 'error');
    if (bothDone) {
      clearInterval(pollInterval);
      document.getElementById('bt-loading').style.display = 'none';
      renderCompare(legacy, fast);
    }
  }, 2000);
}

function showError(msg) {
  document.getElementById('bt-loading').style.display = 'none';
  const el = document.getElementById('bt-error');
  el.textContent = msg;
  el.style.display = 'block';
}

function renderCompare(legacy, fast) {
  const legacyOk = legacy.status === 'done' && legacy.result;
  const fastOk = fast.status === 'done' && fast.result;

  const lTrades = legacyOk ? legacy.result.trades : [];
  const fTrades = fastOk ? fast.result.trades : [];
  const lMetrics = legacyOk ? legacy.result.metrics : {};
  const fMetrics = fastOk ? fast.result.metrics : {};

  // Summary
  document.getElementById('sum-legacy').textContent =
    legacyOk ? `${lTrades.length} trades em ${legacy.elapsed_s}s` : (legacy.error || 'erro');
  document.getElementById('sum-fast').textContent =
    fastOk ? `${fTrades.length} trades em ${fast.elapsed_s}s` : (fast.error || 'erro');
  document.getElementById('sum-dtrades').textContent = (lTrades.length - fTrades.length);
  const dPnl = (lMetrics.total_pnl || 0) - (fMetrics.total_pnl || 0);
  document.getElementById('sum-dpnl').textContent = '$' + dPnl.toFixed(2);

  const diffs = computeDiffs(lTrades, fTrades);
  document.getElementById('sum-divs').textContent = diffs.filter(d => !d.match).length;
  document.getElementById('bt-summary').style.display = 'block';

  // Panels
  renderKpis('kpi-legacy', lMetrics);
  renderKpis('kpi-fast', fMetrics);
  chartLegacy = renderChart('chart-legacy', chartLegacy, (lMetrics.cumulative_pnl || []), '#3B82F6');
  chartFast = renderChart('chart-fast', chartFast, (fMetrics.cumulative_pnl || []), '#10B981');
  document.getElementById('bt-panels').setAttribute('data-ready', '1');

  // Diff table
  renderDiffTable(diffs);
  document.getElementById('bt-diff').style.display = 'block';
}

function renderKpis(containerId, m) {
  const el = document.getElementById(containerId);
  el.innerHTML = `
    <div class="card"><div class="card-label">Trades</div><div class="card-value">${m.total_trades || 0}</div></div>
    <div class="card"><div class="card-label">Win Rate</div><div class="card-value">${(m.win_rate || 0).toFixed(1)}%</div></div>
    <div class="card"><div class="card-label">P&amp;L</div><div class="card-value">$${(m.total_pnl || 0).toFixed(2)}</div></div>
    <div class="card"><div class="card-label">Drawdown</div><div class="card-value">$${Math.abs(m.max_drawdown || 0).toFixed(2)}</div></div>
    <div class="card"><div class="card-label">PF</div><div class="card-value">${(m.profit_factor || 0).toFixed(2)}</div></div>
    <div class="card"><div class="card-label">ROI</div><div class="card-value">${(m.roi || 0).toFixed(2)}%</div></div>
  `;
}

function renderChart(canvasId, prev, cumData, color) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  if (prev) prev.destroy();
  const labels = cumData.map(d => d.entry_time ? new Date(d.entry_time).toLocaleDateString('pt-BR',{month:'2-digit',day:'2-digit'}) : '');
  const values = cumData.map(d => d.cumulative_pnl);
  return new Chart(ctx, {
    type: 'line',
    data: { labels, datasets: [{ label:'P&L Acumulado ($)', data: values, borderColor: color,
      backgroundColor: color + '20', fill: true, tension: 0.3, pointRadius: 1, borderWidth: 2 }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { ticks: { color:'#64748B', maxTicksLimit: 15 }, grid: { color:'rgba(30,58,95,0.3)' } },
        y: { ticks: { color:'#64748B', callback: v => '$' + v }, grid: { color:'rgba(30,58,95,0.3)' } },
      },
      plugins: { legend: { labels: { color:'#94A3B8' } } },
    },
  });
}

function computeDiffs(lTrades, fTrades) {
  const byKey = new Map();
  for (const t of lTrades) byKey.set(t.entry_time + '|' + t.side, { legacy: t });
  for (const t of fTrades) {
    const k = t.entry_time + '|' + t.side;
    if (!byKey.has(k)) byKey.set(k, {});
    byKey.get(k).fast = t;
  }
  const rows = [];
  for (const [k, v] of byKey.entries()) {
    const l = v.legacy, f = v.fast;
    const outL = l ? l.outcome : '--';
    const outF = f ? f.outcome : '--';
    const pnlL = l ? l.pnl : null;
    const pnlF = f ? f.pnl : null;
    const match = l && f
      && outL === outF
      && Math.abs((pnlL || 0) - (pnlF || 0)) < 0.01;
    const [entry_time, side] = k.split('|');
    rows.push({ entry_time, side, outL, outF, pnlL, pnlF, match });
  }
  rows.sort((a, b) => (a.entry_time < b.entry_time ? -1 : 1));
  return rows;
}

function renderDiffTable(diffs) {
  const body = document.getElementById('bt-diff-body');
  if (!diffs.length) { body.innerHTML = '<tr><td colspan="6" class="empty-state">Sem trades</td></tr>'; return; }
  body.innerHTML = diffs.map(d => {
    const cls = d.match ? 'diff-match' : 'diff-mismatch';
    const dt = d.entry_time ? new Date(d.entry_time).toLocaleString('pt-BR') : '--';
    return `<tr class="${cls}">
      <td>${dt}</td>
      <td>${d.side}</td>
      <td>${d.outL} / ${d.outF}</td>
      <td>${d.pnlL !== null ? '$' + d.pnlL.toFixed(2) : '--'}</td>
      <td>${d.pnlF !== null ? '$' + d.pnlF.toFixed(2) : '--'}</td>
      <td>${d.match ? '&#10003;' : '&#10007;'}</td>
    </tr>`;
  }).join('');
}
</script>
{% endblock %}
```

- [ ] **Step 15.2: Open the page in a browser**

`http://localhost:8080/backtest-compare`. Verify it renders, select bb_stoch_btc, 30 dias, click "Rodar Ambos", and check that both panels populate.

- [ ] **Step 15.3: Commit checkpoint.**

---

## Task 16: Nav link

**Files:**
- Modify: `hyperliquid-bot/dashboard/templates/base.html`

- [ ] **Step 16.1: Find the existing Backtest nav link**

```
grep -n "backtest" hyperliquid-bot/dashboard/templates/base.html
```

- [ ] **Step 16.2: Add the Compare link next to it**

After the existing Backtest nav link, add:

```html
<a href="/backtest-compare" class="nav-link {% if request.endpoint == 'backtest_compare_page' %}active{% endif %}">Backtest &#9889;</a>
```

(The exact class names depend on the existing style — match the surrounding pattern.)

- [ ] **Step 16.3: Reload the dashboard, verify the new link appears and works.**

- [ ] **Step 16.4: Commit checkpoint.**

---

## Task 17: Manual validation pass + CLAUDE.md update

**Files:**
- Modify: `hyperliquid-bot/CLAUDE.md`

- [ ] **Step 17.1: Run compare on one instance per family**

In the dashboard, for each of these strategies, click "Rodar Ambos" with 90 dias / $1000 / fee=0 and record `Δ trades` and `Δ PnL`:

- `bb_stoch_btc`
- `bb_reversion_btc`
- `stoch_scalp_xau`
- `ema_cross_hype`   (uses ATR SL)
- `rsi_scalp_btc`
- `bb_rsi_btc`
- `macd_cross_btc`
- `williams_r_xau`

Document each in a short note. Any family with Δ trades > 5 or Δ PnL > 5% should be investigated before declaring done — likely a signal-logic mismatch.

- [ ] **Step 17.2: Update CLAUDE.md**

In `hyperliquid-bot/CLAUDE.md`, in the `backtest/` section (around line 57-61), add a new line for `engine_fast.py`:

```
    engine_fast.py      <- Vectorized backtest engine — espelha API de engine.py (start_backtest_job/get_job). Indicadores pré-computados sobre série inteira (não janela rolante de 600); sinais como máscaras booleanas numpy; outcome via numpy.argmax em slices de TP/SL/BB-mid. 8 funções _signals_<family>: bb_stoch, bb_reversion, stoch_scalp, ema_cross (suporta ATR SL via sl_dist), rsi_scalp, bb_rsi, macd_cross, williams_r. Sem cache no v1; recomputa do zero a cada run. Resultados podem divergir ligeiramente do engine.py nos primeiros candles após warmup (EMA/RSI seed diferente: rolling-600 vs full-series); converge após ~3× o período. Trades sem TP/SL hit descartados.
```

Also add to the `dashboard/` section (around line 62-66), in the description for `app.py`, after the existing backtest endpoints note:

```
  + endpoints compare: GET /backtest-compare (página), POST /api/backtest/compare/run (dispara legacy+fast em paralelo, retorna {legacy_id, fast_id}), GET /api/backtest/compare/status/<legacy_id>/<fast_id> (agrega status dos dois jobs); "backtest_compare_page" excluído do check_configured redirect
```

And add `elapsed_s` note in the engine.py description (around line 60) — append at the end:

```
; job dict agora inclui `elapsed_s` (tempo total da run em segundos) para painel comparativo
```

- [ ] **Step 17.3: Final test pass**

```
pytest tests/backtest/ -v
```
Expected: all pass (or skip if CSV absent).

- [ ] **Step 17.4: Commit checkpoint** — feature complete.

---

## Done criteria

- All `tests/backtest/` tests pass.
- Page `/backtest-compare` runs both engines and shows the comparison summary, two panels, and the trade-by-trade diff table.
- For each of the 8 families' representative instances, Δ trades ≤ 5 and Δ PnL ≤ 5% over 90 days. Larger drift documented and accepted (or fixed) by the user.
- `CLAUDE.md` updated.
