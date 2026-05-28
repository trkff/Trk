"""Integration test for the full run_check orchestrator."""
import time

import numpy as np
import pandas as pd
import pytest

from bot import db
from bot.backtest import csv_loader


def _reset_conn():
    db._local.conn = None


@pytest.fixture
def seeded_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    _reset_conn()
    db.init_db()

    # Synthetic 5m CSV ending close to now (so the period_end_ms clamp lands inside)
    n = 600
    now_ms = int(time.time() * 1000)
    ts_ms = now_ms - np.arange(n)[::-1] * 300_000
    rng = np.random.default_rng(7)
    rets = rng.normal(0, 0.005, n)
    closes = 50_000 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({
        "timestamp": ts_ms.astype(np.int64),
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
    assert row["live_signals"] == 0   # no live signals seeded in this test


def test_run_check_clamps_period_end_to_closed_candle(seeded_env):
    from bot.fidelity.checker import run_check
    run_id = run_check(strategy="bb_stoch", asset="BTC", days=30, profile_id=1)
    row = db.get_fidelity_run(run_id)
    now_ms = int(time.time() * 1000)
    # 5m tf → period_end_ms must be at most now - 300_000
    assert row["period_end_ms"] <= now_ms - 300_000 + 1_000   # 1s slack
