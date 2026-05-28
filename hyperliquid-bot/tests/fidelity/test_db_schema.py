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
    db.migrate_db()
    cols = [r["name"] for r in db.get_conn().execute("PRAGMA table_info(signals)").fetchall()]
    assert cols.count("indicators_json") == 1


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
