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
