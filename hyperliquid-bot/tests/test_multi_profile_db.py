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
        "lighter_wallet_address", "lighter_public_key", "lighter_private_key",
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
    ):
        assert not _row_exists(old), f"legacy key {old!r} should be deleted from config table"

    # Legacy credential keys stay in `config` for now — Lighter exchange client
    # and is_configured() still read them as globals. Phase 4 deletes them.
    assert _row_exists("account_address"), "legacy HL creds should stay until Phase 4"
    assert _row_exists("secret_key"), "legacy HL creds should stay until Phase 4"

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
        credentials={"lighter_wallet_address": "999"},
    )
    assert pid > 1
    assert db.get_profile(pid)["name"] == "Conta 2"
    assert db.get_profile(pid)["lighter_wallet_address"] == "999"
    # Update (rename + new creds)
    db.update_profile(pid, name="Hedge", credentials={"lighter_wallet_address": "1000"})
    assert db.get_profile(pid)["name"] == "Hedge"
    assert db.get_profile(pid)["lighter_wallet_address"] == "1000"
    # Delete
    db.delete_profile(pid)
    assert db.get_profile(pid) is None


def test_create_profile_rejects_duplicate_lighter_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.create_profile(name="A", exchange="lighter", credentials={"lighter_wallet_address": "111"})
    with pytest.raises(ValueError, match="lighter_wallet_address"):
        db.create_profile(name="B", exchange="lighter", credentials={"lighter_wallet_address": "111"})


def _trade_dict(**overrides):
    base = {
        "asset": "BTC", "side": "long",
        "entry_price": 100.0, "size": 0.1,
        "entry_time": "2026-05-27T00:00:00",
        "ema9": None, "ema21": None, "rsi2": None,
        "volume": None, "atr": None, "funding_rate": None,
        "tp_price": None, "sl_price": None, "order_id": None,
        "strategy": "x",
    }
    base.update(overrides)
    return base


def test_trades_signals_logs_are_scoped_by_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    # Insert trades on both profiles
    db.insert_trade(_trade_dict(profile_id=1, asset="BTC", strategy="x"))
    db.insert_trade(_trade_dict(profile_id=pid2, asset="ETH",
                                entry_price=200.0, size=0.5, strategy="y"))
    p1 = db.get_open_trades(profile_id=1)
    p2 = db.get_open_trades(profile_id=pid2)
    assert len(p1) == 1 and p1[0]["asset"] == "BTC"
    assert len(p2) == 1 and p2[0]["asset"] == "ETH"
    # Default (no profile filter) returns both
    assert len(db.get_open_trades()) == 2


def test_insert_log_with_profile_id(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    db.insert_log("2026-05-27", "INFO", "bot", "global log")  # no profile -> NULL
    db.insert_log("2026-05-27", "INFO", "bot", "p1 log", profile_id=1)
    db.insert_log("2026-05-27", "INFO", "bot", "p2 log", profile_id=pid2)
    # profile_id=1 returns global (NULL) + own
    p1_msgs = {r["message"] for r in db.get_logs(profile_id=1)}
    assert {"global log", "p1 log"} <= p1_msgs
    assert "p2 log" not in p1_msgs
    # profile_id=None returns everything
    all_msgs = {r["message"] for r in db.get_logs(profile_id=None)}
    assert {"global log", "p1 log", "p2 log"} <= all_msgs


def test_coi_counter_is_per_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()
    db.set_lighter_coi_counter(7, profile_id=1)
    pid2 = db.create_profile(name="P2", exchange="lighter", credentials={})
    db.set_lighter_coi_counter(99, profile_id=pid2)
    assert db.get_lighter_coi_counter(profile_id=1) == 7
    assert db.get_lighter_coi_counter(profile_id=pid2) == 99
    # Missing → 0
    pid3 = db.create_profile(name="P3", exchange="lighter", credentials={})
    assert db.get_lighter_coi_counter(profile_id=pid3) == 0


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
