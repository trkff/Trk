# Multi-Profile Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run N Lighter accounts in parallel from one RazorHL process, each with its own credentials, strategies, trades, and bot status, sharing a single candle manager and CSV cache.

**Architecture:** New `profiles` table; `profile_id` column on trades/signals/logs; config keys per-profile (`profile.<id>.<key>`); idempotent migration M8 packs existing data into a "Default" profile; `main.py` keeps a dict of bot threads/clients keyed by `profile_id`; dashboard gains a profile dropdown in the header; UI context follows `session["active_profile_id"]`.

**Tech Stack:** Python 3.10+, SQLite (WAL), Flask + Flask-SocketIO, threading.

**Spec:** [`docs/superpowers/specs/2026-05-27-multi-profile-design.md`](../specs/2026-05-27-multi-profile-design.md)

---

## Phase 1 — DB + Migration M8

Goal: schema is ready, existing data lives inside a "Default" profile (id=1), bot still runs hardcoded against id=1. End of Phase 1: bot trades exactly like today, but every row is tagged.

### Task 1.1 — Add `profiles` table

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (`init_db` body, after the existing `executescript` block)
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py` (new)

- [ ] **Step 1: Write the failing test**

Create `hyperliquid-bot/tests/test_multi_profile_db.py`:

```python
import pytest
from bot import db

def test_profiles_table_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(profiles)").fetchall()]
    assert set(cols) >= {
        "id", "name", "exchange",
        "lighter_account_index", "lighter_api_key_private", "lighter_api_key_index",
        "hyperliquid_address", "hyperliquid_secret",
        "created_at", "updated_at",
    }
```

- [ ] **Step 2: Run test, expect failure**

Run: `cd hyperliquid-bot && pytest tests/test_multi_profile_db.py::test_profiles_table_exists -v`
Expected: FAIL (`no such table: profiles`).

- [ ] **Step 3: Add table to `init_db`**

In `hyperliquid-bot/bot/db.py`, inside the existing `conn.executescript("""...""")` block in `init_db()`, append before the index statements:

```sql
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'lighter',
    lighter_account_index TEXT,
    lighter_api_key_private TEXT,
    lighter_api_key_index TEXT,
    hyperliquid_address TEXT,
    hyperliquid_secret TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

- [ ] **Step 4: Run test, expect pass**

Run: `pytest tests/test_multi_profile_db.py::test_profiles_table_exists -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/test_multi_profile_db.py
git commit -m "feat(db): add profiles table"
```

### Task 1.2 — Add `profile_id` column to trades/signals/logs

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (`migrate_db`)
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_multi_profile_db.py`:

```python
def test_profile_id_columns_added(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    for table in ("trades", "signals", "logs"):
        cols = [r["name"] for r in db.get_conn().execute(f"PRAGMA table_info({table})").fetchall()]
        assert "profile_id" in cols, f"{table} missing profile_id"
```

- [ ] **Step 2: Run test, expect failure**

Run: `pytest tests/test_multi_profile_db.py::test_profile_id_columns_added -v`
Expected: FAIL.

- [ ] **Step 3: Add ALTER statements to `migrate_db`**

In `bot/db.py`, after the M7 call, add a new block:

```python
# M8a — add profile_id columns (run before the rest of M8)
for table in ("trades", "signals", "logs"):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if "profile_id" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN profile_id INTEGER DEFAULT 1")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_profile ON {table}(profile_id)")
        conn.commit()
```

- [ ] **Step 4: Run test, expect pass**

Run: `pytest tests/test_multi_profile_db.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/test_multi_profile_db.py
git commit -m "feat(db): add profile_id columns to trades/signals/logs"
```

### Task 1.3 — Migration M8b: create Default profile and namespace existing config keys

**Files:**
- Modify: `hyperliquid-bot/bot/db.py`
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_multi_profile_db.py`:

```python
import json, time

def test_m8_creates_default_profile_and_namespaces_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    conn = db.get_conn()
    # Seed legacy config + insert a trade
    conn.executemany("INSERT INTO config (key, value) VALUES (?, ?)", [
        ("strategy.bb_stoch_btc_5m.enabled", "true"),
        ("strategy.bb_stoch_btc_5m.params", json.dumps({"bb_period": 20})),
        ("bot_status", "running"),
        ("assets", json.dumps(["BTC", "ETH"])),
        ("lighter.client_order_counter", "42"),
        ("account_address", "0xabc"),
        ("secret_key", "deadbeef"),
        ("selected_exchange", "lighter"),
    ])
    conn.commit()
    # Force re-run by clearing the marker
    conn.execute("DELETE FROM config WHERE key = '_migration_multi_profile'")
    conn.commit()
    db.migrate_db()

    # Default profile created
    row = conn.execute("SELECT * FROM profiles WHERE id = 1").fetchone()
    assert row is not None
    assert row["name"] == "Default"
    assert row["exchange"] == "lighter"

    # Keys namespaced
    assert db.get_config("profile.1.strategy.bb_stoch_btc_5m.enabled") == "true"
    assert db.get_config("profile.1.bot_status") == "running"
    assert db.get_config("profile.1.lighter.client_order_counter") == "42"
    # Old keys deleted
    assert db.get_config("strategy.bb_stoch_btc_5m.enabled") is None
    assert db.get_config("bot_status") is None
    # Globals preserved
    assert db.get_config("selected_exchange") == "lighter"
    # Marker set
    assert db.get_config("_migration_multi_profile") == "done"

def test_m8_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    db.migrate_db()  # second run must be no-op
    rows = db.get_conn().execute("SELECT COUNT(*) AS n FROM profiles").fetchone()
    assert rows["n"] >= 1
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_multi_profile_db.py -v`
Expected: FAIL on `test_m8_creates_default_profile_and_namespaces_keys`.

- [ ] **Step 3: Implement M8b**

In `bot/db.py`, add a constant near the top of the file (after `_MR_LEGACY_KEYS`):

```python
_M8_NAMESPACED_PREFIXES = (
    "strategy.",
    "assets",
    "risk.",
    "sizing.",
    "lighter.client_order_counter",
)
_M8_GLOBAL_KEYS_TO_KEEP = (
    "selected_exchange", "use_lighter_ws_candles",
    "_migration_strategy_names_5m", "_migration_dynamic_strategy_5m",
    "_migration_multi_profile",
)
_M8_BOT_STATUS_KEYS = ("bot_status",)
```

Add a new function near M7:

```python
def _migrate_to_multi_profile(conn):
    """M8 — seed Default profile (id=1) and namespace per-profile config keys."""
    marker = conn.execute(
        "SELECT value FROM config WHERE key = '_migration_multi_profile'"
    ).fetchone()
    if marker and marker["value"] == "done":
        return

    now = int(time.time() * 1000)

    # 1. Create Default profile from legacy global credentials, if not present
    existing = conn.execute("SELECT id FROM profiles WHERE id = 1").fetchone()
    if existing is None:
        cred_keys = (
            "account_address", "secret_key",
            "lighter_account_index", "lighter_api_key_private", "lighter_api_key_index",
        )
        creds = {}
        for k in cred_keys:
            row = conn.execute("SELECT value FROM config WHERE key = ?", (k,)).fetchone()
            creds[k] = row["value"] if row else None
        exch_row = conn.execute(
            "SELECT value FROM config WHERE key = 'selected_exchange'"
        ).fetchone()
        exchange = exch_row["value"] if exch_row else "lighter"
        conn.execute(
            """INSERT INTO profiles
               (id, name, exchange, lighter_account_index, lighter_api_key_private,
                lighter_api_key_index, hyperliquid_address, hyperliquid_secret,
                created_at, updated_at)
               VALUES (1, 'Default', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exchange,
                creds.get("lighter_account_index"),
                creds.get("lighter_api_key_private"),
                creds.get("lighter_api_key_index"),
                creds.get("account_address"),
                creds.get("secret_key"),
                now, now,
            ),
        )

    # 2. Backfill profile_id=1 on existing rows (DEFAULT only applies to NEW rows)
    for table in ("trades", "signals", "logs"):
        conn.execute(f"UPDATE {table} SET profile_id = 1 WHERE profile_id IS NULL")

    # 3. Namespace per-profile config keys → profile.1.<key>
    keys_to_move = []
    for row in conn.execute("SELECT key, value FROM config").fetchall():
        k = row["key"]
        if k in _M8_GLOBAL_KEYS_TO_KEEP:
            continue
        if k.startswith("last_ts."):
            continue
        if k.startswith("profile."):
            continue
        is_namespaced = (
            any(k.startswith(p) for p in _M8_NAMESPACED_PREFIXES)
            or k in _M8_BOT_STATUS_KEYS
            or k.startswith("risk.")
            or k.startswith("sizing.")
            or k == "assets"
        )
        if is_namespaced:
            keys_to_move.append((k, row["value"]))
    for k, v in keys_to_move:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"profile.1.{k}", v),
        )
        conn.execute("DELETE FROM config WHERE key = ?", (k,))

    # 4. Set marker
    conn.execute(
        "INSERT INTO config (key, value) VALUES ('_migration_multi_profile', 'done') "
        "ON CONFLICT(key) DO UPDATE SET value = 'done'"
    )
    conn.commit()
```

In `migrate_db()`, after the M7 call, add:

```python
# M8 — multi-profile support
_migrate_to_multi_profile(conn)
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_multi_profile_db.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/test_multi_profile_db.py
git commit -m "feat(db): M8 migrate existing config and trades into Default profile"
```

### Task 1.4 — Smoke test: bot still boots with hardcoded profile_id=1

**Files:**
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Add regression test**

Append to `tests/test_multi_profile_db.py`:

```python
def test_legacy_strategy_keys_readable_under_profile_1(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    # Seed legacy and migrate
    db.get_conn().execute(
        "INSERT INTO config (key, value) VALUES (?, ?)",
        ("strategy.bb_stoch_btc_5m.params", json.dumps({"bb_period": 20})),
    )
    db.get_conn().commit()
    db.get_conn().execute("DELETE FROM config WHERE key = '_migration_multi_profile'")
    db.get_conn().commit()
    db.migrate_db()
    # Reading via the new namespace works
    assert json.loads(db.get_config("profile.1.strategy.bb_stoch_btc_5m.params"))["bb_period"] == 20
```

- [ ] **Step 2: Run and commit**

Run: `pytest tests/test_multi_profile_db.py -v`
Expected: all PASS.

```bash
git add hyperliquid-bot/tests/test_multi_profile_db.py
git commit -m "test(db): assert legacy strategy params reachable via profile.1 namespace"
```

---

## Phase 2 — Access layer refactor

Goal: every read/write that is per-profile takes an explicit `profile_id`. All call sites pass `1` for now. Bot behaviour unchanged.

### Task 2.1 — Profile CRUD helpers in `db.py`

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (append helpers near the end)
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Add failing tests**

Append:

```python
def test_list_create_update_delete_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    db.migrate_db()
    # Default exists
    profiles = db.list_profiles()
    assert any(p["id"] == 1 and p["name"] == "Default" for p in profiles)
    # Create
    pid = db.create_profile(
        name="Conta 2", exchange="lighter",
        credentials={"lighter_account_index": "999"},
    )
    assert pid > 1
    assert db.get_profile(pid)["name"] == "Conta 2"
    # Update (rename + new creds)
    db.update_profile(pid, name="Hedge", credentials={"lighter_account_index": "1000"})
    assert db.get_profile(pid)["name"] == "Hedge"
    assert db.get_profile(pid)["lighter_account_index"] == "1000"
    # Delete
    db.delete_profile(pid)
    assert db.get_profile(pid) is None

def test_create_profile_rejects_duplicate_lighter_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    db.migrate_db()
    db.create_profile(name="A", exchange="lighter", credentials={"lighter_account_index": "111"})
    with pytest.raises(ValueError, match="lighter_account_index"):
        db.create_profile(name="B", exchange="lighter", credentials={"lighter_account_index": "111"})
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest tests/test_multi_profile_db.py -v`
Expected: FAIL on the new tests.

- [ ] **Step 3: Implement helpers**

Append to `bot/db.py`:

```python
_PROFILE_CRED_FIELDS = (
    "lighter_account_index", "lighter_api_key_private", "lighter_api_key_index",
    "hyperliquid_address", "hyperliquid_secret",
)

def list_profiles() -> list[dict]:
    rows = get_conn().execute(
        "SELECT id, name, exchange, lighter_account_index, hyperliquid_address, "
        "created_at, updated_at FROM profiles ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]

def get_profile(profile_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    return dict(row) if row else None

def create_profile(*, name: str, exchange: str, credentials: dict) -> int:
    if not name.strip():
        raise ValueError("name is required")
    if exchange not in ("lighter", "hyperliquid"):
        raise ValueError(f"unknown exchange: {exchange}")
    creds = {k: credentials.get(k) for k in _PROFILE_CRED_FIELDS}
    _check_unique_lighter_account(creds.get("lighter_account_index"), exclude_id=None)
    now = int(time.time() * 1000)
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO profiles
           (name, exchange, lighter_account_index, lighter_api_key_private,
            lighter_api_key_index, hyperliquid_address, hyperliquid_secret,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, exchange,
         creds["lighter_account_index"], creds["lighter_api_key_private"],
         creds["lighter_api_key_index"], creds["hyperliquid_address"],
         creds["hyperliquid_secret"], now, now),
    )
    conn.commit()
    return cur.lastrowid

def update_profile(profile_id: int, *, name: str | None = None,
                   exchange: str | None = None, credentials: dict | None = None):
    fields, values = [], []
    if name is not None:
        if not name.strip():
            raise ValueError("name cannot be empty")
        fields.append("name = ?"); values.append(name)
    if exchange is not None:
        if exchange not in ("lighter", "hyperliquid"):
            raise ValueError(f"unknown exchange: {exchange}")
        fields.append("exchange = ?"); values.append(exchange)
    if credentials:
        if "lighter_account_index" in credentials:
            _check_unique_lighter_account(
                credentials["lighter_account_index"], exclude_id=profile_id
            )
        for k in _PROFILE_CRED_FIELDS:
            if k in credentials:
                fields.append(f"{k} = ?"); values.append(credentials[k])
    if not fields:
        return
    fields.append("updated_at = ?")
    values.append(int(time.time() * 1000))
    values.append(profile_id)
    conn = get_conn()
    conn.execute(f"UPDATE profiles SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()

def delete_profile(profile_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    # Cascade delete of profile-scoped data
    conn.execute("DELETE FROM trades WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM signals WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM logs WHERE profile_id = ?", (profile_id,))
    conn.execute(
        "DELETE FROM config WHERE key LIKE ?", (f"profile.{profile_id}.%",)
    )
    conn.commit()

def _check_unique_lighter_account(account_index: str | None, exclude_id: int | None):
    if not account_index:
        return
    row = get_conn().execute(
        "SELECT id FROM profiles WHERE lighter_account_index = ? AND id != ?",
        (account_index, exclude_id or -1),
    ).fetchone()
    if row:
        raise ValueError(
            f"lighter_account_index '{account_index}' is already used by profile {row['id']}"
        )
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest tests/test_multi_profile_db.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/test_multi_profile_db.py
git commit -m "feat(db): profile CRUD helpers with uniqueness guard"
```

### Task 2.2 — Profile-aware config helpers

**Files:**
- Modify: `hyperliquid-bot/bot/db.py`
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Failing test**

```python
def test_profile_config_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db(); db.migrate_db()
    db.set_profile_config(1, "bot_status", "running")
    assert db.get_profile_config(1, "bot_status") == "running"
    assert db.get_config("profile.1.bot_status") == "running"
    assert db.get_profile_config(1, "missing") is None
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Implement**

Append to `bot/db.py`:

```python
def get_profile_config(profile_id: int, key: str) -> str | None:
    return get_config(f"profile.{profile_id}.{key}")

def set_profile_config(profile_id: int, key: str, value: str):
    set_config(f"profile.{profile_id}.{key}", value)

def set_profile_configs(profile_id: int, kvs: dict):
    set_configs({f"profile.{profile_id}.{k}": v for k, v in kvs.items()})
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/bot/db.py hyperliquid-bot/tests/test_multi_profile_db.py
git commit -m "feat(db): profile-aware config helpers"
```

### Task 2.3 — Profile-aware strategy config

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (existing `get_strategy_config` / `set_strategy_config`)
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Failing test**

```python
def test_strategy_config_by_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db(); db.migrate_db()
    db.set_strategy_config("bb_stoch_btc_5m", True, {"x": 1}, profile_id=1)
    cfg = db.get_strategy_config("bb_stoch_btc_5m", profile_id=1)
    assert cfg["enabled"] is True and cfg["params"] == {"x": 1}
    # Different profile sees defaults
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    cfg2 = db.get_strategy_config("bb_stoch_btc_5m", profile_id=pid2)
    assert cfg2["enabled"] is False and cfg2["params"] == {}
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Rewrite the two functions**

Replace existing `get_strategy_config` and `set_strategy_config` in `bot/db.py`:

```python
def get_strategy_config(strategy_name: str, profile_id: int = 1) -> dict:
    enabled_key = f"profile.{profile_id}.strategy.{strategy_name}.enabled"
    params_key = f"profile.{profile_id}.strategy.{strategy_name}.params"
    enabled_raw = get_config(enabled_key)
    if enabled_raw is None:
        enabled_raw = "false"
        set_config(enabled_key, enabled_raw)
    enabled = enabled_raw == "true"
    params_raw = get_config(params_key) or "{}"
    try:
        params = json.loads(params_raw)
    except json.JSONDecodeError:
        params = {}
    return {"enabled": enabled, "params": params}

def set_strategy_config(strategy_name: str, enabled: bool, params: dict, profile_id: int = 1):
    set_configs({
        f"profile.{profile_id}.strategy.{strategy_name}.enabled": "true" if enabled else "false",
        f"profile.{profile_id}.strategy.{strategy_name}.params": json.dumps(params),
    })
```

- [ ] **Step 4: Update all call sites**

Run `grep -rn "get_strategy_config\|set_strategy_config" hyperliquid-bot --include="*.py"` and update each to pass `profile_id=` explicitly. Touch points: `bot/strategies/manager.py`, `bot/backtest/engine.py`, `bot/backtest/scanner.py`, `dashboard/app.py`. Pass `profile_id=1` everywhere for now. If a site can later receive a different id (e.g. flask endpoint), wire the value through but keep the default.

- [ ] **Step 5: Run full strategy/backtest tests**

Run: `pytest tests/ -k "strategy or backtest or scanner" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot
git commit -m "feat(db): make strategy config profile-aware (defaults to profile 1)"
```

### Task 2.4 — Profile-aware trade/signal/log inserts and queries

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (`insert_trade`, `get_open_trades`, `update_trade`, `insert_signal`, `insert_log`, `get_logs`, `get_strategy_stats`)
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Failing test**

```python
def test_trades_signals_logs_are_scoped_by_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db(); db.migrate_db()
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    db.insert_trade({"profile_id": 1, "asset": "BTC", "side": "long",
                     "entry_price": 100.0, "size": 0.1, "status": "open",
                     "entry_time": "2026-05-27T00:00:00", "strategy": "x"})
    db.insert_trade({"profile_id": pid2, "asset": "ETH", "side": "long",
                     "entry_price": 200.0, "size": 0.5, "status": "open",
                     "entry_time": "2026-05-27T00:00:00", "strategy": "y"})
    p1 = db.get_open_trades(profile_id=1)
    p2 = db.get_open_trades(profile_id=pid2)
    assert len(p1) == 1 and p1[0]["asset"] == "BTC"
    assert len(p2) == 1 and p2[0]["asset"] == "ETH"
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Add `profile_id` everywhere**

In `bot/db.py`:
- `insert_trade(trade)` — read `trade.get("profile_id", 1)` and include in the INSERT column list and values.
- `get_open_trades(profile_id: int = 1)` — append `WHERE profile_id = ?` (combine with existing `status = 'open'`).
- `update_trade(trade_id, **fields)` — no change (operates by PK).
- `insert_signal(signal)` — read `signal.get("profile_id", 1)`, include in INSERT.
- `insert_log(timestamp, level, module, message, profile_id=None)` — add the optional column; INSERT NULL when omitted (global logs from candle manager).
- `get_logs(limit=200, level=None, profile_id=None)` — filter on `profile_id IS NULL OR profile_id = ?` when set; no filter when `profile_id is None`.
- `get_strategy_stats(profile_id: int = 1)` — append `WHERE profile_id = ?` to the underlying query.
- `get_trades_closed(...)`, `get_signals(...)` — add `profile_id: int | None = None` filter (same pattern as logs).

Show the implementation for `insert_trade` (others follow the same shape):

```python
def insert_trade(trade: dict) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO trades
           (profile_id, asset, side, entry_price, size, status, entry_time,
            ema9, ema21, rsi2, volume, atr, funding_rate, tp_price, sl_price,
            order_id, strategy, signal_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade.get("profile_id", 1),
            trade["asset"], trade["side"], trade["entry_price"], trade["size"],
            trade.get("status", "open"), trade["entry_time"],
            trade.get("ema9"), trade.get("ema21"), trade.get("rsi2"),
            trade.get("volume"), trade.get("atr"), trade.get("funding_rate"),
            trade.get("tp_price"), trade.get("sl_price"),
            trade.get("order_id"), trade.get("strategy"), trade.get("signal_price"),
        ),
    )
    conn.commit()
    return cur.lastrowid
```

- [ ] **Step 4: Update call sites**

`grep -rn "insert_trade\|get_open_trades\|insert_signal\|insert_log\|get_strategy_stats" hyperliquid-bot --include="*.py"`. Inject `profile_id` (default `1` for now) into each call. Trades being inserted from `executor.py` should add the key to the trade dict before passing.

- [ ] **Step 5: Run all tests**

Run: `cd hyperliquid-bot && pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot
git commit -m "feat(db): scope trades/signals/logs by profile_id"
```

### Task 2.5 — Profile-aware COI counter and candle helpers

**Files:**
- Modify: `hyperliquid-bot/bot/db.py` (`get_lighter_coi_counter`, `set_lighter_coi_counter`)
- Modify: `hyperliquid-bot/bot/exchanges/lighter_exchange.py` (callers of those helpers)
- Test: `hyperliquid-bot/tests/test_multi_profile_db.py`

- [ ] **Step 1: Failing test**

```python
def test_coi_counter_is_per_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db(); db.migrate_db()
    db.set_lighter_coi_counter(7, profile_id=1)
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    db.set_lighter_coi_counter(99, profile_id=pid2)
    assert db.get_lighter_coi_counter(profile_id=1) == 7
    assert db.get_lighter_coi_counter(profile_id=pid2) == 99
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Rewrite the helpers**

In `bot/db.py`:

```python
def get_lighter_coi_counter(profile_id: int = 1) -> int:
    raw = get_config(f"profile.{profile_id}.lighter.client_order_counter")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0

def set_lighter_coi_counter(n: int, profile_id: int = 1) -> None:
    set_config(f"profile.{profile_id}.lighter.client_order_counter", str(int(n)))
```

In `bot/exchanges/lighter_exchange.py`, `LighterExchangeClient.__init__` needs to accept `profile_id` and store it. Pass `self._profile_id` to all `db.get_lighter_coi_counter()` / `db.set_lighter_coi_counter()` calls.

Modify the signature:

```python
def __init__(self, ..., profile_id: int = 1):
    ...
    self._profile_id = profile_id
    self._client_order_counter = db.get_lighter_coi_counter(profile_id=self._profile_id)
```

And the persist site:

```python
db.set_lighter_coi_counter(self._client_order_counter, profile_id=self._profile_id)
```

- [ ] **Step 4: Update factory**

In `bot/exchanges/factory.py`, `create_exchange_client(...)` already reads credentials. Add a `profile_id` parameter (default 1) and forward to the Lighter constructor.

- [ ] **Step 5: Run tests, expect pass**

Run: `pytest tests/test_multi_profile_db.py -v` plus any existing `tests/exchanges/` cases. Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot
git commit -m "feat(exchange): scope COI counter by profile_id"
```

### Task 2.6 — Plumb `profile_id` through executor and risk

**Files:**
- Modify: `hyperliquid-bot/bot/executor.py`
- Modify: `hyperliquid-bot/bot/risk.py`
- Test: `hyperliquid-bot/tests/test_risk_fees.py` (extend or add a smoke test)

- [ ] **Step 1: Failing test**

Add to `tests/test_risk_fees.py` (or a new file) a smoke test that calls `executor.open_position(client, signal, 100, cfg, profile_id=2)` against a mocked client and verifies the persisted trade row has `profile_id=2`. Pattern matches existing executor tests.

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Refactor**

In `bot/executor.py`:
- Top-level lock dict becomes `_open_locks: dict[tuple[int, str], threading.Lock] = {}`.
- `_get_asset_lock(profile_id: int, asset: str) -> threading.Lock` keys by `(profile_id, asset)`.
- `open_position(client, signal, size_usd, cfg, *, profile_id: int = 1)`:
  - Lock acquire by `(profile_id, signal["asset"])`.
  - The dedup query becomes `db.get_open_trades(profile_id=profile_id)`.
  - The trade dict assembled at insert-time includes `"profile_id": profile_id`.
  - When calling `client.market_open(...)` and downstream, pass `profile_id` through to anything that touches `db`.
- `close_position(...)` similarly takes `profile_id` and forwards to `db.update_trade` / `db.insert_log(profile_id=...)`.

In `bot/risk.py`:
- `check_open_positions_tp_sl(client, ..., *, profile_id: int = 1)` and any helpers that read trades pass `profile_id` to db calls.

- [ ] **Step 4: Update call sites**

`grep -rn "open_position\|close_position\|check_open_positions_tp_sl" hyperliquid-bot --include="*.py"`. Add `profile_id=1` to each call.

- [ ] **Step 5: Run tests, expect pass.**

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot
git commit -m "feat(executor,risk): take explicit profile_id (defaults to 1)"
```

### Task 2.7 — Plumb `profile_id` through strategies manager and main loop

**Files:**
- Modify: `hyperliquid-bot/bot/strategies/manager.py`
- Modify: `hyperliquid-bot/main.py`
- Test: `hyperliquid-bot/tests/strategies/` (extend a smoke test)

- [ ] **Step 1: Failing test**

In `tests/strategies/test_manager_profile.py` (new):

```python
from bot.strategies import manager
from bot import db

def test_evaluate_all_uses_profile_strategies(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db._local.conn = None
    db.init_db(); db.migrate_db()
    # Enable bb_stoch_btc_5m only on profile 2
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    db.set_strategy_config("bb_stoch_btc_5m", True, {}, profile_id=pid2)
    # Profile 1 sees it disabled
    assert manager.get_enabled_strategies(profile_id=1) == []
    assert any(
        s.NAME == "bb_stoch_btc_5m"
        for s in manager.get_enabled_strategies(profile_id=pid2)
    )
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Refactor manager**

In `bot/strategies/manager.py`:
- Add `profile_id: int = 1` to `evaluate_all`, `get_enabled_strategies`, `get_active_assets`, `get_required_timeframes`, and any internal helper that reads `db.get_strategy_config`. Forward to `db`.
- `register_dynamic_instance(strategy, asset, tag=None, timeframe="5m", _legacy_no_tf_in_name=False)` — no change (instance registration is global; configs are per-profile).

- [ ] **Step 4: Refactor main loop (still hardcoded profile=1)**

In `main.py`:
- `bot_loop()` and `process_asset(...)` accept `profile_id: int = 1` as the first param of their signatures.
- All `db.get_config("bot_status")` becomes `db.get_profile_config(profile_id, "bot_status")`.
- All `db.get_open_trades()` becomes `db.get_open_trades(profile_id=profile_id)`.
- `manager.evaluate_all(...)` and `manager.get_active_assets(...)` calls pass `profile_id=profile_id`.
- `start_bot()` / `stop_bot()` / `pause_bot()` / `resume_bot()` / `get_bot_status()` accept `profile_id: int = 1` and forward to `db.set_profile_config(profile_id, "bot_status", ...)` / `db.get_profile_config(profile_id, "bot_status")`.
- `_bot_thread`, `_stop_event` remain singletons for now (Phase 4 turns these into dicts).

- [ ] **Step 5: Run all tests**

Run: `cd hyperliquid-bot && pytest -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot
git commit -m "feat(manager,main): thread profile_id through bot loop (single profile still)"
```

### Task 2.8 — Plumb `profile_id` through dashboard endpoints

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`

- [ ] **Step 1: Use `g.profile_id` everywhere**

Add a Flask `before_request` (Phase 3 will wire the session cookie; for now hardcode to 1):

```python
from flask import g

@app.before_request
def _set_active_profile():
    g.profile_id = 1  # placeholder until Phase 3 wires session
```

Every endpoint that currently calls `db.get_open_trades()` / `db.get_strategy_stats()` / `db.get_strategy_config(name)` / `db.get_config("strategy.<name>.*")` / `db.get_config("bot_status")` becomes `... (profile_id=g.profile_id)` or `... (g.profile_id, ...)`. Same for `set_strategy_config`, `db.set_config("bot_status", ...)` (becomes `db.set_profile_config(g.profile_id, "bot_status", ...)`).

Endpoints to audit:
- `/api/status` (bot_status)
- `/api/trades`, `/api/positions`, `/api/strategy-stats`
- `/api/strategies`, `/api/strategies/<name>`, `/api/strategies/applied`
- `/api/strategies/applied/<name>` DELETE
- `/api/backtest/run`
- `/api/scanner/apply`
- `/api/logs`
- `/api/config/*` for risk/sizing/assets

- [ ] **Step 2: Run the app manually**

Run: `cd hyperliquid-bot && python run.py`
Verify (via curl or browser): dashboard loads, trades list shows existing rows, strategies tab populated. Expect identical behaviour to pre-refactor.

- [ ] **Step 3: Run pytest**

Run: `pytest -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add hyperliquid-bot/dashboard
git commit -m "feat(dashboard): route per-profile endpoints through g.profile_id"
```

### Task 2.9 — Plumb `profile_id` through backtest engine and scanner apply

**Files:**
- Modify: `hyperliquid-bot/bot/backtest/engine.py`
- Modify: `hyperliquid-bot/bot/backtest/scanner.py`

- [ ] **Step 1: Update signatures**

- `engine.start_backtest_job(strategy_name, asset, days, *, profile_id: int = 1)` and `_run_backtest(...)` read params with `db.get_strategy_config(strategy_name, profile_id=profile_id)`.
- `scanner.apply_result(asset, strategy, params, tag=None, timeframe="5m", *, profile_id: int = 1)` writes `db.set_configs({...})` namespaced via `set_profile_configs(profile_id, {...})`.

For `scanner.apply_result`, replace the three `set_configs` keys with:

```python
db.set_profile_configs(profile_id, {
    f"strategy.{name}.params": json.dumps(translated),
    f"strategy.{name}.scanner_metrics": json.dumps(scanner_metrics),
    f"strategy.{name}.enabled": "true",
})
```

- [ ] **Step 2: Wire call sites**

In `dashboard/app.py`, the `/api/backtest/run` and `/api/scanner/apply` endpoints pass `profile_id=g.profile_id` to these helpers.

- [ ] **Step 3: Run pytest**

Run: `cd hyperliquid-bot && pytest tests/backtest tests/strategies -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add hyperliquid-bot
git commit -m "feat(backtest,scanner): take profile_id, write under namespaced keys"
```

### Task 2.10 — Phase 2 smoke test in real DB

- [ ] **Step 1: Manual smoke**

Run `python run.py`. Verify in the dashboard:
- Bot status, trades, signals, strategies tab all populate correctly.
- Start/Pause/Stop still works.
- Scanner apply persists under `profile.1.strategy.<name>.*` (confirm via `sqlite3 bot_data.db 'SELECT key FROM config WHERE key LIKE "profile.1.%"'`).

- [ ] **Step 2: Commit a note in the changelog if any tweaks were needed.**

---

## Phase 3 — Profile CRUD endpoints + UI selector

Goal: dashboard lists profiles, lets you create / rename / edit credentials / delete; header dropdown switches the active profile in the session; single bot still runs (multi-bot lands in Phase 4).

### Task 3.1 — GET `/api/profiles`

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`
- Test: `hyperliquid-bot/tests/dashboard/test_profile_endpoints.py` (new)

- [ ] **Step 1: Failing test**

```python
import json
from bot import db
from dashboard.app import create_app

def _bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db._local.conn = None
    db.init_db(); db.migrate_db()

def test_list_profiles(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    db.create_profile(name="Conta 2", exchange="lighter",
                      credentials={"lighter_account_index": "42"})
    app, _ = create_app()
    client = app.test_client()
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    data = resp.get_json()
    names = {p["name"] for p in data}
    assert {"Default", "Conta 2"}.issubset(names)
    # Sensitive fields are NOT returned
    assert all("lighter_api_key_private" not in p for p in data)
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Implement endpoint**

In `dashboard/app.py`:

```python
_PROFILE_PUBLIC_FIELDS = (
    "id", "name", "exchange",
    "lighter_account_index", "hyperliquid_address",
    "created_at", "updated_at",
)

@app.route("/api/profiles", methods=["GET"])
def api_list_profiles():
    profiles = db.list_profiles()
    out = []
    for p in profiles:
        d = {k: p.get(k) for k in _PROFILE_PUBLIC_FIELDS}
        d["bot_status"] = db.get_profile_config(p["id"], "bot_status") or "stopped"
        out.append(d)
    return jsonify(out)
```

- [ ] **Step 4: Run, expect pass. Commit.**

```bash
git add hyperliquid-bot/dashboard hyperliquid-bot/tests/dashboard
git commit -m "feat(api): GET /api/profiles"
```

### Task 3.2 — POST `/api/profiles`

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`
- Test: same file

- [ ] **Step 1: Failing test**

```python
def test_create_profile(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    app, _ = create_app()
    c = app.test_client()
    resp = c.post("/api/profiles", json={
        "name": "Hedge", "exchange": "lighter",
        "credentials": {"lighter_account_index": "555"},
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Hedge" and body["id"] >= 2

def test_create_profile_rejects_duplicate(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    db.create_profile(name="A", exchange="lighter",
                      credentials={"lighter_account_index": "777"})
    app, _ = create_app()
    c = app.test_client()
    resp = c.post("/api/profiles", json={
        "name": "B", "exchange": "lighter",
        "credentials": {"lighter_account_index": "777"},
    })
    assert resp.status_code == 409
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Implement**

```python
@app.route("/api/profiles", methods=["POST"])
def api_create_profile():
    body = request.get_json(silent=True) or {}
    try:
        pid = db.create_profile(
            name=body.get("name", "").strip(),
            exchange=body.get("exchange", "lighter"),
            credentials=body.get("credentials") or {},
        )
    except ValueError as e:
        msg = str(e)
        status = 409 if "already used" in msg else 400
        return jsonify({"error": msg}), status
    return jsonify(db.get_profile(pid)), 201
```

- [ ] **Step 4: Run, expect pass. Commit.**

```bash
git commit -am "feat(api): POST /api/profiles"
```

### Task 3.3 — PATCH `/api/profiles/<id>` (rename + edit creds)

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`

- [ ] **Step 1: Failing test**

```python
def test_patch_profile_rename(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    pid = db.create_profile(name="Old", exchange="lighter", credentials={})
    app, _ = create_app()
    c = app.test_client()
    resp = c.patch(f"/api/profiles/{pid}", json={"name": "New"})
    assert resp.status_code == 200
    assert db.get_profile(pid)["name"] == "New"
```

- [ ] **Step 2: Implement**

```python
@app.route("/api/profiles/<int:pid>", methods=["PATCH"])
def api_patch_profile(pid):
    if db.get_profile(pid) is None:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    try:
        db.update_profile(
            pid,
            name=body.get("name"),
            exchange=body.get("exchange"),
            credentials=body.get("credentials"),
        )
    except ValueError as e:
        msg = str(e)
        return jsonify({"error": msg}), (409 if "already used" in msg else 400)
    return jsonify(db.get_profile(pid))
```

- [ ] **Step 3: Run, commit.**

```bash
git commit -am "feat(api): PATCH /api/profiles/<id>"
```

### Task 3.4 — DELETE `/api/profiles/<id>` with safety guards

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`

- [ ] **Step 1: Failing tests**

```python
def test_delete_blocks_last_profile(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    app, _ = create_app()
    c = app.test_client()
    resp = c.delete("/api/profiles/1")
    assert resp.status_code == 409

def test_delete_blocks_with_open_trade(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    pid = db.create_profile(name="X", exchange="lighter", credentials={})
    db.insert_trade({"profile_id": pid, "asset": "BTC", "side": "long",
                     "entry_price": 1.0, "size": 0.1, "status": "open",
                     "entry_time": "2026-05-27", "strategy": "x"})
    app, _ = create_app()
    c = app.test_client()
    resp = c.delete(f"/api/profiles/{pid}")
    assert resp.status_code == 409
```

- [ ] **Step 2: Implement**

```python
@app.route("/api/profiles/<int:pid>", methods=["DELETE"])
def api_delete_profile(pid):
    if db.get_profile(pid) is None:
        return jsonify({"error": "not found"}), 404
    profiles = db.list_profiles()
    if len(profiles) <= 1:
        return jsonify({"error": "cannot delete the last profile"}), 409
    open_rows = db.get_open_trades(profile_id=pid)
    if open_rows:
        return jsonify({
            "error": "close open positions before deleting this profile",
            "open_count": len(open_rows),
        }), 409
    db.delete_profile(pid)
    return "", 204
```

- [ ] **Step 3: Run, commit.**

```bash
git commit -am "feat(api): DELETE /api/profiles/<id> with safety guards"
```

### Task 3.5 — POST `/api/profiles/<id>/activate` + Flask session

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`

- [ ] **Step 1: Wire session secret**

If `create_app()` does not set `app.secret_key`, add at the top of `create_app`:

```python
app.secret_key = db.get_config("flask.secret_key")
if not app.secret_key:
    import secrets
    app.secret_key = secrets.token_hex(32)
    db.set_config("flask.secret_key", app.secret_key)
```

- [ ] **Step 2: Replace the placeholder `before_request`**

```python
from flask import g, session

@app.before_request
def _set_active_profile():
    pid = session.get("active_profile_id")
    if pid is None:
        first = db.list_profiles()
        pid = first[0]["id"] if first else 1
        session["active_profile_id"] = pid
    elif db.get_profile(pid) is None:
        # Profile was deleted under us
        first = db.list_profiles()
        pid = first[0]["id"] if first else 1
        session["active_profile_id"] = pid
    g.profile_id = pid
```

- [ ] **Step 3: Add the activate endpoint**

```python
@app.route("/api/profiles/<int:pid>/activate", methods=["POST"])
def api_activate_profile(pid):
    if db.get_profile(pid) is None:
        return jsonify({"error": "not found"}), 404
    session["active_profile_id"] = pid
    return jsonify({"active_profile_id": pid})
```

- [ ] **Step 4: Test**

```python
def test_activate_profile_persists_in_session(tmp_path, monkeypatch):
    _bootstrap(tmp_path, monkeypatch)
    pid = db.create_profile(name="Hedge", exchange="lighter", credentials={})
    app, _ = create_app()
    c = app.test_client()
    resp = c.post(f"/api/profiles/{pid}/activate")
    assert resp.status_code == 200
    # /api/status should now reflect the new profile's state
    db.set_profile_config(pid, "bot_status", "paused")
    db.set_profile_config(1, "bot_status", "running")
    resp = c.get("/api/status")
    assert resp.get_json().get("bot_status") == "paused"
```

Run, commit.

```bash
git commit -am "feat(session): persist active_profile_id in Flask session"
```

### Task 3.6 — Profile dropdown in the header

**Files:**
- Modify: `hyperliquid-bot/dashboard/templates/base.html`
- Modify: `hyperliquid-bot/dashboard/static/css/dashboard.css` (or whatever file holds the header styles — confirm with `grep`)
- Modify: `hyperliquid-bot/dashboard/static/js/dashboard.js` (new event handlers)

- [ ] **Step 1: Add markup**

In `templates/base.html`, immediately inside `<header>` and before the existing nav links:

```html
<div class="profile-selector" id="profileSelector">
  <button class="profile-current" id="profileCurrentBtn">
    <span class="profile-status-dot" id="profileStatusDot"></span>
    <span class="profile-current-name" id="profileCurrentName">…</span>
    <span class="caret">▾</span>
  </button>
  <div class="profile-menu" id="profileMenu" hidden>
    <ul class="profile-list" id="profileList"></ul>
    <div class="profile-actions">
      <button data-action="rename">✎ Renomear perfil ativo</button>
      <button data-action="edit-creds">⚙ Editar credenciais</button>
      <button data-action="delete">🗑 Excluir perfil ativo</button>
      <button data-action="new">+ Novo perfil</button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Add CSS**

Append to `static/css/dashboard.css`:

```css
.profile-selector { position: relative; display: inline-block; margin-right: 12px; }
.profile-current { display: flex; align-items: center; gap: 6px;
  background: #1c1f24; color: #ddd; border: 1px solid #2a2d33;
  padding: 6px 10px; border-radius: 6px; cursor: pointer; }
.profile-status-dot { width: 8px; height: 8px; border-radius: 50%;
  background: #555; display: inline-block; }
.profile-status-dot.running { background: #2ecc71; }
.profile-status-dot.paused  { background: #f1c40f; }
.profile-status-dot.stopped { background: #555; }
.profile-menu { position: absolute; top: 100%; left: 0; z-index: 200;
  background: #1c1f24; border: 1px solid #2a2d33; border-radius: 6px;
  min-width: 240px; margin-top: 4px; padding: 6px 0; }
.profile-list { list-style: none; margin: 0; padding: 0; }
.profile-list li { display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; cursor: pointer; }
.profile-list li:hover { background: #25282d; }
.profile-list li.active { color: #2ecc71; font-weight: 600; }
.profile-actions { border-top: 1px solid #2a2d33; padding: 4px 0; }
.profile-actions button { display: block; width: 100%; text-align: left;
  background: transparent; color: #ddd; border: 0; padding: 6px 12px; cursor: pointer; }
.profile-actions button:hover { background: #25282d; }
```

- [ ] **Step 3: Add JS**

Append to `static/js/dashboard.js`:

```js
let _activeProfileId = null;
let _profilesCache = [];

async function fetchProfiles() {
  const res = await fetch('/api/profiles');
  _profilesCache = await res.json();
  return _profilesCache;
}

async function fetchActiveProfile() {
  // Inferred from the first /api/status payload — but lighter to read explicitly:
  const profiles = await fetchProfiles();
  // The session cookie already targets one; trust the server. We render and let
  // socketio updates correct any drift.
  const fromCookie = profiles.find(p => p.is_active) || profiles[0];
  _activeProfileId = fromCookie?.id ?? 1;
  return _activeProfileId;
}

function renderProfileMenu() {
  const list = document.getElementById('profileList');
  if (!list) return;
  list.innerHTML = '';
  for (const p of _profilesCache) {
    const li = document.createElement('li');
    if (p.id === _activeProfileId) li.classList.add('active');
    li.dataset.id = p.id;
    li.innerHTML =
      `<span class="profile-status-dot ${p.bot_status}"></span>` +
      `<span>${p.name}</span>`;
    li.addEventListener('click', () => activateProfile(p.id));
    list.appendChild(li);
  }
  const active = _profilesCache.find(p => p.id === _activeProfileId);
  if (active) {
    document.getElementById('profileCurrentName').textContent = active.name;
    const dot = document.getElementById('profileStatusDot');
    dot.className = 'profile-status-dot ' + active.bot_status;
  }
}

async function activateProfile(pid) {
  await fetch(`/api/profiles/${pid}/activate`, {method: 'POST'});
  window.location.reload();  // simplest: server-rendered context refresh
}

document.getElementById('profileCurrentBtn')?.addEventListener('click', () => {
  const m = document.getElementById('profileMenu');
  m.hidden = !m.hidden;
});

document.getElementById('profileSelector')?.addEventListener('click', (e) => {
  const action = e.target?.dataset?.action;
  if (!action) return;
  if (action === 'new') openProfileModal({mode: 'create'});
  if (action === 'rename') openProfileModal({mode: 'rename', id: _activeProfileId});
  if (action === 'edit-creds') openProfileModal({mode: 'creds', id: _activeProfileId});
  if (action === 'delete') deleteActiveProfile();
});

async function deleteActiveProfile() {
  if (!confirm('Excluir o perfil ativo? Trades e configs serão removidos.')) return;
  const res = await fetch(`/api/profiles/${_activeProfileId}`, {method: 'DELETE'});
  if (res.status === 409) {
    const body = await res.json();
    alert('Não foi possível excluir: ' + (body.error || ''));
    return;
  }
  window.location.reload();
}

// Bootstrap
(async () => {
  await fetchActiveProfile();
  renderProfileMenu();
})();
```

- [ ] **Step 4: Make `/api/profiles` mark the active one**

Update the GET handler:

```python
@app.route("/api/profiles", methods=["GET"])
def api_list_profiles():
    profiles = db.list_profiles()
    out = []
    for p in profiles:
        d = {k: p.get(k) for k in _PROFILE_PUBLIC_FIELDS}
        d["bot_status"] = db.get_profile_config(p["id"], "bot_status") or "stopped"
        d["is_active"] = (p["id"] == g.profile_id)
        out.append(d)
    return jsonify(out)
```

- [ ] **Step 5: Manual smoke**

Run `python run.py`, open dashboard, see dropdown with "Default" + status dot. Click → menu appears with action buttons.

- [ ] **Step 6: Commit**

```bash
git add hyperliquid-bot/dashboard
git commit -m "feat(ui): profile selector dropdown in header"
```

### Task 3.7 — Profile create/rename/edit-creds modal

**Files:**
- Modify: `hyperliquid-bot/dashboard/templates/base.html` (modal markup)
- Modify: `hyperliquid-bot/dashboard/static/js/dashboard.js`
- Modify: `hyperliquid-bot/dashboard/static/css/dashboard.css`

- [ ] **Step 1: Add modal markup**

In `templates/base.html`, before `</body>`:

```html
<div class="modal-backdrop" id="profileModalBackdrop" hidden>
  <div class="modal" id="profileModal">
    <h3 id="profileModalTitle">Novo perfil</h3>
    <form id="profileForm">
      <label>Nome
        <input name="name" type="text" required>
      </label>
      <label>Exchange
        <select name="exchange">
          <option value="lighter">Lighter</option>
          <option value="hyperliquid">Hyperliquid</option>
        </select>
      </label>
      <fieldset class="creds creds-lighter">
        <legend>Lighter</legend>
        <label>Account Index <input name="lighter_account_index"></label>
        <label>API Key Private <input name="lighter_api_key_private" type="password"></label>
        <label>API Key Index <input name="lighter_api_key_index"></label>
      </fieldset>
      <fieldset class="creds creds-hl" hidden>
        <legend>Hyperliquid</legend>
        <label>Address <input name="hyperliquid_address"></label>
        <label>Secret <input name="hyperliquid_secret" type="password"></label>
      </fieldset>
      <div class="modal-actions">
        <button type="button" data-close>Cancelar</button>
        <button type="submit">Salvar</button>
      </div>
    </form>
  </div>
</div>
```

- [ ] **Step 2: JS — `openProfileModal({mode, id})`**

Append to `dashboard.js`:

```js
function openProfileModal({mode, id}) {
  const back = document.getElementById('profileModalBackdrop');
  const title = document.getElementById('profileModalTitle');
  const form = document.getElementById('profileForm');
  form.reset();
  form.dataset.mode = mode;
  form.dataset.id = id ?? '';
  title.textContent = ({
    create: 'Novo perfil',
    rename: 'Renomear perfil',
    creds: 'Editar credenciais',
  })[mode] || 'Perfil';
  // Hide credential fieldsets for rename
  form.querySelectorAll('fieldset.creds').forEach(f => {
    f.style.display = (mode === 'rename') ? 'none' : '';
  });
  // Pre-fill for rename/creds
  if (mode !== 'create') {
    const p = _profilesCache.find(x => x.id === id);
    if (p) {
      form.name.value = p.name;
      form.exchange.value = p.exchange;
      form.lighter_account_index.value = p.lighter_account_index || '';
      form.hyperliquid_address.value = p.hyperliquid_address || '';
    }
  }
  back.hidden = false;
}

document.getElementById('profileModalBackdrop')?.addEventListener('click', (e) => {
  if (e.target.dataset.close !== undefined || e.target.id === 'profileModalBackdrop') {
    document.getElementById('profileModalBackdrop').hidden = true;
  }
});

document.getElementById('profileForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const fd = new FormData(f);
  const credentials = {
    lighter_account_index: fd.get('lighter_account_index') || null,
    lighter_api_key_private: fd.get('lighter_api_key_private') || null,
    lighter_api_key_index: fd.get('lighter_api_key_index') || null,
    hyperliquid_address: fd.get('hyperliquid_address') || null,
    hyperliquid_secret: fd.get('hyperliquid_secret') || null,
  };
  const mode = f.dataset.mode;
  const id = f.dataset.id;
  let res;
  if (mode === 'create') {
    res = await fetch('/api/profiles', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        name: fd.get('name'),
        exchange: fd.get('exchange'),
        credentials,
      }),
    });
  } else {
    const body = (mode === 'rename')
      ? {name: fd.get('name')}
      : {credentials, exchange: fd.get('exchange')};
    res = await fetch(`/api/profiles/${id}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.error || `Falha (HTTP ${res.status})`);
    return;
  }
  document.getElementById('profileModalBackdrop').hidden = true;
  window.location.reload();
});
```

- [ ] **Step 3: CSS for modal**

Append to `dashboard.css`:

```css
.modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.6);
  display: flex; align-items: center; justify-content: center; z-index: 1000; }
.modal { background: #1c1f24; border: 1px solid #2a2d33; border-radius: 8px;
  padding: 20px; min-width: 360px; }
.modal h3 { margin: 0 0 12px; }
.modal label { display: block; margin: 8px 0; color: #ccc; }
.modal input, .modal select { width: 100%; background: #15171b; color: #eee;
  border: 1px solid #2a2d33; border-radius: 4px; padding: 6px; }
.modal fieldset { border: 1px solid #2a2d33; border-radius: 4px;
  padding: 8px 12px; margin: 10px 0; }
.modal .modal-actions { display: flex; gap: 8px; justify-content: flex-end;
  margin-top: 12px; }
.modal .modal-actions button { padding: 6px 14px; }
```

- [ ] **Step 4: Smoke test in browser**

Create a profile, rename it, edit credentials. Each action should refresh the page and the dropdown should reflect the change.

- [ ] **Step 5: Commit**

```bash
git add hyperliquid-bot/dashboard
git commit -m "feat(ui): create/rename/edit-credentials modal"
```

---

## Phase 4 — Multi-bot execution

Goal: starting/pausing/stopping a bot only affects that profile's bot. Multiple profiles can run in parallel sharing one candle manager.

### Task 4.1 — Convert `main.py` singletons to dicts

**Files:**
- Modify: `hyperliquid-bot/main.py`

- [ ] **Step 1: Rewrite globals**

Replace the top-level singleton globals (`_bot_thread`, `_stop_event`, `client`) with dicts:

```python
_bot_threads: dict[int, threading.Thread] = {}
_bot_clients: dict[int, object] = {}             # LighterExchangeClient | HyperliquidClient
_stop_events: dict[int, threading.Event] = {}
_bot_lock = threading.Lock()                     # protects the dicts
candle_mgr = None                                # singleton (unchanged)
```

- [ ] **Step 2: Add `union_assets()` helper**

```python
def _union_assets() -> list[str]:
    seen, out = set(), []
    with _bot_lock:
        pids = [pid for pid, t in _bot_threads.items() if t.is_alive()]
    for pid in pids:
        raw = db.get_profile_config(pid, "assets")
        if not raw:
            continue
        try:
            for a in json.loads(raw):
                if a not in seen:
                    seen.add(a); out.append(a)
        except json.JSONDecodeError:
            continue
    return out
```

- [ ] **Step 3: Run pytest sanity**

Existing tests don't touch these globals directly. Run `pytest -v`; expect PASS.

- [ ] **Step 4: Commit**

```bash
git commit -am "refactor(main): dict-based bot threads/clients keyed by profile_id"
```

### Task 4.2 — Per-profile `start_bot` / `stop_bot` / `pause_bot` / `resume_bot`

**Files:**
- Modify: `hyperliquid-bot/main.py`

- [ ] **Step 1: Rewrite the four functions**

```python
def start_bot(profile_id: int = 1):
    with _bot_lock:
        existing = _bot_threads.get(profile_id)
        if existing is not None and existing.is_alive():
            log.warning("Bot for profile %s already running", profile_id)
            return existing
        _stop_events[profile_id] = threading.Event()
        _bot_clients[profile_id] = _build_client_for_profile(profile_id)
        t = threading.Thread(
            target=bot_loop, args=(profile_id,),
            daemon=True, name=f"bot-loop-p{profile_id}",
        )
        _bot_threads[profile_id] = t
        t.start()
    db.set_profile_config(profile_id, "bot_status", "running")
    _refresh_candle_manager_assets()
    return t

def stop_bot(profile_id: int = 1):
    with _bot_lock:
        ev = _stop_events.get(profile_id)
        if ev:
            ev.set()
    db.set_profile_config(profile_id, "bot_status", "stopped")
    log.info("Stop signal sent to bot of profile %s", profile_id)
    # Thread will exit on next loop iteration; clean up async
    threading.Thread(
        target=_reap_bot_thread, args=(profile_id,), daemon=True
    ).start()

def pause_bot(profile_id: int = 1):
    db.set_profile_config(profile_id, "bot_status", "paused")

def resume_bot(profile_id: int = 1):
    db.set_profile_config(profile_id, "bot_status", "running")

def get_bot_status(profile_id: int = 1) -> str:
    return db.get_profile_config(profile_id, "bot_status") or "stopped"

def _reap_bot_thread(profile_id: int):
    t = _bot_threads.get(profile_id)
    if t:
        t.join(timeout=10)
    with _bot_lock:
        _bot_threads.pop(profile_id, None)
        _bot_clients.pop(profile_id, None)
        _stop_events.pop(profile_id, None)
    _refresh_candle_manager_assets()

def _build_client_for_profile(profile_id: int):
    from bot.exchanges.factory import create_exchange_client
    return create_exchange_client(profile_id=profile_id)
```

- [ ] **Step 2: Update `create_exchange_client`**

In `bot/exchanges/factory.py`, accept `profile_id` and read credentials from `db.get_profile(profile_id)` rather than the legacy globals. Forward `profile_id` to the constructor (Task 2.5 already wired the COI counter through it).

- [ ] **Step 3: Run pytest**

Run: `pytest -v`. Expect PASS.

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(main): per-profile start/stop/pause/resume with thread reaper"
```

### Task 4.3 — Multi-profile `on_candle_close` dispatch

**Files:**
- Modify: `hyperliquid-bot/main.py`
- Test: `hyperliquid-bot/tests/test_multi_bot.py` (new)

- [ ] **Step 1: Failing test**

```python
import threading, time
from unittest.mock import MagicMock, patch
from bot import db
import main as bot_main

def test_on_candle_close_dispatches_per_running_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db._local.conn = None
    db.init_db(); db.migrate_db()
    import json as _json
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    db.set_profile_config(1, "assets", _json.dumps(["BTC"]))
    db.set_profile_config(pid2, "assets", _json.dumps(["BTC", "ETH"]))

    seen = []
    def fake_process(pid, asset, cfg, *a, **k):
        seen.append((pid, asset))

    monkeypatch.setattr(bot_main, "process_asset", fake_process)
    # Mark both profiles "running" in the thread dict (no real threads)
    bot_main._bot_threads[1] = threading.Thread(target=lambda: time.sleep(60), daemon=True)
    bot_main._bot_threads[pid2] = threading.Thread(target=lambda: time.sleep(60), daemon=True)
    bot_main._bot_threads[1].start()
    bot_main._bot_threads[pid2].start()
    try:
        db.set_profile_config(1, "bot_status", "running")
        db.set_profile_config(pid2, "bot_status", "running")
        bot_main._on_candle_close_dispatch("BTC", "5m")
        bot_main._on_candle_close_dispatch("ETH", "5m")
        time.sleep(0.2)  # let any executor pool drain (if used)
    finally:
        for ev in bot_main._stop_events.values():
            ev.set()
    assert (1, "BTC") in seen
    assert (pid2, "BTC") in seen
    assert (pid2, "ETH") in seen
    assert (1, "ETH") not in seen
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Implement**

Replace the existing `on_candle_close` body in `main.py`:

```python
def _on_candle_close_dispatch(asset: str, interval: str):
    if interval != "5m":
        return
    with _bot_lock:
        running = []
        for pid, t in _bot_threads.items():
            if not t.is_alive():
                continue
            if db.get_profile_config(pid, "bot_status") != "running":
                continue
            raw = db.get_profile_config(pid, "assets") or "[]"
            try:
                if asset in json.loads(raw):
                    running.append(pid)
            except json.JSONDecodeError:
                pass
    for pid in running:
        cfg = _build_cfg_for_profile(pid)
        # Pull all TFs (same as today) and call process_asset
        process_asset(pid, asset, cfg)

def _build_cfg_for_profile(profile_id: int) -> dict:
    """Return the per-profile cfg dict the bot loop already uses."""
    cfg = {}
    for key in ("risk", "sizing"):
        raw = db.get_profile_config(profile_id, key) or "{}"
        try:
            cfg[key] = json.loads(raw)
        except json.JSONDecodeError:
            cfg[key] = {}
    return cfg
```

In `bot_loop(profile_id)`, the candle manager is now created lazily by `_refresh_candle_manager_assets`. The candle manager's `on_candle_close` callback points at `_on_candle_close_dispatch` (one global callback, not one per profile).

- [ ] **Step 4: Implement `_refresh_candle_manager_assets`**

```python
def _refresh_candle_manager_assets():
    global candle_mgr
    union = _union_assets()
    if not union:
        if candle_mgr is not None:
            try:
                candle_mgr.stop()
            except Exception:
                log.exception("Error stopping candle manager")
            candle_mgr = None
        return
    if candle_mgr is None:
        candle_mgr = _create_candle_manager(union)
        candle_mgr.start()
    else:
        candle_mgr.update_assets(union)
```

- [ ] **Step 5: Run test, expect pass.**

```bash
pytest tests/test_multi_bot.py -v
```

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(main): dispatch candle close to all running profiles"
```

### Task 4.4 — Lock execution by (profile_id, asset)

**Files:**
- Modify: `hyperliquid-bot/bot/executor.py`
- Test: `hyperliquid-bot/tests/test_multi_bot.py`

- [ ] **Step 1: Failing test**

```python
def test_locks_keyed_by_profile_and_asset():
    from bot import executor
    l1 = executor._get_asset_lock(1, "BTC")
    l2 = executor._get_asset_lock(2, "BTC")
    l3 = executor._get_asset_lock(1, "BTC")
    assert l1 is not l2          # different profile → different lock
    assert l1 is l3              # same key → same lock
```

- [ ] **Step 2: Run, expect failure.**

- [ ] **Step 3: Implement (Task 2.6 already laid the groundwork)**

In `bot/executor.py`:

```python
_open_locks: dict[tuple[int, str], threading.Lock] = {}
_locks_guard = threading.Lock()

def _get_asset_lock(profile_id: int, asset: str) -> threading.Lock:
    with _locks_guard:
        key = (profile_id, asset)
        if key not in _open_locks:
            _open_locks[key] = threading.Lock()
        return _open_locks[key]
```

`open_position(client, signal, size_usd, cfg, *, profile_id: int)` already exists from Task 2.6 — make sure the lock call passes `profile_id, signal["asset"]`.

- [ ] **Step 4: Run, commit.**

```bash
git commit -am "feat(executor): lock by (profile_id, asset)"
```

### Task 4.5 — Auto-resume per profile on `run.py` boot

**Files:**
- Modify: `hyperliquid-bot/run.py`

- [ ] **Step 1: Replace the existing auto-resume block**

Wherever `run.py` currently calls `start_bot()` after detecting `bot_status` was `running` or `paused`, replace with:

```python
from bot import db
import main as bot_main

def _auto_resume_bots():
    for p in db.list_profiles():
        status = db.get_profile_config(p["id"], "bot_status") or "stopped"
        if status in ("running", "paused"):
            try:
                bot_main.start_bot(profile_id=p["id"])
                if status == "paused":
                    bot_main.pause_bot(profile_id=p["id"])
            except Exception:
                log.exception("Auto-resume failed for profile %s", p["id"])

# Call this after init_db()
_auto_resume_bots()
```

- [ ] **Step 2: Smoke test**

```bash
sqlite3 hyperliquid-bot/bot_data.db "SELECT key, value FROM config WHERE key LIKE 'profile.%.bot_status'"
```

If Default was running, restart `run.py` — bot should auto-resume. Create a second profile, set it running, restart — both should resume.

- [ ] **Step 3: Commit**

```bash
git commit -am "feat(run): auto-resume bots for every profile with running/paused status"
```

### Task 4.6 — Bot control endpoints per profile

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py`

- [ ] **Step 1: Replace existing /api/bot/* endpoints (they used to operate on the single bot)**

```python
@app.route("/api/profiles/<int:pid>/bot/start", methods=["POST"])
def api_profile_bot_start(pid):
    if db.get_profile(pid) is None:
        return jsonify({"error": "not found"}), 404
    import main as bot_main
    bot_main.start_bot(profile_id=pid)
    return jsonify({"bot_status": "running"})

@app.route("/api/profiles/<int:pid>/bot/pause", methods=["POST"])
def api_profile_bot_pause(pid):
    if db.get_profile(pid) is None:
        return jsonify({"error": "not found"}), 404
    import main as bot_main
    bot_main.pause_bot(profile_id=pid)
    return jsonify({"bot_status": "paused"})

@app.route("/api/profiles/<int:pid>/bot/stop", methods=["POST"])
def api_profile_bot_stop(pid):
    if db.get_profile(pid) is None:
        return jsonify({"error": "not found"}), 404
    import main as bot_main
    bot_main.stop_bot(profile_id=pid)
    return jsonify({"bot_status": "stopped"})

# Legacy single-bot endpoints become thin wrappers over the active profile
@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    return api_profile_bot_start(g.profile_id)

@app.route("/api/bot/pause", methods=["POST"])
def api_bot_pause():
    return api_profile_bot_pause(g.profile_id)

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    return api_profile_bot_stop(g.profile_id)
```

- [ ] **Step 2: Smoke**

Open dashboard, two profiles in DB, switch between them and confirm Start/Pause/Stop only affects the active one.

- [ ] **Step 3: Commit**

```bash
git commit -am "feat(api): per-profile bot control endpoints"
```

---

## Phase 5 — Polish

Goal: real-time dropdown status dots, SocketIO payloads carry `profile_id`, logs UI defaults to active profile with "Show all" toggle.

### Task 5.1 — SocketIO payloads carry `profile_id`

**Files:**
- Modify: `hyperliquid-bot/dashboard/app.py` (every `socketio.emit(...)` call site)
- Modify: `hyperliquid-bot/bot/executor.py`, `hyperliquid-bot/main.py` (any socket emits inside the bot)

- [ ] **Step 1: Add `profile_id` to every emit**

`grep -rn "socketio.emit\|emit(" hyperliquid-bot --include="*.py"` and inject:

```python
socketio.emit("trade_update", {"profile_id": profile_id, **payload})
```

For globally-scoped emits (candle manager logs, migration notices), use `profile_id=None`.

- [ ] **Step 2: Client-side filter**

In `dashboard.js` socket handlers:

```js
function isForActive(payload) {
  return payload.profile_id == null || payload.profile_id === _activeProfileId;
}
socket.on('trade_update', (p) => { if (isForActive(p)) onTradeUpdate(p); });
socket.on('signal_update', (p) => { if (isForActive(p)) onSignalUpdate(p); });
// ...
```

- [ ] **Step 3: Bot status dot stays live across profiles**

Listen unfiltered to `bot_status`:

```js
socket.on('bot_status', (p) => {
  // update the dropdown dot for that profile regardless of active profile
  const item = document.querySelector(`#profileList li[data-id="${p.profile_id}"]`);
  if (item) {
    item.querySelector('.profile-status-dot').className =
      'profile-status-dot ' + p.bot_status;
  }
  if (p.profile_id === _activeProfileId) {
    document.getElementById('profileStatusDot').className =
      'profile-status-dot ' + p.bot_status;
    onBotStatusForActive(p);
  }
});
```

- [ ] **Step 4: Smoke**

Run two profile bots, watch the dropdown dots flip independently when starting/stopping each.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(socket): tag every event with profile_id, filter on client"
```

### Task 5.2 — Logs UI defaults to active profile + toggle

**Files:**
- Modify: `hyperliquid-bot/dashboard/templates/logs.html` (or wherever logs render — confirm with `ls dashboard/templates`)
- Modify: `hyperliquid-bot/dashboard/app.py` (`/api/logs`)
- Modify: `hyperliquid-bot/dashboard/static/js/dashboard.js`

- [ ] **Step 1: Update `/api/logs`**

```python
@app.route("/api/logs", methods=["GET"])
def api_logs():
    show_all = request.args.get("all") == "1"
    pid = None if show_all else g.profile_id
    return jsonify(db.get_logs(
        limit=int(request.args.get("limit", 200)),
        level=request.args.get("level"),
        profile_id=pid,
    ))
```

`db.get_logs(profile_id=None, ...)` returns everything; `db.get_logs(profile_id=X, ...)` returns rows where `profile_id IS NULL OR profile_id = X` (NULL = global candle manager logs).

- [ ] **Step 2: Add toggle to the logs page**

In the logs template, near the existing filter controls:

```html
<label>
  <input type="checkbox" id="logsShowAllProfiles">
  Mostrar todos os perfis
</label>
```

In `dashboard.js`:

```js
document.getElementById('logsShowAllProfiles')?.addEventListener('change', (e) => {
  loadLogs({all: e.target.checked});
});

function loadLogs(opts = {}) {
  const params = new URLSearchParams();
  if (opts.all) params.set('all', '1');
  fetch('/api/logs?' + params).then(r => r.json()).then(renderLogs);
}
```

- [ ] **Step 3: Smoke**

Two profiles with running bots → logs view filters to the active one by default; toggle shows everything (including candle manager `profile_id IS NULL` logs).

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(logs): default to active profile with show-all toggle"
```

### Task 5.3 — Verification matrix

Final smoke pass before declaring done.

- [ ] **DB sanity:** `sqlite3 bot_data.db "SELECT key FROM config WHERE key NOT LIKE 'profile.%' AND key NOT LIKE 'last_ts.%' AND key NOT LIKE '_migration%' AND key NOT IN ('selected_exchange','use_lighter_ws_candles','flask.secret_key');"` → empty.
- [ ] **Migration is idempotent:** restart `run.py`, M8 doesn't fire (marker check).
- [ ] **Two profiles in parallel:** create Profile B with a second Lighter account, enable a non-overlapping strategy, start both bots. Confirm both threads alive (`bot_main._bot_threads`), candle manager subscribed to union of assets, trades land in DB with the correct `profile_id`.
- [ ] **Delete guards:** trying to delete the last profile → 409; trying to delete a profile with an open trade → 409.
- [ ] **Rename:** rename Profile B to "Hedge", dropdown reflects it, no other state changes.
- [ ] **Duplicate account_index:** trying to create a second profile with Profile A's Lighter `account_index` → 409.
- [ ] **All tests pass:** `cd hyperliquid-bot && pytest -v`.

- [ ] **Commit a CHANGELOG entry**

Append a line to `CLAUDE.md` (under a new section if appropriate) documenting that the bot now supports multi-profile. Commit.

```bash
git commit -am "docs(CLAUDE): record multi-profile feature landing"
```

---

## Self-review notes

- **Spec coverage:** Phase 1 covers DB schema + M8. Phase 2 covers access layer for every callsite mentioned in the spec (db, executor, risk, manager, main, dashboard, backtest, scanner). Phase 3 covers profile CRUD endpoints, session, UI dropdown + modal. Phase 4 covers multi-bot execution: dict globals, per-profile start/stop, shared candle manager via `_refresh_candle_manager_assets`, `(profile_id, asset)` lock, auto-resume. Phase 5 covers SocketIO + logs UI per-profile, the two "default A" decisions from the spec's open questions. Profile deletion guards explicitly tested (Task 3.4 + Task 5.3).
- **Open spec questions answered:** candle buffer stays per-`LighterExchangeClient` (no work in this plan — Task 4.1's `_bot_clients` dict naturally produces N buffers, accepted in spec); logs default to active profile with toggle (Task 5.2).
- **No placeholders.** All endpoints have method, body, response code. All schema changes show the exact SQL. All JS handlers carry their full body.
- **Type consistency:** `profile_id: int` throughout. `_bot_threads`, `_bot_clients`, `_stop_events` keyed by `int`. Lock keys `(int, str)`. Frontend uses `===` (strict) comparisons against the int returned by Flask.
