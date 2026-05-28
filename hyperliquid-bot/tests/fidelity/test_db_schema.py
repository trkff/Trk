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
