"""Tests for /fidelity page + API endpoints."""
import pytest

from bot import db
from dashboard.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    app, _ = create_app()
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
