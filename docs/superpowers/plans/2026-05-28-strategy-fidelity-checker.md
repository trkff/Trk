# Strategy Fidelity Checker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an on-demand `/fidelity` page that compares the live execution of each strategy instance against the canonical backtest engine over the same period, across 3 layers (signals, trades, metrics) with drill-down to the diverging candle and automatic cause attribution.

**Architecture:** A new module `bot/fidelity/checker.py` orchestrates the comparison. It reads `signals` and `trades` from SQLite (live side) and reruns `engine._run_backtest(..., return_signals=True)` on the same CSV window (backtest side). Diffs are persisted into two new tables (`fidelity_runs` header, `fidelity_diffs` per-row) and rendered by a Flask page with score cards + tabs + drill-down modal. Strategies populate a new `signals.indicators_json` column via a shared `BaseStrategy` helper so indicator-level comparison is exact.

**Tech Stack:** Python 3.10+, SQLite (sqlite3 stdlib), Flask + SocketIO, pandas / pandas-ta / numpy (already in use), Chart.js (already loaded), pytest.

**Spec:** [docs/superpowers/specs/2026-05-28-strategy-fidelity-checker-design.md](../specs/2026-05-28-strategy-fidelity-checker-design.md)

---

## File Structure

### Create

| Path | Responsibility |
|---|---|
| `hyperliquid-bot/bot/fidelity/__init__.py` | Empty package marker |
| `hyperliquid-bot/bot/fidelity/checker.py` | `run_check()` orchestrator + 3 diff functions + score + heuristics |
| `hyperliquid-bot/bot/fidelity/job.py` | Async job registry (`start_check_job`, `get_job`) — mirrors engine.py pattern |
| `hyperliquid-bot/dashboard/templates/fidelity.html` | Page layout (form + cards + tabs + modal) |
| `hyperliquid-bot/dashboard/static/js/fidelity.js` | Frontend logic (run trigger, polling, rendering, modal) |
| `hyperliquid-bot/dashboard/static/css/fidelity.css` | Page-specific styling |
| `hyperliquid-bot/tests/fidelity/__init__.py` | Empty |
| `hyperliquid-bot/tests/fidelity/test_db_schema.py` | Migrations M9/M10 schema checks |
| `hyperliquid-bot/tests/fidelity/test_diff_signals.py` | Signal-layer diff unit tests |
| `hyperliquid-bot/tests/fidelity/test_diff_trades.py` | Trade-layer diff unit tests |
| `hyperliquid-bot/tests/fidelity/test_score.py` | Score formula tests |
| `hyperliquid-bot/tests/fidelity/test_checker_integration.py` | End-to-end with fixtures |
| `hyperliquid-bot/tests/backtest/test_engine_return_signals.py` | Regression: return_signals=True vs False |
| `hyperliquid-bot/tests/strategies/test_indicators_json.py` | All 8 strategies populate indicators_json |

### Modify

| Path | Change |
|---|---|
| `hyperliquid-bot/bot/db.py` | Migrations M9 + M10; `insert_signal` accepts `indicators_json`; helpers for runs/diffs |
| `hyperliquid-bot/bot/strategies/base.py` | `_make_indicators_snapshot(p, df)` helper |
| `hyperliquid-bot/bot/strategies/bb_stoch.py` | Inject `indicators_json` in returned signal dict |
| `hyperliquid-bot/bot/strategies/bb_reversion.py` | Same |
| `hyperliquid-bot/bot/strategies/bb_rsi.py` | Same |
| `hyperliquid-bot/bot/strategies/stoch_scalp.py` | Same |
| `hyperliquid-bot/bot/strategies/ema_cross.py` | Same |
| `hyperliquid-bot/bot/strategies/macd_cross.py` | Same |
| `hyperliquid-bot/bot/strategies/rsi_scalp.py` | Same |
| `hyperliquid-bot/bot/strategies/williams_r.py` | Same |
| `hyperliquid-bot/bot/backtest/engine.py` | `_run_backtest(..., return_signals=False)` mode |
| `hyperliquid-bot/dashboard/app.py` | Route `/fidelity` + 5 API endpoints |
| `hyperliquid-bot/dashboard/templates/base.html` | Sidebar link to `/fidelity` |

---

## Task 1: Migration M9 — `signals.indicators_json` column

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (migrate_db function, around line 220 — after M8)
- Test: `hyperliquid-bot/tests/fidelity/test_db_schema.py` (create)

- [ ] **Step 1: Write the failing test**

Create `hyperliquid-bot/tests/fidelity/__init__.py` (empty file). Then create the test:

```python
# hyperliquid-bot/tests/fidelity/test_db_schema.py
from bot import db


def _reset_conn():
    db._local.conn = None


def test_m9_adds_indicators_json_to_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(signals)").fetchall()]
    assert "indicators_json" in cols


def test_m9_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.migrate_db()  # second run must not crash
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(signals)").fetchall()]
    assert cols.count("indicators_json") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v`
Expected: FAIL — `assert 'indicators_json' in cols` is False.

- [ ] **Step 3: Add M9 to `migrate_db()` in `bot/db.py`**

Find the end of M8 (around the `_fix_profile_credential_columns` call) and append:

```python
    # M9 — add indicators_json column to signals (for fidelity checker)
    sig_cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()]
    if "indicators_json" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN indicators_json TEXT")
        conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/fidelity/__init__.py hyperliquid-bot/tests/fidelity/test_db_schema.py
git commit -m "feat(db): M9 add signals.indicators_json column for fidelity checker"
```

---

## Task 2: Migration M10 — `fidelity_runs` + `fidelity_diffs` tables

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (init_db schema block + migrate_db)
- Test: `hyperliquid-bot/tests/fidelity/test_db_schema.py` (extend)

- [ ] **Step 1: Write the failing test (append to test_db_schema.py)**

```python
def test_m10_creates_fidelity_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(fidelity_runs)").fetchall()]
    assert set(cols) >= {
        "id", "created_at", "profile_id", "strategy", "asset", "timeframe",
        "period_start_ms", "period_end_ms", "params_json",
        "live_signals", "bt_signals", "matched",
        "phantom", "missed", "side_mismatch", "price_drift", "indicator_drift",
        "fidelity_score", "live_metrics_json", "bt_metrics_json",
    }


def test_m10_creates_fidelity_diffs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(fidelity_diffs)").fetchall()]
    assert set(cols) >= {
        "id", "run_id", "ts_ms", "layer", "diff_type", "side",
        "live_json", "bt_json", "delta_pct", "notes",
    }


def test_m10_creates_indexes(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    idx = [r["name"] for r in db.get_conn().execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()]
    assert "idx_fidelity_runs_strategy_created" in idx
    assert "idx_fidelity_diffs_run_layer_type" in idx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v`
Expected: FAIL — table does not exist.

- [ ] **Step 3: Add tables to init_db's executescript**

In `bot/db.py`, find the `init_db()` `executescript("""...""")` block (the big multi-table CREATE). Append before the closing `"""`):

```sql
    CREATE TABLE IF NOT EXISTS fidelity_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        profile_id INTEGER NOT NULL,
        strategy TEXT NOT NULL,
        asset TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        period_start_ms INTEGER NOT NULL,
        period_end_ms INTEGER NOT NULL,
        params_json TEXT,
        live_signals INTEGER DEFAULT 0,
        bt_signals INTEGER DEFAULT 0,
        matched INTEGER DEFAULT 0,
        phantom INTEGER DEFAULT 0,
        missed INTEGER DEFAULT 0,
        side_mismatch INTEGER DEFAULT 0,
        price_drift INTEGER DEFAULT 0,
        indicator_drift INTEGER DEFAULT 0,
        fidelity_score REAL,
        live_metrics_json TEXT,
        bt_metrics_json TEXT
    );

    CREATE TABLE IF NOT EXISTS fidelity_diffs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        ts_ms INTEGER,
        layer TEXT NOT NULL,
        diff_type TEXT NOT NULL,
        side TEXT,
        live_json TEXT,
        bt_json TEXT,
        delta_pct REAL,
        notes TEXT,
        FOREIGN KEY (run_id) REFERENCES fidelity_runs(id)
    );

    CREATE INDEX IF NOT EXISTS idx_fidelity_runs_strategy_created ON fidelity_runs(strategy, created_at);
    CREATE INDEX IF NOT EXISTS idx_fidelity_diffs_run_layer_type ON fidelity_diffs(run_id, layer, diff_type);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/fidelity/test_db_schema.py
git commit -m "feat(db): M10 add fidelity_runs and fidelity_diffs tables"
```

---

## Task 3: DB helpers for fidelity runs/diffs

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (append helpers at end of file, before any trailing `if __name__`)
- Test: `hyperliquid-bot/tests/fidelity/test_db_schema.py` (extend)

- [ ] **Step 1: Write the failing test (append)**

```python
def test_insert_fidelity_run_returns_id(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    rid = db.insert_fidelity_run({
        "created_at": "2026-05-28T12:00:00+00:00",
        "profile_id": 1,
        "strategy": "bb_stoch_btc_5m",
        "asset": "BTC",
        "timeframe": "5m",
        "period_start_ms": 1_700_000_000_000,
        "period_end_ms": 1_700_086_400_000,
        "params_json": "{}",
        "live_signals": 10, "bt_signals": 10, "matched": 9,
        "phantom": 0, "missed": 1, "side_mismatch": 0,
        "price_drift": 0, "indicator_drift": 0,
        "fidelity_score": 0.95,
        "live_metrics_json": "{}", "bt_metrics_json": "{}",
    })
    assert isinstance(rid, int) and rid > 0


def test_insert_fidelity_diff_links_to_run(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    rid = db.insert_fidelity_run({
        "created_at": "2026-05-28T12:00:00+00:00", "profile_id": 1,
        "strategy": "bb_stoch_btc_5m", "asset": "BTC", "timeframe": "5m",
        "period_start_ms": 0, "period_end_ms": 1,
    })
    did = db.insert_fidelity_diff({
        "run_id": rid, "ts_ms": 1_700_000_000_000,
        "layer": "signal", "diff_type": "missed", "side": "long",
        "live_json": None, "bt_json": '{"signal_price": 50000}',
        "delta_pct": None, "notes": "WS gap",
    })
    assert isinstance(did, int) and did > 0
    rows = db.get_fidelity_diffs(rid)
    assert len(rows) == 1 and rows[0]["diff_type"] == "missed"


def test_list_fidelity_runs_orders_by_created_desc(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    for ts in ("2026-05-26T00:00:00+00:00", "2026-05-28T00:00:00+00:00", "2026-05-27T00:00:00+00:00"):
        db.insert_fidelity_run({
            "created_at": ts, "profile_id": 1,
            "strategy": "x", "asset": "BTC", "timeframe": "5m",
            "period_start_ms": 0, "period_end_ms": 1,
        })
    runs = db.list_fidelity_runs(limit=10)
    assert [r["created_at"] for r in runs] == [
        "2026-05-28T00:00:00+00:00",
        "2026-05-27T00:00:00+00:00",
        "2026-05-26T00:00:00+00:00",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v -k "insert_fidelity or list_fidelity"`
Expected: FAIL — `AttributeError: module 'bot.db' has no attribute 'insert_fidelity_run'`.

- [ ] **Step 3: Add helpers to `bot/db.py`** (append near the bottom, alongside other domain helpers like `insert_signal`)

```python
# ── Fidelity helpers ────────────────────────────────────────────────

_FIDELITY_RUN_COLS = (
    "created_at", "profile_id", "strategy", "asset", "timeframe",
    "period_start_ms", "period_end_ms", "params_json",
    "live_signals", "bt_signals", "matched",
    "phantom", "missed", "side_mismatch", "price_drift", "indicator_drift",
    "fidelity_score", "live_metrics_json", "bt_metrics_json",
)


def insert_fidelity_run(run: dict) -> int:
    cols = [c for c in _FIDELITY_RUN_COLS if c in run]
    placeholders = ",".join(f":{c}" for c in cols)
    conn = get_conn()
    cur = conn.execute(
        f"INSERT INTO fidelity_runs ({','.join(cols)}) VALUES ({placeholders})",
        run,
    )
    conn.commit()
    return cur.lastrowid


def insert_fidelity_diff(diff: dict) -> int:
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO fidelity_diffs
            (run_id, ts_ms, layer, diff_type, side, live_json, bt_json, delta_pct, notes)
        VALUES
            (:run_id, :ts_ms, :layer, :diff_type, :side, :live_json, :bt_json, :delta_pct, :notes)
        """,
        diff,
    )
    conn.commit()
    return cur.lastrowid


def insert_fidelity_diffs_bulk(diffs: list[dict]) -> int:
    if not diffs:
        return 0
    conn = get_conn()
    conn.executemany(
        """
        INSERT INTO fidelity_diffs
            (run_id, ts_ms, layer, diff_type, side, live_json, bt_json, delta_pct, notes)
        VALUES
            (:run_id, :ts_ms, :layer, :diff_type, :side, :live_json, :bt_json, :delta_pct, :notes)
        """,
        diffs,
    )
    conn.commit()
    return len(diffs)


def list_fidelity_runs(limit: int = 20, profile_id: int | None = None) -> list[dict]:
    q = "SELECT * FROM fidelity_runs"
    params: list = []
    if profile_id is not None:
        q += " WHERE profile_id = ?"
        params.append(profile_id)
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in get_conn().execute(q, params).fetchall()]


def get_fidelity_run(run_id: int) -> dict | None:
    row = get_conn().execute("SELECT * FROM fidelity_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_fidelity_diffs(run_id: int, layer: str | None = None,
                      diff_type: str | None = None) -> list[dict]:
    q = "SELECT * FROM fidelity_diffs WHERE run_id = ?"
    params: list = [run_id]
    if layer:
        q += " AND layer = ?"
        params.append(layer)
    if diff_type:
        q += " AND diff_type = ?"
        params.append(diff_type)
    q += " ORDER BY ts_ms ASC, id ASC"
    return [dict(r) for r in get_conn().execute(q, params).fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v`
Expected: PASS (all 8 tests now).

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/fidelity/test_db_schema.py
git commit -m "feat(db): add fidelity_runs/diffs CRUD helpers"
```

---

## Task 4: Extend `insert_signal` to accept `indicators_json`

**Files:**
- Modify: `hyperliquid-bot/bot/db.py:1077` (`insert_signal`)
- Test: `hyperliquid-bot/tests/fidelity/test_db_schema.py` (extend)

- [ ] **Step 1: Write the failing test (append)**

```python
def test_insert_signal_persists_indicators_json(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    sid = db.insert_signal({
        "timestamp": "2026-05-28T12:00:00+00:00", "asset": "BTC", "side": "long",
        "executed": 1, "reason": None,
        "ema9": None, "ema21": None, "rsi2": 0,
        "volume": 1.0, "volume_avg": 1.0, "atr": 100.0, "funding_rate": 0.0,
        "strategy_name": "bb_stoch_btc_5m",
        "indicators_json": '{"bbp": 0.05, "stoch_k": 12.3}',
    })
    row = db.get_conn().execute("SELECT indicators_json FROM signals WHERE id = ?", (sid,)).fetchone()
    assert row["indicators_json"] == '{"bbp": 0.05, "stoch_k": 12.3}'


def test_insert_signal_backwards_compatible_without_indicators_json(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    sid = db.insert_signal({
        "timestamp": "2026-05-28T12:00:00+00:00", "asset": "BTC", "side": "long",
        "executed": 1, "reason": None,
        "ema9": None, "ema21": None, "rsi2": 0,
        "volume": 1.0, "volume_avg": 1.0, "atr": 100.0, "funding_rate": 0.0,
        "strategy_name": "bb_stoch_btc_5m",
    })
    row = db.get_conn().execute("SELECT indicators_json FROM signals WHERE id = ?", (sid,)).fetchone()
    assert row["indicators_json"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v -k indicators_json`
Expected: FAIL — column not in INSERT statement.

- [ ] **Step 3: Update `insert_signal` in `bot/db.py`**

Replace the body of `insert_signal` (around line 1077):

```python
def insert_signal(signal: dict) -> int:
    conn = get_conn()
    signal.setdefault("strategy_name", "mean_reversion")
    signal.setdefault("profile_id", 1)
    signal.setdefault("indicators_json", None)
    cur = conn.execute("""
        INSERT INTO signals (profile_id, timestamp, asset, side, executed, reason,
                             ema9, ema21, rsi2, volume, volume_avg, atr, funding_rate,
                             strategy_name, indicators_json)
        VALUES (:profile_id, :timestamp, :asset, :side, :executed, :reason,
                :ema9, :ema21, :rsi2, :volume, :volume_avg, :atr, :funding_rate,
                :strategy_name, :indicators_json)
    """, signal)
    conn.commit()
    return cur.lastrowid
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_db_schema.py -v`
Expected: PASS (all 10 tests).

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/fidelity/test_db_schema.py
git commit -m "feat(db): insert_signal accepts indicators_json (backwards compatible)"
```

---

## Task 5: `BaseStrategy._make_indicators_snapshot()` helper + bb_stoch wiring

**Rationale:** Centralize the indicator-snapshot logic in the base class so each strategy only needs to call the helper with the values it computed. We'll wire bb_stoch first as a reference; remaining 7 strategies are Task 7.

**Files:**
- Modify: `hyperliquid-bot/bot/strategies/base.py`
- Modify: `hyperliquid-bot/bot/strategies/bb_stoch.py`
- Test: `hyperliquid-bot/tests/strategies/test_indicators_json.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# hyperliquid-bot/tests/strategies/test_indicators_json.py
import json
import pandas as pd
import numpy as np
import pandas_ta as ta

from bot.strategies.manager import STRATEGY_MAP


def _synth_df(n=80, start_price=100.0, seed=1):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.005, n)
    closes = start_price * np.exp(np.cumsum(rets))
    highs = closes * (1 + np.abs(rng.normal(0, 0.002, n)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.002, n)))
    ts_ms = np.arange(n) * 300_000 + 1_700_000_000_000
    return pd.DataFrame({"timestamp": ts_ms, "open": closes, "high": highs,
                         "low": lows, "close": closes, "volume": np.ones(n)})


def _make_indicators_stub():
    return {"ema9": 100.0, "ema21": 100.0, "rsi2": 50.0,
            "volume": 1.0, "volume_avg": 1.0,
            "atr": 1.0, "close_1m": 100.0,
            "volume_5m": 1.0, "volume_avg_5m": 1.0, "atr_5m": 1.0}


def test_bb_stoch_signal_includes_indicators_json():
    # Find any bb_stoch instance
    name = next(n for n in STRATEGY_MAP if n.startswith("bb_stoch_"))
    strat = STRATEGY_MAP[name]
    df = _synth_df(n=120)
    # Force conditions that will (sometimes) trigger; just ensure helper produces JSON
    sig = strat.evaluate(
        asset=(strat.DEFAULT_PARAMS.get("assets") or ["BTC"])[0],
        indicators=_make_indicators_stub(),
        funding_rate=0.0,
        cfg={}, params={},
        df_5m=df,
        new_5m=True,
    )
    # If no signal fires, we cannot assert. Run a synthetic forced-trigger by
    # manipulating df to extreme BBP at the last candle.
    if sig is None:
        # Push last close way below BBL — guaranteed BBP < 0 trigger zone
        df.loc[df.index[-1], "close"] = df["close"].iloc[-2] * 0.85
        df.loc[df.index[-1], "low"] = df["close"].iloc[-1]
        sig = strat.evaluate(
            asset=(strat.DEFAULT_PARAMS.get("assets") or ["BTC"])[0],
            indicators=_make_indicators_stub(),
            funding_rate=0.0, cfg={}, params={}, df_5m=df, new_5m=True,
        )
    assert sig is not None, "Could not force a signal for bb_stoch"
    assert "indicators_json" in sig
    payload = json.loads(sig["indicators_json"])
    assert "bbp" in payload
    assert "stoch_k" in payload
    assert "stoch_d" in payload
    assert "close" in payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/strategies/test_indicators_json.py -v`
Expected: FAIL — `"indicators_json" not in sig`.

- [ ] **Step 3: Add helper to `BaseStrategy`**

In `bot/strategies/base.py`, inside class `BaseStrategy` (before `evaluate`):

```python
    @staticmethod
    def _make_indicators_snapshot(values: dict) -> str:
        """Serialize an indicator snapshot to JSON.

        Filters NaN/None/inf values; rounds floats to 6 decimals to keep the
        payload small and reproducible across re-runs of the backtest engine.
        """
        import json, math
        clean: dict = {}
        for k, v in values.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                clean[k] = v
                continue
            if math.isnan(fv) or math.isinf(fv):
                continue
            clean[k] = round(fv, 6)
        return json.dumps(clean)
```

- [ ] **Step 4: Wire bb_stoch to use the helper**

In `bot/strategies/bb_stoch.py`, inside `evaluate()`, just before the LONG return block (around line 198, where `if long_bb and long_stoch:` starts), build the snapshot once:

```python
        indicators_json = self._make_indicators_snapshot({
            "close": close_curr,
            "bbu": bbu_curr, "bbl": bbl_curr, "bbm": bbm_curr,
            "bbp": bbp_curr,
            "stoch_k": stk_curr, "stoch_d": std_curr,
            "ema": ema_val,
        })
```

Then add `"indicators_json": indicators_json,` to both the LONG and SHORT return dicts (the dicts passed to `apply_live_filters`).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/strategies/test_indicators_json.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot/bot/strategies/base.py hyperliquid-bot/bot/strategies/bb_stoch.py hyperliquid-bot/tests/strategies/test_indicators_json.py
git commit -m "feat(strategies): add _make_indicators_snapshot helper + wire bb_stoch"
```

---

## Task 6: Wire `indicators_json` into the executor + insert_signal path

**Context:** The strategy returns a signal dict including `indicators_json`. The executor / signals-insertion path must forward this field to `db.insert_signal`. We need to verify that nothing strips it on the way.

**Files:**
- Modify: `hyperliquid-bot/bot/strategies/live_filters.py` (verify it propagates `indicators_json` through `apply_live_filters`)
- Modify: `hyperliquid-bot/bot/executor.py` (verify signal insertion includes it)
- Test: `hyperliquid-bot/tests/fidelity/test_signal_persistence.py` (create)

- [ ] **Step 1: Read the propagation path**

Run: `grep -n "insert_signal\|signal_price" hyperliquid-bot/bot/executor.py hyperliquid-bot/bot/strategies/live_filters.py | head -30`

Identify where `insert_signal(signal)` is called with the full signal dict. The dict is passed through; `apply_live_filters` returns a `signal` dict (or None) — it should already pass `indicators_json` through since it's a passthrough of `{**signal, ...}`. Verify by reading those files.

- [ ] **Step 2: Write the failing integration test**

```python
# hyperliquid-bot/tests/fidelity/test_signal_persistence.py
import json
from bot import db


def _reset_conn():
    db._local.conn = None


def test_signal_with_indicators_json_persists_through_insert(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    signal = {
        "timestamp": "2026-05-28T12:00:00+00:00", "asset": "BTC", "side": "long",
        "executed": 1, "reason": None,
        "ema9": None, "ema21": None, "rsi2": 0,
        "volume": 1.0, "volume_avg": 1.0, "atr": 100.0, "funding_rate": 0.0,
        "strategy_name": "bb_stoch_btc_5m",
        "indicators_json": json.dumps({"bbp": 0.05, "stoch_k": 12.0, "close": 50000.0}),
    }
    sid = db.insert_signal(signal)
    sigs = db.get_signals(strategy_name="bb_stoch_btc_5m")
    assert any(s["id"] == sid for s in sigs)
```

- [ ] **Step 3: Run the test**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_signal_persistence.py -v`
Expected: PASS (no code changes needed yet — Tasks 1+4 already added the column and the param). If it fails, fix `insert_signal` accordingly.

- [ ] **Step 4: Extend `get_signals` to return `indicators_json`**

`get_signals` already does `SELECT *` so it should include the new column automatically. Add an assertion test:

```python
def test_get_signals_returns_indicators_json(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.insert_signal({
        "timestamp": "2026-05-28T12:00:00+00:00", "asset": "BTC", "side": "long",
        "executed": 1, "reason": None,
        "ema9": None, "ema21": None, "rsi2": 0,
        "volume": 1.0, "volume_avg": 1.0, "atr": 100.0, "funding_rate": 0.0,
        "strategy_name": "bb_stoch_btc_5m",
        "indicators_json": '{"bbp": 0.05}',
    })
    sigs = db.get_signals(strategy_name="bb_stoch_btc_5m")
    assert sigs[0]["indicators_json"] == '{"bbp": 0.05}'
```

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_signal_persistence.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/tests/fidelity/test_signal_persistence.py
git commit -m "test(fidelity): verify indicators_json survives the live signal path"
```

---

## Task 7: Wire `indicators_json` into remaining 7 strategies

**Files (modify each):**
- `hyperliquid-bot/bot/strategies/bb_reversion.py`
- `hyperliquid-bot/bot/strategies/bb_rsi.py`
- `hyperliquid-bot/bot/strategies/stoch_scalp.py`
- `hyperliquid-bot/bot/strategies/ema_cross.py`
- `hyperliquid-bot/bot/strategies/macd_cross.py`
- `hyperliquid-bot/bot/strategies/rsi_scalp.py`
- `hyperliquid-bot/bot/strategies/williams_r.py`

- Test: `hyperliquid-bot/tests/strategies/test_indicators_json.py` (extend with parametrized test)

- [ ] **Step 1: Write the failing parametrized test (append)**

```python
import pytest

STRATEGY_FAMILY_KEYS = {
    "bb_reversion": ["close", "bbp", "bbm", "bbu", "bbl", "rsi"],
    "bb_rsi":       ["close", "bbp", "bbu", "bbl", "rsi"],
    "stoch_scalp":  ["close", "stoch_k", "stoch_d"],
    "ema_cross":    ["close", "ema_fast", "ema_slow"],
    "macd_cross":   ["close", "macd", "macd_signal"],
    "rsi_scalp":    ["close", "rsi"],
    "williams_r":   ["close", "wr"],
}


@pytest.mark.parametrize("family,required_keys", STRATEGY_FAMILY_KEYS.items())
def test_each_family_emits_indicators_json(family, required_keys):
    name = next((n for n in STRATEGY_MAP if n.startswith(f"{family}_")), None)
    if name is None:
        pytest.skip(f"No registered instance for family {family}")
    strat = STRATEGY_MAP[name]
    df = _synth_df(n=300, seed=hash(family) & 0xFF)
    asset = (strat.DEFAULT_PARAMS.get("assets") or ["BTC"])[0]
    # Push final candle into multiple extreme positions to maximize chance of trigger
    sig = None
    for mult in (1.0, 0.80, 1.20, 0.70, 1.30):
        df2 = df.copy()
        df2.loc[df2.index[-1], "close"] = df["close"].iloc[-2] * mult
        df2.loc[df2.index[-1], "low"] = min(df2["close"].iloc[-1], df["low"].iloc[-1])
        df2.loc[df2.index[-1], "high"] = max(df2["close"].iloc[-1], df["high"].iloc[-1])
        sig = strat.evaluate(
            asset=asset, indicators=_make_indicators_stub(), funding_rate=0.0,
            cfg={}, params={}, df_5m=df2, new_5m=True,
        )
        if sig is not None:
            break
    if sig is None:
        pytest.skip(f"Could not force a signal for {family}")
    assert "indicators_json" in sig, f"{family} signal missing indicators_json"
    payload = json.loads(sig["indicators_json"])
    missing = [k for k in required_keys if k not in payload]
    assert not missing, f"{family} indicators_json missing keys: {missing}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/strategies/test_indicators_json.py -v -k each_family`
Expected: FAIL — 7 strategies missing `indicators_json`.

- [ ] **Step 3: Wire each strategy**

For each of the 7 files, repeat this pattern: inside `evaluate()`, just before the first return that builds a signal dict, compute the snapshot and inject `"indicators_json"` into both LONG and SHORT signal dicts.

**bb_reversion.py** — keys to include:
```python
indicators_json = self._make_indicators_snapshot({
    "close": close_curr, "bbp": bbp_curr, "bbu": bbu_curr,
    "bbl": bbl_curr, "bbm": bbm_curr, "rsi": rsi_curr,
    "ema": ema_val,
})
```

**bb_rsi.py** — keys:
```python
indicators_json = self._make_indicators_snapshot({
    "close": close_curr, "bbp": bbp_curr, "bbu": bbu_curr,
    "bbl": bbl_curr, "rsi": rsi_curr, "ema": ema_val,
})
```

**stoch_scalp.py** — keys:
```python
indicators_json = self._make_indicators_snapshot({
    "close": close_curr, "stoch_k": stk_curr, "stoch_d": std_curr,
    "stoch_k_prev": stk_prev, "stoch_d_prev": std_prev,
    "ema": ema_val,
})
```

**ema_cross.py** — keys:
```python
indicators_json = self._make_indicators_snapshot({
    "close": close_curr,
    "ema_fast": ema_fast_curr, "ema_slow": ema_slow_curr,
    "ema_fast_prev": ema_fast_prev, "ema_slow_prev": ema_slow_prev,
    "ema_trend": ema_trend_val, "atr": atr_val,
})
```

**macd_cross.py** — keys:
```python
indicators_json = self._make_indicators_snapshot({
    "close": close_curr,
    "macd": macd_curr, "macd_signal": sig_curr,
    "macd_prev": macd_prev, "macd_signal_prev": sig_prev,
    "ema_trend": ema_trend_val,
})
```

**rsi_scalp.py** — keys:
```python
indicators_json = self._make_indicators_snapshot({
    "close": close_curr, "rsi": rsi_curr, "rsi_prev": rsi_prev,
    "ema": ema_val,
})
```

**williams_r.py** — keys:
```python
indicators_json = self._make_indicators_snapshot({
    "close": close_curr, "wr": wr_curr, "wr_prev": wr_prev,
    "ema": ema_val,
})
```

Add `"indicators_json": indicators_json,` to each signal dict (both LONG and SHORT branches). For values referenced above (e.g., `ema_fast_prev`) that the existing code doesn't extract yet, hoist them out of the existing calculation a couple of lines earlier — read the file to find the existing variable names. If a value isn't computed, omit it from the dict — `_make_indicators_snapshot` skips Nones.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/strategies/test_indicators_json.py -v`
Expected: PASS for all 8 families (the one from Task 5 plus 7 new ones). Some families may `skip` if the synth df can't force a trigger; that's acceptable as long as none fail.

- [ ] **Step 5: Run the full strategy test suite to ensure no regression**

Run: `cd hyperliquid-bot && python -m pytest tests/strategies/ -v`
Expected: PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot/bot/strategies/bb_reversion.py hyperliquid-bot/bot/strategies/bb_rsi.py hyperliquid-bot/bot/strategies/stoch_scalp.py hyperliquid-bot/bot/strategies/ema_cross.py hyperliquid-bot/bot/strategies/macd_cross.py hyperliquid-bot/bot/strategies/rsi_scalp.py hyperliquid-bot/bot/strategies/williams_r.py hyperliquid-bot/tests/strategies/test_indicators_json.py
git commit -m "feat(strategies): wire indicators_json snapshot in remaining 7 families"
```

---

## Task 8: Engine `return_signals=True` mode

**Goal:** Add a flag to `_run_backtest` that, when True, returns an additional `signals` list — one entry per candle where `sig_long[i]` or `sig_short[i]` is True, with the indicator snapshot.

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine.py:284` (`_run_backtest`)
- Test: `hyperliquid-bot/tests/backtest/test_engine_return_signals.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# hyperliquid-bot/tests/backtest/test_engine_return_signals.py
import os
import pandas as pd
import pytest

from bot.backtest import engine, csv_loader


@pytest.fixture
def fake_csv(tmp_path, monkeypatch):
    """Create a synthetic 5m CSV the engine can load."""
    import numpy as np
    n = 600
    ts_ms = 1_700_000_000_000 + np.arange(n) * 300_000
    rng = np.random.default_rng(42)
    rets = rng.normal(0, 0.005, n)
    closes = 50000 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "timestamp": ts_ms,
        "open": closes, "high": closes * 1.001, "low": closes * 0.999,
        "close": closes, "volume": 1.0,
    })
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    df.to_csv(candles_dir / "btc_5m.csv", index=False)
    monkeypatch.setattr(csv_loader, "_CANDLES_DIR", candles_dir)
    monkeypatch.setattr(csv_loader, "_update_csv", lambda *a, **k: None)
    return candles_dir


def test_return_signals_false_unchanged_shape(fake_csv, monkeypatch):
    # Patch DB to in-memory and pre-seed bb_stoch_btc_5m strategy config
    from bot import db
    db._local.conn = None
    monkeypatch.setattr(db, "DB_PATH", fake_csv.parent / "t.db")
    db.init_db()
    result = engine._run_backtest("bb_stoch", "BTC", days=30,
                                   trade_size_usd=1000.0, fee_rate=0.0)
    assert set(result.keys()) >= {"trades", "metrics", "strategy_resolved"}
    assert "signals" not in result


def test_return_signals_true_includes_signals_list(fake_csv, monkeypatch):
    from bot import db
    db._local.conn = None
    monkeypatch.setattr(db, "DB_PATH", fake_csv.parent / "t.db")
    db.init_db()
    result = engine._run_backtest("bb_stoch", "BTC", days=30,
                                   trade_size_usd=1000.0, fee_rate=0.0,
                                   return_signals=True)
    assert "signals" in result
    assert isinstance(result["signals"], list)
    for s in result["signals"]:
        assert {"ts_ms", "side", "signal_price", "indicators"} <= set(s.keys())
        assert s["side"] in ("long", "short")


def test_return_signals_does_not_change_trades(fake_csv, monkeypatch):
    from bot import db
    db._local.conn = None
    monkeypatch.setattr(db, "DB_PATH", fake_csv.parent / "t.db")
    db.init_db()
    a = engine._run_backtest("bb_stoch", "BTC", days=30, trade_size_usd=1000.0, fee_rate=0.0)
    db._local.conn = None
    monkeypatch.setattr(db, "DB_PATH", fake_csv.parent / "t.db")
    b = engine._run_backtest("bb_stoch", "BTC", days=30, trade_size_usd=1000.0, fee_rate=0.0,
                              return_signals=True)
    assert len(a["trades"]) == len(b["trades"])
    for t1, t2 in zip(a["trades"], b["trades"]):
        assert t1["entry_time"] == t2["entry_time"]
        assert t1["entry_price"] == t2["entry_price"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/backtest/test_engine_return_signals.py -v`
Expected: FAIL — `_run_backtest` doesn't accept `return_signals`.

- [ ] **Step 3: Modify `_run_backtest` to support the flag**

In `bot/backtest/engine.py`, change the signature (line 284):

```python
def _run_backtest(strategy_name, asset, days, trade_size_usd, fee_rate, progress_cb=None,
                  profile_id: int = 1, return_signals: bool = False):
```

After the `sig_long, sig_short` lines (around line 318) and after `_apply_v2_filters` finalizes them, build the signal list when requested. **Important:** the indicator snapshot needs to include the same fields the strategies emit. Add a per-family helper near the family functions:

```python
# Near the bottom of the file, add a snapshot extractor per family
def _snapshot_bb_stoch(close, BBP, BBM, BBU, BBL, K, D, ema, i):
    return {"close": float(close[i]),
            "bbp": _safe(BBP, i), "bbm": _safe(BBM, i),
            "bbu": _safe(BBU, i), "bbl": _safe(BBL, i),
            "stoch_k": _safe(K, i), "stoch_d": _safe(D, i),
            "ema": _safe(ema, i)}


def _safe(arr, i):
    if arr is None:
        return None
    v = float(arr[i])
    import math
    return None if (math.isnan(v) or math.isinf(v)) else round(v, 6)
```

Refactor each `_signals_<family>` to return a 5-tuple `(sig_long, sig_short, bb_mid, sl_dist, snapshot_fn)` where `snapshot_fn` is a closure `(i) -> dict` for that family. Example for bb_stoch:

```python
def _signals_bb_stoch(close, high, low, close_s, high_s, low_s, params):
    # ... existing code computing BBP, BBM, K, D, ema, BBU, BBL ...
    # (you must extract BBU/BBL too; currently bb_stoch only uses BBM/BBP)
    bb = ta.bbands(close_s, length=bb_period, std=bb_std)
    BBU = bb[[c for c in bb.columns if c.startswith("BBU_")][0]].values.astype(float)
    BBL = bb[[c for c in bb.columns if c.startswith("BBL_")][0]].values.astype(float)
    # ... same sig_long/sig_short logic ...
    def snap(i):
        return _snapshot_bb_stoch(close, BBP, BBM, BBU, BBL, K, D, ema, i)
    return sig_long, sig_short, bb_mid_out, None, snap
```

Update `_run_backtest` to unpack the 5-tuple:
```python
    sig_long, sig_short, bb_mid, sl_dist, snap_fn = fn(close, high, low, close_s, high_s, low_s, params)
```

After the v2 filters, build the signals list when requested:

```python
    signals_out = []
    if return_signals:
        import numpy as np
        idx_long = np.where(sig_long)[0]
        idx_short = np.where(sig_short)[0]
        for i in idx_long:
            signals_out.append({
                "ts_ms": int(ts[i]), "side": "long",
                "signal_price": round(float(close[i]), 6),
                "indicators": snap_fn(int(i)),
            })
        for i in idx_short:
            signals_out.append({
                "ts_ms": int(ts[i]), "side": "short",
                "signal_price": round(float(close[i]), 6),
                "indicators": snap_fn(int(i)),
            })
        # Clamp to requested period (same logic as filtered trades)
        cutoff_ms = int(time.time() * 1000) - days * 86_400_000
        signals_out = [s for s in signals_out if s["ts_ms"] >= cutoff_ms]
        signals_out.sort(key=lambda s: s["ts_ms"])
```

At the return:
```python
    result = {
        "trades": trades_with_pnl,
        "metrics": metrics,
        "strategy_resolved": strategy_name,
    }
    if return_signals:
        result["signals"] = signals_out
    return result
```

Apply the same snapshot refactor to the other 7 `_signals_<family>` functions, each returning a closure that reads the indicator arrays already computed in that scope. Keys per family must match the live `indicators_json` keys established in Task 5/7 so the diff has matching field names:

| Family | Snapshot keys |
|---|---|
| bb_stoch | close, bbp, bbm, bbu, bbl, stoch_k, stoch_d, ema |
| bb_reversion | close, bbp, bbm, bbu, bbl, rsi, ema |
| bb_rsi | close, bbp, bbu, bbl, rsi, ema |
| stoch_scalp | close, stoch_k, stoch_d, stoch_k_prev, stoch_d_prev, ema |
| ema_cross | close, ema_fast, ema_slow, ema_fast_prev, ema_slow_prev, ema_trend, atr |
| macd_cross | close, macd, macd_signal, macd_prev, macd_signal_prev, ema_trend |
| rsi_scalp | close, rsi, rsi_prev, ema |
| williams_r | close, wr, wr_prev, ema |

For `*_prev` values, compute via `np.roll(arr, 1)` and pass into the closure. **Hoist arrays the family didn't previously need** (e.g., bb_stoch wasn't extracting BBU/BBL; bb_reversion already does — read each file to confirm what's missing).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/backtest/test_engine_return_signals.py -v`
Expected: PASS.

- [ ] **Step 5: Run existing backtest tests for regression**

Run: `cd hyperliquid-bot && python -m pytest tests/backtest/ -v`
Expected: PASS (no regressions). If any pre-existing test fails because it now hits the new 5-tuple unpack inside a family fn, audit and fix — the production caller (`_run_backtest`) is the only entry point.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot/bot/backtest/engine.py hyperliquid-bot/tests/backtest/test_engine_return_signals.py
git commit -m "feat(backtest): engine return_signals mode with per-family snapshots"
```

---

## Task 9: `checker.py` — signal-layer diff

**Files:**
- Create: `hyperliquid-bot/bot/fidelity/__init__.py` (empty)
- Create: `hyperliquid-bot/bot/fidelity/checker.py`
- Test: `hyperliquid-bot/tests/fidelity/test_diff_signals.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# hyperliquid-bot/tests/fidelity/test_diff_signals.py
import json
import pytest

from bot.fidelity.checker import diff_signals, PRICE_TOL, IND_TOL


def _live(ts, side, price, indicators=None):
    return {
        "ts_ms": ts, "side": side, "signal_price": price,
        "indicators_json": json.dumps(indicators) if indicators else None,
    }


def _bt(ts, side, price, indicators):
    return {"ts_ms": ts, "side": side, "signal_price": price, "indicators": indicators}


def test_exact_match_is_matched():
    live = [_live(1000, "long", 100.0, {"bbp": 0.05})]
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["matched"] == 1
    assert out["diffs"] == []


def test_phantom_when_live_only():
    live = [_live(1000, "long", 100.0, {"bbp": 0.05})]
    bt = []
    out = diff_signals(live, bt)
    assert out["phantom"] == 1
    assert any(d["diff_type"] == "phantom" for d in out["diffs"])


def test_missed_when_bt_only():
    live = []
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["missed"] == 1
    assert any(d["diff_type"] == "missed" for d in out["diffs"])


def test_side_mismatch():
    live = [_live(1000, "long", 100.0)]
    bt = [_bt(1000, "short", 100.0, {})]
    out = diff_signals(live, bt)
    assert out["side_mismatch"] == 1


def test_price_drift_above_tolerance():
    live = [_live(1000, "long", 100.10, {"bbp": 0.05})]   # 0.1% above
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["price_drift"] == 1
    d = next(d for d in out["diffs"] if d["diff_type"] == "price")
    assert d["delta_pct"] is not None and d["delta_pct"] > PRICE_TOL


def test_price_drift_within_tolerance_is_matched():
    live = [_live(1000, "long", 100.02, {"bbp": 0.05})]   # 0.02% < 0.05%
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.05})]
    out = diff_signals(live, bt)
    assert out["price_drift"] == 0
    assert out["matched"] == 1


def test_indicator_drift():
    live = [_live(1000, "long", 100.0, {"bbp": 0.05, "stoch_k": 20.0})]
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.06, "stoch_k": 25.0})]  # 20% drift on bbp, 25% on stoch_k
    out = diff_signals(live, bt)
    assert out["indicator_drift"] >= 1
    inds = [d for d in out["diffs"] if d["diff_type"] == "indicator"]
    assert any("bbp" in (d.get("notes") or "") for d in inds)


def test_indicator_drift_skipped_when_live_lacks_indicators():
    live = [_live(1000, "long", 100.0, indicators=None)]
    bt = [_bt(1000, "long", 100.0, {"bbp": 0.06})]
    out = diff_signals(live, bt)
    assert out["indicator_drift"] == 0
    assert out["matched"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_diff_signals.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create `bot/fidelity/__init__.py`** (empty file)

- [ ] **Step 4: Create `bot/fidelity/checker.py`** with the signal diff implementation

```python
"""Strategy fidelity checker.

Reruns the canonical backtest (engine._run_backtest with return_signals=True)
over the same period the live bot operated and compares 3 layers:
signals, trades, metrics. Persists diffs into fidelity_runs + fidelity_diffs.
"""
from __future__ import annotations

import json
import math
from typing import Any


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

    out = {
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_diff_signals.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot/bot/fidelity/__init__.py hyperliquid-bot/bot/fidelity/checker.py hyperliquid-bot/tests/fidelity/test_diff_signals.py
git commit -m "feat(fidelity): signal-layer diff with price/indicator tolerances"
```

---

## Task 10: `checker.py` — trade-layer diff

**Files:**
- Modify: `hyperliquid-bot/bot/fidelity/checker.py`
- Test: `hyperliquid-bot/tests/fidelity/test_diff_trades.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# hyperliquid-bot/tests/fidelity/test_diff_trades.py
import pytest
from bot.fidelity.checker import diff_trades, PRICE_TOL


def _live_trade(entry_ms, side, entry, exit_, pnl, exit_type=None):
    return {"entry_ts_ms": entry_ms, "side": side,
            "entry_price": entry, "exit_price": exit_,
            "pnl": pnl, "exit_type": exit_type}


def _bt_trade(entry_ms, side, entry, exit_, exit_type, duration=10):
    return {"entry_ts_ms": entry_ms, "side": side,
            "entry_price": entry, "exit_price": exit_,
            "exit_type": exit_type, "duration_candles": duration}


def test_exact_match_is_matched():
    live = [_live_trade(1000, "long", 100.0, 101.0, 1.0, "tp")]
    bt = [_bt_trade(1000, "long", 100.0, 101.0, "tp")]
    out = diff_trades(live, bt, tf_ms=300_000)
    assert out["matched"] == 1


def test_extra_live_trade():
    live = [_live_trade(1000, "long", 100.0, 101.0, 1.0)]
    bt = []
    out = diff_trades(live, bt, tf_ms=300_000)
    assert out["extra_live"] == 1


def test_missed_trade():
    live = []
    bt = [_bt_trade(1000, "long", 100.0, 101.0, "tp")]
    out = diff_trades(live, bt, tf_ms=300_000)
    assert out["missed_trade"] == 1


def test_entry_px_drift():
    live = [_live_trade(1000, "long", 100.20, 101.0, 1.0)]   # 0.2% > tol
    bt = [_bt_trade(1000, "long", 100.0, 101.0, "tp")]
    out = diff_trades(live, bt, tf_ms=300_000)
    assert out["entry_px_drift"] == 1


def test_exit_type_mismatch():
    live = [_live_trade(1000, "long", 100.0, 99.0, -1.0, exit_type="sl")]
    bt = [_bt_trade(1000, "long", 100.0, 101.0, "tp")]
    out = diff_trades(live, bt, tf_ms=300_000)
    assert out["exit_type_mismatch"] == 1


def test_match_within_one_candle_window():
    """Live entered 1 candle after backtest — still considered same trade."""
    live = [_live_trade(1_300_000, "long", 100.0, 101.0, 1.0, "tp")]   # 1 candle later
    bt = [_bt_trade(1_000_000, "long", 100.0, 101.0, "tp")]
    out = diff_trades(live, bt, tf_ms=300_000)
    assert out["matched"] == 1
    assert out["missed_trade"] == 0
    assert out["extra_live"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_diff_trades.py -v`
Expected: FAIL — `ImportError: cannot import name 'diff_trades'`.

- [ ] **Step 3: Append `diff_trades` to `bot/fidelity/checker.py`**

```python
def diff_trades(live_trades: list[dict], bt_trades: list[dict],
                tf_ms: int, price_tol: float = PRICE_TOL) -> dict:
    """Match live trades to backtest trades by entry_ts proximity (±1 candle).

    Each live trade dict must include: entry_ts_ms, side, entry_price,
        exit_price, exit_type (may be None), pnl.
    Each bt trade dict must include: entry_ts_ms, side, entry_price,
        exit_price, exit_type, duration_candles.

    Returns counters + list of diff dicts.
    """
    out = {
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

        if b["exit_price"] and lt.get("exit_price") and float(b["exit_price"]) > 0:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_diff_trades.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/fidelity/checker.py hyperliquid-bot/tests/fidelity/test_diff_trades.py
git commit -m "feat(fidelity): trade-layer diff with proximity matching"
```

---

## Task 11: `checker.py` — score + heuristics + metric diff

**Files:**
- Modify: `hyperliquid-bot/bot/fidelity/checker.py`
- Test: `hyperliquid-bot/tests/fidelity/test_score.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# hyperliquid-bot/tests/fidelity/test_score.py
from bot.fidelity.checker import fidelity_score, attribute_cause


def test_perfect_score_is_one():
    s = fidelity_score(
        signal_counts={"matched": 10, "live_signals": 10, "bt_signals": 10,
                       "price_drift": 0, "indicator_drift": 0},
        trade_outcome_match_rate=1.0,
    )
    assert s == 1.0


def test_zero_match_is_low():
    s = fidelity_score(
        signal_counts={"matched": 0, "live_signals": 10, "bt_signals": 10,
                       "price_drift": 0, "indicator_drift": 0},
        trade_outcome_match_rate=0.0,
    )
    assert s < 0.5


def test_score_components_independent():
    # All matched but 50% have price drift
    s = fidelity_score(
        signal_counts={"matched": 10, "live_signals": 10, "bt_signals": 10,
                       "price_drift": 5, "indicator_drift": 0},
        trade_outcome_match_rate=1.0,
    )
    # match=1.0×0.50  + price=(1-0.5)×0.20  + ind=1.0×0.15  + trades=1.0×0.15  = 0.90
    assert abs(s - 0.90) < 1e-6


def test_attribute_cause_phantom_with_nearby_indicator_drift():
    diff = {"diff_type": "phantom", "ts_ms": 1000}
    siblings = [{"diff_type": "indicator", "ts_ms": 1000, "notes": "indicator=bbp"}]
    cause = attribute_cause(diff, siblings, live_signal=None)
    assert "indicador" in cause.lower()


def test_attribute_cause_missed_with_block_reason():
    diff = {"diff_type": "missed", "ts_ms": 1000}
    live_signal = {"reason": "Funding rate limit exceeded"}
    cause = attribute_cause(diff, [], live_signal=live_signal)
    assert "Funding" in cause


def test_attribute_cause_price_drift_default():
    diff = {"diff_type": "price", "ts_ms": 1000}
    cause = attribute_cause(diff, [], live_signal=None)
    assert "vela" in cause.lower() or "candle" in cause.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_score.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Append to `bot/fidelity/checker.py`**

```python
def fidelity_score(*, signal_counts: dict, trade_outcome_match_rate: float) -> float:
    """Composite 0..1 score (see spec section 5)."""
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
        return "Backtest abriu trade que o live não abriu — provável bloqueio por filtro de risco ou max_positions."

    if t == "extra_live":
        return "Live abriu trade que o backtest não abriu — possível sinal espúrio."

    return "Causa não classificada."


def diff_metrics(live_metrics: dict, bt_metrics: dict,
                 abs_tol: float = 0.05) -> dict:
    """Compare aggregate metrics; flag fields differing by > abs_tol (absolute,
    on the metric's natural scale: WR/PF/ROI fractions, count integers, etc).
    Returns {"diffs": [...]} only — caller decides whether to persist.
    """
    out = {"diffs": []}
    for k in ("win_rate", "profit_factor", "roi", "total_pnl", "max_drawdown",
              "trades_per_day"):
        if k not in live_metrics or k not in bt_metrics:
            continue
        lv, bv = float(live_metrics[k] or 0), float(bt_metrics[k] or 0)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_score.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/fidelity/checker.py hyperliquid-bot/tests/fidelity/test_score.py
git commit -m "feat(fidelity): composite score, cause heuristics, metric diff"
```

---

## Task 12: `checker.py` — `run_check` orchestrator + persistence

**Files:**
- Modify: `hyperliquid-bot/bot/fidelity/checker.py`
- Test: `hyperliquid-bot/tests/fidelity/test_checker_integration.py` (create)

- [ ] **Step 1: Write the failing integration test**

```python
# hyperliquid-bot/tests/fidelity/test_checker_integration.py
import json
import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone, timedelta

from bot import db
from bot.backtest import csv_loader


def _reset_conn():
    db._local.conn = None


@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()

    # Build synthetic 5m CSV
    n = 600
    ts_ms = 1_700_000_000_000 + np.arange(n) * 300_000
    rng = np.random.default_rng(7)
    rets = rng.normal(0, 0.005, n)
    closes = 50_000 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "timestamp": ts_ms,
        "open": closes, "high": closes * 1.002, "low": closes * 0.998,
        "close": closes, "volume": 1.0,
    })
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    df.to_csv(candles_dir / "btc_5m.csv", index=False)
    monkeypatch.setattr(csv_loader, "_CANDLES_DIR", candles_dir)
    monkeypatch.setattr(csv_loader, "_update_csv", lambda *a, **k: None)

    return {"start_ms": int(ts_ms[0]), "end_ms": int(ts_ms[-1])}


def test_run_check_creates_run_and_diffs(seeded_env):
    from bot.fidelity.checker import run_check
    run_id = run_check(strategy="bb_stoch", asset="BTC", days=30, profile_id=1)
    assert isinstance(run_id, int) and run_id > 0
    row = db.get_fidelity_run(run_id)
    assert row is not None
    assert row["strategy"].startswith("bb_stoch")
    assert row["fidelity_score"] is not None
    # No live signals exist in the test DB, so all bt signals should be "missed"
    assert row["missed"] >= 0
    assert row["live_signals"] == 0


def test_run_check_respects_clamp_to_closed_candle(seeded_env):
    """period_end_ms should be clamped to (now - tf_ms) to avoid the open candle."""
    from bot.fidelity.checker import run_check
    run_id = run_check(strategy="bb_stoch", asset="BTC", days=30, profile_id=1)
    row = db.get_fidelity_run(run_id)
    import time
    now_ms = int(time.time() * 1000)
    assert row["period_end_ms"] <= now_ms - 300_000   # 5m floor
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_checker_integration.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_check'`.

- [ ] **Step 3: Append `run_check` to `bot/fidelity/checker.py`**

```python
def _load_live_signals(strategy: str, asset: str, profile_id: int,
                      start_ms: int, end_ms: int) -> list[dict]:
    """Read live signals for the period and normalize to {ts_ms, side, signal_price,
    indicators_json, reason, executed}."""
    from datetime import datetime, timezone
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
        ind = None
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
        "duration_candles": int(t.get("duration", 0)),
    }


_TF_TO_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000}


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

    # Resolve strategy → real instance name (engine helper)
    resolved = bt_engine._resolve_strategy_instance(strategy, asset)

    # Determine timeframe from params
    from bot.strategies.manager import STRATEGY_MAP
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

    # 3. Diffs
    # Read tolerances from config (with defaults)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_checker_integration.py -v`
Expected: PASS.

- [ ] **Step 5: Run all fidelity tests for regression**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot/bot/fidelity/checker.py hyperliquid-bot/tests/fidelity/test_checker_integration.py
git commit -m "feat(fidelity): run_check orchestrator with 3-layer diff + persistence"
```

---

## Task 13: Async job wrapper

**Files:**
- Create: `hyperliquid-bot/bot/fidelity/job.py`
- Test: `hyperliquid-bot/tests/fidelity/test_job.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# hyperliquid-bot/tests/fidelity/test_job.py
import time
from bot.fidelity import job


def test_start_job_returns_uuid_string():
    jid = job.start_check_job(lambda: 42)
    assert isinstance(jid, str) and len(jid) > 0


def test_job_completes_with_result():
    jid = job.start_check_job(lambda: 1234)
    for _ in range(50):
        rec = job.get_job(jid)
        if rec and rec["status"] == "done":
            break
        time.sleep(0.05)
    rec = job.get_job(jid)
    assert rec["status"] == "done"
    assert rec["result"] == 1234


def test_job_records_error():
    def boom(): raise RuntimeError("nope")
    jid = job.start_check_job(boom)
    for _ in range(50):
        rec = job.get_job(jid)
        if rec and rec["status"] == "error":
            break
        time.sleep(0.05)
    rec = job.get_job(jid)
    assert rec["status"] == "error"
    assert "nope" in rec["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_job.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `bot/fidelity/job.py`**

```python
"""Threaded job registry for fidelity checker (mirrors engine.py pattern)."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Any

_jobs: dict = {}
_jobs_lock = threading.Lock()


def start_check_job(fn: Callable[[], Any], description: str = "") -> str:
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "description": description,
            "started_at": time.time(),
            "result": None,
            "error": None,
            "elapsed_s": None,
        }

    def _runner():
        started = time.time()
        try:
            result = fn()
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = result
                _jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)
                _jobs[job_id]["elapsed_s"] = round(time.time() - started, 3)

    threading.Thread(target=_runner, daemon=True, name=f"fidelity-{job_id[:8]}").start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return dict(_jobs[job_id]) if job_id in _jobs else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/fidelity/test_job.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/fidelity/job.py hyperliquid-bot/tests/fidelity/test_job.py
git commit -m "feat(fidelity): async job registry wrapper"
```

---

## Task 14: Flask routes + API endpoints

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py` (add `/fidelity` page + 5 API endpoints + exclude page from `check_configured` redirect)
- Test: `hyperliquid-bot/tests/dashboard/test_fidelity_endpoints.py` (create)

- [ ] **Step 1: Find the `check_configured` exclusion list and the `STRATEGY_MAP` import pattern**

Run: `grep -n "check_configured\|scanner_v2_page\|STRATEGY_MAP" hyperliquid-bot/dashboard/app.py | head -20`

Note where pages like `scanner_v2_page` are excluded from the redirect; the same exclusion must apply to `fidelity_page`.

- [ ] **Step 2: Write the failing test**

```python
# hyperliquid-bot/tests/dashboard/test_fidelity_endpoints.py
import json
import pytest

from bot import db
from dashboard.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_fidelity_page_renders(client):
    r = client.get("/fidelity")
    assert r.status_code == 200
    assert b"fidelity" in r.data.lower() or b"fidelidade" in r.data.lower()


def test_api_runs_list_empty(client):
    r = client.get("/api/fidelity/runs")
    assert r.status_code == 200
    assert r.get_json() == {"runs": []}


def test_api_strategies_returns_list(client):
    r = client.get("/api/fidelity/strategies")
    assert r.status_code == 200
    body = r.get_json()
    assert "strategies" in body
    assert isinstance(body["strategies"], list)


def test_api_run_404_for_missing_id(client):
    r = client.get("/api/fidelity/runs/99999")
    assert r.status_code == 404
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd hyperliquid-bot && python -m pytest tests/dashboard/test_fidelity_endpoints.py -v`
Expected: FAIL — `404` on `/fidelity` page.

- [ ] **Step 4: Add the route and endpoints to `dashboard/app.py`**

Inside `create_app()` (alongside the other `@app.route` decorators), add:

```python
    @app.route("/fidelity")
    def fidelity_page():
        return render_template("fidelity.html", page="fidelity")

    @app.route("/api/fidelity/strategies")
    def api_fidelity_strategies():
        """List strategy instances with at least 1 closed trade in current profile."""
        from bot.strategies.manager import STRATEGY_MAP
        rows = db.get_conn().execute(
            """
            SELECT strategy, COUNT(*) as n FROM trades
            WHERE profile_id = ? AND status = 'closed'
            GROUP BY strategy ORDER BY strategy
            """,
            (g.profile_id,),
        ).fetchall()
        out = []
        for r in rows:
            name = r["strategy"]
            if name not in STRATEGY_MAP:
                continue
            strat = STRATEGY_MAP[name]
            assets = strat.DEFAULT_PARAMS.get("assets") or []
            out.append({
                "name": name,
                "display": strat.DISPLAY_NAME or name,
                "asset": assets[0] if assets else None,
                "timeframe": strat.DEFAULT_PARAMS.get("timeframe", "5m"),
                "trades": r["n"],
            })
        return jsonify({"strategies": out})

    @app.route("/api/fidelity/run", methods=["POST"])
    def api_fidelity_run():
        from bot.fidelity.checker import run_check
        from bot.fidelity.job import start_check_job
        data = request.get_json() or {}
        strategy = data.get("strategy")
        if not strategy:
            return jsonify({"error": "strategy required"}), 400
        days = int(data.get("days", 7))
        # Resolve asset from strategy default
        from bot.strategies.manager import STRATEGY_MAP
        if strategy not in STRATEGY_MAP:
            return jsonify({"error": "unknown strategy"}), 404
        assets = STRATEGY_MAP[strategy].DEFAULT_PARAMS.get("assets") or []
        if not assets:
            return jsonify({"error": "strategy has no asset configured"}), 400
        asset = assets[0]
        profile_id = g.profile_id
        jid = start_check_job(
            lambda: run_check(strategy=strategy, asset=asset, days=days,
                              profile_id=profile_id),
            description=f"{strategy} {days}d",
        )
        return jsonify({"job_id": jid})

    @app.route("/api/fidelity/status/<job_id>")
    def api_fidelity_status(job_id):
        from bot.fidelity.job import get_job
        rec = get_job(job_id)
        if not rec:
            return jsonify({"error": "job not found"}), 404
        return jsonify(rec)

    @app.route("/api/fidelity/runs")
    def api_fidelity_runs():
        limit = int(request.args.get("limit", 20))
        runs = db.list_fidelity_runs(limit=limit, profile_id=g.profile_id)
        return jsonify({"runs": runs})

    @app.route("/api/fidelity/runs/<int:run_id>")
    def api_fidelity_run_detail(run_id):
        row = db.get_fidelity_run(run_id)
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(row)

    @app.route("/api/fidelity/runs/<int:run_id>/diffs")
    def api_fidelity_run_diffs(run_id):
        layer = request.args.get("layer")
        diff_type = request.args.get("type")
        diffs = db.get_fidelity_diffs(run_id, layer=layer, diff_type=diff_type)
        return jsonify({"diffs": diffs})
```

Then find the `check_configured` function (where pages like `scanner_v2_page` are excluded) and add `"fidelity_page"` to that exclusion list. Run: `grep -n "scanner_v2_page" hyperliquid-bot/dashboard/app.py` to locate it.

- [ ] **Step 5: Create the test directory & minimal template stub**

`hyperliquid-bot/tests/dashboard/__init__.py` (empty if not present).

Create a minimal `hyperliquid-bot/dashboard/templates/fidelity.html` to make `/fidelity` return 200 (full UI in Task 15):

```html
{% extends "base.html" %}
{% block content %}
<div class="fidelity-page">
  <h1>Fidelidade da Estratégia</h1>
  <div id="fidelity-app">Loading...</div>
</div>
{% endblock %}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd hyperliquid-bot && python -m pytest tests/dashboard/test_fidelity_endpoints.py -v`
Expected: PASS (all 4 tests).

- [ ] **Step 7: Commit**

```bash
git add hyperliquid-bot/dashboard/app.py hyperliquid-bot/dashboard/templates/fidelity.html hyperliquid-bot/tests/dashboard/__init__.py hyperliquid-bot/tests/dashboard/test_fidelity_endpoints.py
git commit -m "feat(dashboard): /fidelity route and API endpoints with stub template"
```

---

## Task 15: Frontend UI (HTML + JS + CSS)

**Files:**
- Modify: `hyperliquid-bot/dashboard/templates/fidelity.html`
- Modify: `hyperliquid-bot/dashboard/templates/base.html` (add sidebar link)
- Create: `hyperliquid-bot/dashboard/static/js/fidelity.js`
- Create: `hyperliquid-bot/dashboard/static/css/fidelity.css`

**No new tests for this task** — the page is interactive; existing endpoint tests cover the backend. Visual verification is the acceptance criterion.

- [ ] **Step 1: Add sidebar link in `base.html`**

Run: `grep -n "scanner_v2\|strategies\|ativos" hyperliquid-bot/dashboard/templates/base.html | head -10`

Find the nav list and add an entry near the analise/scanner_v2 links:

```html
<li class="nav-item">
  <a href="/fidelity" class="nav-link {% if page == 'fidelity' %}active{% endif %}">
    <span class="nav-icon">📏</span>
    <span class="nav-label">Fidelidade</span>
  </a>
</li>
```

- [ ] **Step 2: Replace `dashboard/templates/fidelity.html` with the full page**

```html
{% extends "base.html" %}
{% block content %}
<link rel="stylesheet" href="{{ url_for('static', filename='css/fidelity.css') }}">

<div class="fidelity-page">
  <header class="fidelity-header">
    <h1>Fidelidade da Estratégia</h1>
    <p class="muted">Compara o live vs o backtest canônico no mesmo período.</p>
  </header>

  <section class="fidelity-form">
    <label>Estratégia
      <select id="fid-strategy"></select>
    </label>
    <label>Período
      <select id="fid-days">
        <option value="1">D-1</option>
        <option value="7" selected>7 dias</option>
        <option value="30">30 dias</option>
      </select>
    </label>
    <button id="fid-run" class="btn btn-primary">▶ Verificar fidelidade</button>
    <div id="fid-progress" class="fid-progress hidden"></div>
  </section>

  <section class="fidelity-history">
    <label>Histórico
      <select id="fid-history"><option value="">— últimas verificações —</option></select>
    </label>
  </section>

  <section id="fid-cards" class="fidelity-cards"></section>

  <section id="fid-drilldown" class="fidelity-drilldown hidden">
    <div class="fid-tabs">
      <button class="fid-tab active" data-layer="signal">Sinais (<span data-count="signal">0</span>)</button>
      <button class="fid-tab" data-layer="trade">Trades (<span data-count="trade">0</span>)</button>
      <button class="fid-tab" data-layer="metric">Métricas (<span data-count="metric">0</span>)</button>
    </div>
    <div class="fid-filters" id="fid-filters"></div>
    <table class="fid-diff-table">
      <thead><tr id="fid-diff-head"></tr></thead>
      <tbody id="fid-diff-body"></tbody>
    </table>
  </section>

  <div id="fid-modal" class="fid-modal hidden">
    <div class="fid-modal-card">
      <button class="fid-modal-close">&times;</button>
      <h2 id="fid-modal-title">Detalhes</h2>
      <div class="fid-modal-grid">
        <div><h3>Live</h3><pre id="fid-modal-live"></pre></div>
        <div><h3>Backtest</h3><pre id="fid-modal-bt"></pre></div>
      </div>
      <p id="fid-modal-cause" class="fid-cause"></p>
    </div>
  </div>
</div>

<script src="{{ url_for('static', filename='js/fidelity.js') }}"></script>
{% endblock %}
```

- [ ] **Step 3: Create `dashboard/static/js/fidelity.js`**

```javascript
(() => {
  const $ = (s, ctx = document) => ctx.querySelector(s);
  const $$ = (s, ctx = document) => Array.from(ctx.querySelectorAll(s));

  let _currentRun = null;
  let _currentDiffs = [];
  let _currentLayer = "signal";

  async function loadStrategies() {
    const r = await fetch("/api/fidelity/strategies");
    const { strategies } = await r.json();
    const sel = $("#fid-strategy");
    sel.innerHTML = strategies.map(s =>
      `<option value="${s.name}">${s.display} — ${s.asset} (${s.trades} trades)</option>`
    ).join("");
  }

  async function loadHistory() {
    const r = await fetch("/api/fidelity/runs?limit=20");
    const { runs } = await r.json();
    const sel = $("#fid-history");
    sel.innerHTML = `<option value="">— últimas verificações —</option>` +
      runs.map(rn => {
        const dt = new Date(rn.created_at).toLocaleString("pt-BR");
        return `<option value="${rn.id}">${dt} · ${rn.strategy} · ★${rn.fidelity_score?.toFixed(2)}</option>`;
      }).join("");
  }

  function bandColor(score) {
    if (score >= 0.9) return "fid-green";
    if (score >= 0.7) return "fid-yellow";
    return "fid-red";
  }
  function bandLabel(score) {
    if (score >= 0.9) return "Excelente";
    if (score >= 0.7) return "Bom";
    return "Investigar";
  }

  function renderCard(run) {
    const lm = run.live_metrics_json ? JSON.parse(run.live_metrics_json) : {};
    const bm = run.bt_metrics_json ? JSON.parse(run.bt_metrics_json) : {};
    const total = Math.max(run.live_signals, run.bt_signals, 1);
    const pricePct = run.matched > 0 ? (1 - run.price_drift / run.matched) * 100 : 100;
    const indPct = run.matched > 0 ? (1 - run.indicator_drift / run.matched) * 100 : 100;
    const html = `
      <div class="fid-card ${bandColor(run.fidelity_score)}" data-run="${run.id}">
        <header>
          <span class="fid-name">${run.strategy}</span>
          <span class="fid-score">★ ${run.fidelity_score?.toFixed(2)} <em>(${bandLabel(run.fidelity_score)})</em></span>
        </header>
        <hr>
        <div class="fid-row">Sinais &nbsp; <b>${run.matched}/${total}</b> matched · ${run.phantom} phantom · ${run.missed} missed · ${run.side_mismatch} lado</div>
        <div class="fid-row">Preço &nbsp; <b>${pricePct.toFixed(0)}%</b> dentro tol (${run.price_drift} drift)</div>
        <div class="fid-row">Indicadores &nbsp; <b>${indPct.toFixed(0)}%</b> dentro tol (${run.indicator_drift} drift)</div>
        <hr>
        <div class="fid-row">Live &nbsp; WR ${(lm.win_rate*100||0).toFixed(0)}% · PF ${(lm.profit_factor||0).toFixed(2)} · ROI ${((lm.roi||0)*100).toFixed(2)}%</div>
        <div class="fid-row">BT &nbsp;&nbsp; WR ${(bm.win_rate*100||0).toFixed(0)}% · PF ${(bm.profit_factor||0).toFixed(2)} · ROI ${((bm.roi||0)*100).toFixed(2)}%</div>
      </div>`;
    return html;
  }

  async function selectRun(runId) {
    _currentRun = runId;
    const r = await fetch(`/api/fidelity/runs/${runId}`);
    const run = await r.json();
    $("#fid-cards").innerHTML = renderCard(run);
    $$("#fid-cards .fid-card").forEach(c => c.addEventListener("click", () => loadDrilldown(runId)));
    loadDrilldown(runId);
  }

  async function loadDrilldown(runId) {
    const r = await fetch(`/api/fidelity/runs/${runId}/diffs`);
    const { diffs } = await r.json();
    _currentDiffs = diffs;
    $("#fid-drilldown").classList.remove("hidden");
    const counts = { signal: 0, trade: 0, metric: 0 };
    diffs.forEach(d => { counts[d.layer] = (counts[d.layer] || 0) + 1; });
    Object.entries(counts).forEach(([k, v]) => {
      const span = $(`.fid-tab [data-count="${k}"]`);
      if (span) span.textContent = v;
    });
    renderTab(_currentLayer);
  }

  function renderTab(layer) {
    _currentLayer = layer;
    $$(".fid-tab").forEach(t => t.classList.toggle("active", t.dataset.layer === layer));
    const rows = _currentDiffs.filter(d => d.layer === layer);
    // Filter chips by diff_type
    const types = Array.from(new Set(rows.map(r => r.diff_type)));
    const fbox = $("#fid-filters");
    fbox.innerHTML = types.map(t => `<button class="fid-chip" data-type="${t}">${t} (${rows.filter(r => r.diff_type === t).length})</button>`).join("");

    const head = $("#fid-diff-head");
    head.innerHTML = `<th>ts</th><th>tipo</th><th>side</th><th>Δ%</th><th>causa</th>`;
    renderRows(rows);

    $$("#fid-filters .fid-chip").forEach(c => c.addEventListener("click", () => {
      const t = c.dataset.type;
      const filtered = t ? rows.filter(r => r.diff_type === t) : rows;
      renderRows(filtered);
    }));
  }

  function renderRows(rows) {
    const body = $("#fid-diff-body");
    body.innerHTML = rows.map(r => {
      const dt = r.ts_ms ? new Date(Number(r.ts_ms)).toLocaleString("pt-BR") : "—";
      const delta = r.delta_pct != null ? (r.delta_pct * 100).toFixed(3) + "%" : "—";
      return `<tr data-id="${r.id}"><td>${dt}</td><td>${r.diff_type}</td><td>${r.side || ""}</td><td>${delta}</td><td>${r.notes || ""}</td></tr>`;
    }).join("");
    $$("#fid-diff-body tr").forEach(tr => tr.addEventListener("click", () => {
      const id = Number(tr.dataset.id);
      const diff = _currentDiffs.find(d => d.id === id);
      openModal(diff);
    }));
  }

  function openModal(diff) {
    $("#fid-modal").classList.remove("hidden");
    $("#fid-modal-title").textContent = `${diff.layer} · ${diff.diff_type}`;
    $("#fid-modal-live").textContent = diff.live_json ? JSON.stringify(JSON.parse(diff.live_json), null, 2) : "—";
    $("#fid-modal-bt").textContent = diff.bt_json ? JSON.parse(diff.bt_json) && JSON.stringify(JSON.parse(diff.bt_json), null, 2) : "—";
    $("#fid-modal-cause").textContent = "Provável causa: " + (diff.notes || "—");
  }

  $(".fid-modal-close").addEventListener("click", () => $("#fid-modal").classList.add("hidden"));
  $$(".fid-tab").forEach(t => t.addEventListener("click", () => renderTab(t.dataset.layer)));

  $("#fid-run").addEventListener("click", async () => {
    const strategy = $("#fid-strategy").value;
    const days = Number($("#fid-days").value);
    $("#fid-progress").classList.remove("hidden");
    $("#fid-progress").textContent = "Iniciando...";
    const r = await fetch("/api/fidelity/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy, days }),
    });
    const { job_id, error } = await r.json();
    if (error) { $("#fid-progress").textContent = "Erro: " + error; return; }
    poll(job_id);
  });

  async function poll(job_id) {
    const r = await fetch(`/api/fidelity/status/${job_id}`);
    const rec = await r.json();
    $("#fid-progress").textContent = `${rec.status}${rec.elapsed_s ? ` (${rec.elapsed_s}s)` : ""}`;
    if (rec.status === "done") {
      $("#fid-progress").classList.add("hidden");
      await loadHistory();
      await selectRun(rec.result);
      return;
    }
    if (rec.status === "error") {
      $("#fid-progress").textContent = "Erro: " + rec.error;
      return;
    }
    setTimeout(() => poll(job_id), 1500);
  }

  $("#fid-history").addEventListener("change", e => {
    if (e.target.value) selectRun(Number(e.target.value));
  });

  loadStrategies();
  loadHistory();
})();
```

- [ ] **Step 4: Create `dashboard/static/css/fidelity.css`**

```css
.fidelity-page { padding: 1.5rem; color: #e2e8f0; }
.fidelity-header h1 { margin-bottom: 0.25rem; }
.fidelity-header .muted { color: #94a3b8; }
.fidelity-form, .fidelity-history { display: flex; gap: 1rem; align-items: end; margin: 1rem 0; }
.fidelity-form label, .fidelity-history label { display: flex; flex-direction: column; font-size: 0.85rem; color: #94a3b8; gap: 0.25rem; }
.fidelity-form select, .fidelity-history select { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; padding: 0.4rem 0.6rem; border-radius: 4px; }
.btn-primary { background: #2563eb; color: #fff; padding: 0.5rem 1rem; border: none; border-radius: 4px; cursor: pointer; }
.btn-primary:hover { background: #1d4ed8; }
.fid-progress { padding: 0.5rem; background: #1e293b; border-radius: 4px; color: #cbd5e1; }
.hidden { display: none !important; }

.fidelity-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 1rem; margin-top: 1.5rem; }
.fid-card { background: #1e293b; border-radius: 8px; padding: 1rem; border-left: 4px solid #475569; cursor: pointer; }
.fid-card.fid-green { border-left-color: #10b981; }
.fid-card.fid-yellow { border-left-color: #f59e0b; }
.fid-card.fid-red { border-left-color: #ef4444; }
.fid-card header { display: flex; justify-content: space-between; font-weight: 600; }
.fid-card hr { border: none; border-top: 1px solid #334155; margin: 0.5rem 0; }
.fid-card .fid-row { font-size: 0.88rem; color: #cbd5e1; margin: 0.25rem 0; }

.fidelity-drilldown { margin-top: 2rem; background: #1e293b; padding: 1rem; border-radius: 8px; }
.fid-tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
.fid-tab { background: #0f172a; color: #94a3b8; border: 1px solid #334155; padding: 0.4rem 0.9rem; border-radius: 4px; cursor: pointer; }
.fid-tab.active { background: #2563eb; color: #fff; border-color: #2563eb; }
.fid-filters { display: flex; gap: 0.4rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
.fid-chip { background: #0f172a; color: #cbd5e1; border: 1px solid #334155; padding: 0.25rem 0.6rem; border-radius: 999px; font-size: 0.8rem; cursor: pointer; }
.fid-chip:hover { background: #334155; }

.fid-diff-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
.fid-diff-table th, .fid-diff-table td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #334155; text-align: left; }
.fid-diff-table tbody tr:hover { background: #334155; cursor: pointer; }

.fid-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 100; }
.fid-modal-card { background: #1e293b; padding: 1.5rem; border-radius: 8px; max-width: 900px; width: 90%; max-height: 80vh; overflow: auto; position: relative; }
.fid-modal-close { position: absolute; top: 0.5rem; right: 0.8rem; background: none; color: #cbd5e1; border: none; font-size: 1.5rem; cursor: pointer; }
.fid-modal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 0.5rem; }
.fid-modal-grid pre { background: #0f172a; padding: 0.75rem; border-radius: 4px; max-height: 400px; overflow: auto; font-size: 0.8rem; }
.fid-cause { background: #0f172a; padding: 0.6rem 0.8rem; border-radius: 4px; color: #fbbf24; margin-top: 1rem; }
```

- [ ] **Step 5: Smoke-test manually**

Run: `cd hyperliquid-bot && python run.py` (in another terminal). Open `http://localhost:8080/fidelity`. Verify:
1. Page loads, sidebar link is highlighted.
2. Strategy dropdown populates from `/api/fidelity/strategies`.
3. History dropdown populates.
4. Clicking "Verificar" triggers a job; progress text updates; on completion, a card renders.
5. Click the card → drill-down appears; tabs switch; clicking a row opens the modal lado-a-lado.

If the strategy dropdown is empty, the DB has no closed trades — use an existing DB or seed one.

- [ ] **Step 6: Re-run the full test suite**

Run: `cd hyperliquid-bot && python -m pytest tests/ -v`
Expected: PASS (no regressions across fidelity, backtest, strategies, dashboard).

- [ ] **Step 7: Commit**

```bash
git add hyperliquid-bot/dashboard/templates/fidelity.html hyperliquid-bot/dashboard/templates/base.html hyperliquid-bot/dashboard/static/js/fidelity.js hyperliquid-bot/dashboard/static/css/fidelity.css
git commit -m "feat(dashboard): fidelity page UI — cards, tabs, drill-down modal"
```

---

## Task 16: Update repo CLAUDE.md

**Files:**
- Modify: `hyperliquid-bot/CLAUDE.md`

- [ ] **Step 1: Append a section under the dashboard Telas list**

Search for the dashboard "Telas" enumeration in `CLAUDE.md` (the section that lists Overview, Trades, Sinais, etc.) and add an item:

```
9. **Fidelidade** (`/fidelity`): Compara live vs backtest canônico no mesmo período. Sob demanda, escolhe estratégia + período (D-1/7d/30d) e roda 3 camadas de diff: sinais (phantom/missed/side/price_drift/indicator_drift), trades (entry_px/exit_type/missed), métricas (WR/PF/ROI/MaxDD). Cards com score 0..1 (verde≥0.9 / amarelo≥0.7 / vermelho<0.7) e drill-down até o candle exato com causa provável atribuída por heurística (vela aberta, filtro de risco, warmup diferente, etc.). Tabelas `fidelity_runs` + `fidelity_diffs`. Coluna `signals.indicators_json` (M9) guarda snapshot completo dos indicadores por sinal pra comparação exata. `engine._run_backtest(..., return_signals=True)` devolve sinais + snapshot per-família. Endpoints `/api/fidelity/{strategies,run,status,runs,runs/<id>,runs/<id>/diffs}`. Tolerâncias em `config.fidelity.{price_tol_pct,indicator_tol_pct}` (defaults 0.0005 e 0.01). `fidelity_page` excluída de `check_configured`.
```

Also add an entry to the migrations list near M7/M8: `M9 — signals.indicators_json (snapshot JSON dos indicadores no momento do sinal, para fidelity checker)` and `M10 — fidelity_runs/fidelity_diffs tables (header + diffs por candle/trade/metric, com cause atribuída por heurística)`.

- [ ] **Step 2: Commit**

```bash
git add hyperliquid-bot/CLAUDE.md
git commit -m "docs(claude-md): document fidelity checker (M9, M10, /fidelity page)"
```

---

## Self-review checklist (for the implementing engineer)

After all 16 tasks land:

1. [ ] `pytest tests/` is green
2. [ ] `/fidelity` page renders, runs a check end-to-end, and shows correct counts
3. [ ] An existing `bb_stoch_btc_5m` (or any deployed strategy) returns `fidelity_score > 0` when the live DB has signals/trades for the period
4. [ ] No regression in existing `tests/backtest/` and `tests/strategies/`
5. [ ] CLAUDE.md describes the new page, migrations, and column

---

## Out of scope (v1) — explicit reminders

- No params/enabled-history persistence (uses current params; document caveat in UI if needed later)
- No continuous/cron mode
- No CSV/JSON export of diffs
- No multi-strategy aggregate card
