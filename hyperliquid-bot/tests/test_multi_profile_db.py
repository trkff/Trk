import json
import time

import pytest

from bot import db


def _reset_conn():
    db._local.conn = None


def test_profiles_table_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(profiles)").fetchall()]
    assert set(cols) >= {
        "id", "name", "exchange",
        "lighter_account_index", "lighter_api_key_private", "lighter_api_key_index",
        "hyperliquid_address", "hyperliquid_secret",
        "created_at", "updated_at",
    }


def test_profile_id_columns_added(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    for table in ("trades", "signals", "logs"):
        cols = [r["name"] for r in db.get_conn().execute(f"PRAGMA table_info({table})").fetchall()]
        assert "profile_id" in cols, f"{table} missing profile_id"


def test_m8_creates_default_profile_and_namespaces_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    conn = db.get_conn()
    # Seed legacy config that should be namespaced under profile.1.*
    conn.executemany("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", [
        ("strategy.bb_stoch_btc_5m.enabled", "true"),
        ("strategy.bb_stoch_btc_5m.params", json.dumps({"bb_period": 20})),
        ("bot_status", "running"),
        ("assets", json.dumps(["BTC", "ETH"])),
        ("lighter.client_order_counter", "42"),
        ("account_address", "0xabc"),
        ("secret_key", "deadbeef"),
        ("selected_exchange", "lighter"),
        ("risk.max_positions", "3"),
        ("sizing.mode", "risk_pct"),
    ])
    conn.commit()
    # Force re-run of M8 by clearing the marker and any pre-created Default profile
    conn.execute("DELETE FROM config WHERE key = '_migration_multi_profile'")
    conn.execute("DELETE FROM profiles")
    conn.commit()
    db.migrate_db()

    # Default profile created with credentials populated from legacy globals
    row = conn.execute("SELECT * FROM profiles WHERE id = 1").fetchone()
    assert row is not None
    assert row["name"] == "Default"
    assert row["exchange"] == "lighter"
    assert row["hyperliquid_address"] == "0xabc"
    assert row["hyperliquid_secret"] == "deadbeef"

    # Per-profile keys are namespaced
    assert db.get_config("profile.1.strategy.bb_stoch_btc_5m.enabled") == "true"
    assert db.get_config("profile.1.strategy.bb_stoch_btc_5m.params") == json.dumps({"bb_period": 20})
    assert db.get_config("profile.1.bot_status") == "running"
    assert db.get_config("profile.1.assets") == json.dumps(["BTC", "ETH"])
    assert db.get_config("profile.1.lighter.client_order_counter") == "42"
    assert db.get_config("profile.1.risk.max_positions") == "3"
    assert db.get_config("profile.1.sizing.mode") == "risk_pct"

    # Old per-profile keys are deleted from the config table (DEFAULT_CONFIG
    # fallback in get_config would mask this, so query the table directly).
    def _row_exists(key):
        return conn.execute("SELECT 1 FROM config WHERE key = ?", (key,)).fetchone() is not None

    for old in (
        "strategy.bb_stoch_btc_5m.enabled",
        "strategy.bb_stoch_btc_5m.params",
        "bot_status",
        "assets",
        "lighter.client_order_counter",
        "risk.max_positions",
        "sizing.mode",
        # Legacy credential keys consumed into the Default profile row
        "account_address",
        "secret_key",
    ):
        assert not _row_exists(old), f"legacy key {old!r} should be deleted from config table"

    # Truly global keys are preserved
    assert db.get_config("selected_exchange") == "lighter"

    # Marker set
    assert db.get_config("_migration_multi_profile") == "done"


def test_m8_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.migrate_db()  # second call must be a no-op
    rows = db.get_conn().execute("SELECT COUNT(*) AS n FROM profiles").fetchone()
    assert rows["n"] >= 1


def test_list_create_update_delete_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
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
    assert db.get_profile(pid)["lighter_account_index"] == "999"
    # Update (rename + new creds)
    db.update_profile(pid, name="Hedge", credentials={"lighter_account_index": "1000"})
    assert db.get_profile(pid)["name"] == "Hedge"
    assert db.get_profile(pid)["lighter_account_index"] == "1000"
    # Delete
    db.delete_profile(pid)
    assert db.get_profile(pid) is None


def test_create_profile_rejects_duplicate_lighter_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.create_profile(name="A", exchange="lighter", credentials={"lighter_account_index": "111"})
    with pytest.raises(ValueError, match="lighter_account_index"):
        db.create_profile(name="B", exchange="lighter", credentials={"lighter_account_index": "111"})


def test_strategy_config_by_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.set_strategy_config("bb_stoch_btc_5m", True, {"x": 1}, profile_id=1)
    cfg = db.get_strategy_config("bb_stoch_btc_5m", profile_id=1)
    assert cfg["enabled"] is True and cfg["params"] == {"x": 1}
    # Different profile sees defaults
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    cfg2 = db.get_strategy_config("bb_stoch_btc_5m", profile_id=pid2)
    assert cfg2["enabled"] is False and cfg2["params"] == {}
    # And persisting on profile 2 doesn't affect profile 1
    db.set_strategy_config("bb_stoch_btc_5m", True, {"y": 2}, profile_id=pid2)
    cfg1 = db.get_strategy_config("bb_stoch_btc_5m", profile_id=1)
    assert cfg1["params"] == {"x": 1}


def test_profile_config_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.set_profile_config(1, "bot_status", "running")
    assert db.get_profile_config(1, "bot_status") == "running"
    # The underlying table sees the namespaced key
    row = db.get_conn().execute(
        "SELECT value FROM config WHERE key = ?", ("profile.1.bot_status",)
    ).fetchone()
    assert row is not None and row["value"] == "running"
    # Missing key returns None (no DEFAULT_CONFIG fallback for profile-scoped keys)
    assert db.get_profile_config(1, "missing_key") is None
    # set_profile_configs writes a batch
    db.set_profile_configs(1, {"sizing.mode": "risk_pct", "assets": '["BTC"]'})
    assert db.get_profile_config(1, "sizing.mode") == "risk_pct"
    assert db.get_profile_config(1, "assets") == '["BTC"]'


def test_delete_profile_cascades_namespaced_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    pid = db.create_profile(name="Tmp", exchange="lighter", credentials={})
    conn = db.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (f"profile.{pid}.bot_status", "running"),
    )
    conn.commit()
    db.delete_profile(pid)
    rows = conn.execute(
        "SELECT key FROM config WHERE key LIKE ?", (f"profile.{pid}.%",)
    ).fetchall()
    assert rows == []


def test_legacy_strategy_params_reachable_via_profile_1(tmp_path, monkeypatch):
    """Legacy `strategy.<name>.params` value survives M8 reachable under the new namespace."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        ("strategy.bb_stoch_btc_5m.params", json.dumps({"bb_period": 20, "stoch_os": 25})),
    )
    conn.commit()
    # Force re-run of M8b
    conn.execute("DELETE FROM config WHERE key = '_migration_multi_profile'")
    conn.commit()
    db.migrate_db()

    params = json.loads(db.get_config("profile.1.strategy.bb_stoch_btc_5m.params"))
    assert params == {"bb_period": 20, "stoch_os": 25}
