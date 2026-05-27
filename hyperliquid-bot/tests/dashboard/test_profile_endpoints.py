import json

import pytest

from bot import db
from dashboard.app import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    a, _ = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def test_list_profiles_returns_default(client):
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    data = resp.get_json()
    names = {p["name"] for p in data}
    assert "Default" in names
    # No private keys leak
    for p in data:
        assert "lighter_private_key" not in p
        assert "hyperliquid_secret" not in p
    # bot_status field is set
    assert all("bot_status" in p for p in data)


def test_list_profiles_marks_active_one(client):
    # Default starts active (session bootstraps to the first profile)
    resp = client.get("/api/profiles")
    data = resp.get_json()
    active = [p for p in data if p.get("is_active")]
    assert len(active) == 1
    assert active[0]["id"] == 1


def test_create_profile(client):
    resp = client.post("/api/profiles", json={
        "name": "Hedge", "exchange": "lighter",
        "credentials": {"lighter_wallet_address": "0xdeadbeef"},
    })
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Hedge"
    assert body["id"] >= 2


def test_create_profile_rejects_empty_name(client):
    resp = client.post("/api/profiles", json={"name": "  ", "exchange": "lighter"})
    assert resp.status_code == 400


def test_create_profile_rejects_unknown_exchange(client):
    resp = client.post("/api/profiles", json={"name": "X", "exchange": "ftx"})
    assert resp.status_code == 400


def test_create_profile_rejects_duplicate_wallet(client):
    client.post("/api/profiles", json={
        "name": "A", "exchange": "lighter",
        "credentials": {"lighter_wallet_address": "0xshared"},
    })
    resp = client.post("/api/profiles", json={
        "name": "B", "exchange": "lighter",
        "credentials": {"lighter_wallet_address": "0xshared"},
    })
    assert resp.status_code == 409


def test_patch_profile_rename(client):
    create = client.post("/api/profiles", json={
        "name": "Old", "exchange": "lighter",
    })
    pid = create.get_json()["id"]
    resp = client.patch(f"/api/profiles/{pid}", json={"name": "New"})
    assert resp.status_code == 200
    assert db.get_profile(pid)["name"] == "New"


def test_patch_profile_404_when_missing(client):
    resp = client.patch("/api/profiles/9999", json={"name": "x"})
    assert resp.status_code == 404


def test_delete_blocks_last_profile(client):
    resp = client.delete("/api/profiles/1")
    assert resp.status_code == 409
    body = resp.get_json()
    assert "last profile" in body["error"].lower()


def test_delete_blocks_with_open_trade(client):
    pid = db.create_profile(name="X", exchange="lighter", credentials={})
    db.insert_trade({
        "profile_id": pid, "asset": "BTC", "side": "long",
        "entry_price": 1.0, "size": 0.1,
        "entry_time": "2026-05-27T00:00:00", "strategy": "x",
        "ema9": None, "ema21": None, "rsi2": None, "volume": None,
        "atr": None, "funding_rate": None, "tp_price": None, "sl_price": None,
        "order_id": None,
    })
    resp = client.delete(f"/api/profiles/{pid}")
    assert resp.status_code == 409
    assert "open" in resp.get_json()["error"].lower()


def test_delete_profile_succeeds_when_safe(client):
    pid = db.create_profile(name="Tmp", exchange="lighter", credentials={})
    resp = client.delete(f"/api/profiles/{pid}")
    assert resp.status_code == 204
    assert db.get_profile(pid) is None


def test_activate_profile_persists_in_session(client):
    pid = db.create_profile(name="Hedge", exchange="lighter", credentials={})
    resp = client.post(f"/api/profiles/{pid}/activate")
    assert resp.status_code == 200
    assert resp.get_json()["active_profile_id"] == pid
    # Subsequent request sees the active profile reflected
    db.set_profile_config(pid, "bot_status", "paused")
    db.set_profile_config(1, "bot_status", "running")
    overview = client.get("/api/overview").get_json()
    assert overview["bot_status"] == "paused"


def test_activate_profile_404_when_missing(client):
    resp = client.post("/api/profiles/9999/activate")
    assert resp.status_code == 404


def test_patch_can_clear_credential_with_null(client):
    """Sending null for a credential field overwrites the stored value with NULL
    (vs. omitting the field, which preserves it). Lets the dashboard "Limpar"
    button actually wipe a wallet/key.
    """
    pid = db.create_profile(name="X", exchange="lighter", credentials={
        "lighter_wallet_address": "0xWALLET",
        "lighter_public_key": "PUB",
        "lighter_private_key": "PRIV",
    })
    # Omitting a field preserves it
    client.patch(f"/api/profiles/{pid}", json={"credentials": {"lighter_public_key": "NEWPUB"}})
    p = db.get_profile(pid)
    assert p["lighter_wallet_address"] == "0xWALLET"
    assert p["lighter_public_key"] == "NEWPUB"
    # Sending null clears it
    client.patch(f"/api/profiles/{pid}", json={"credentials": {"lighter_wallet_address": None}})
    p = db.get_profile(pid)
    assert p["lighter_wallet_address"] is None
    assert p["lighter_public_key"] == "NEWPUB"  # untouched
    assert p["lighter_private_key"] == "PRIV"  # untouched
