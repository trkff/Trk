"""Smoke tests for the multi-profile bot orchestration (Phase 4)."""

import json
import threading
import time
from unittest.mock import patch

import pytest

from bot import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.conn = None
    db.init_db()
    return db


def test_union_assets_combines_running_profiles(fresh_db, monkeypatch):
    import main as bot_main

    pid2 = fresh_db.create_profile(name="P2", exchange="lighter", credentials={})
    # Asset universe é dirigido pelas estratégias enabled — habilita BTC em
    # profile 1 e ETH+SOL em profile 2 via instâncias hardcoded.
    fresh_db.set_strategy_config("bb_stoch_btc_5m", True, {}, profile_id=1)
    fresh_db.set_strategy_config("bb_stoch_eth_5m", True, {}, profile_id=pid2)
    fresh_db.set_strategy_config("bb_stoch_sol_5m", True, {}, profile_id=pid2)
    # _union_assets filters by status in (running, starting) — set both running
    fresh_db.set_profile_config(1, "bot_status", "running")
    fresh_db.set_profile_config(pid2, "bot_status", "running")

    # Pretend both bots are running (use real alive threads that just sleep)
    stop_a = threading.Event()
    stop_b = threading.Event()
    threads = {
        1: threading.Thread(target=stop_a.wait, daemon=True),
        pid2: threading.Thread(target=stop_b.wait, daemon=True),
    }
    for t in threads.values():
        t.start()
    try:
        with bot_main._bot_lock:
            bot_main._bot_threads.update(threads)
        assets = bot_main._union_assets()
        # Universo é a união dos assets pinned pelas estratégias enabled em
        # cada perfil running (não há mais lista global monitored_assets).
        assert "BTC" in assets
        assert "ETH" in assets
        assert "SOL" in assets
    finally:
        stop_a.set(); stop_b.set()
        with bot_main._bot_lock:
            bot_main._bot_threads.clear()
        for t in threads.values():
            t.join(timeout=2)


def test_on_candle_close_dispatches_only_to_running_profiles(fresh_db, monkeypatch):
    import main as bot_main

    pid2 = fresh_db.create_profile(name="P2", exchange="lighter", credentials={})
    # Profile 1: só BTC (uma instância pinned). Profile 2: BTC e ETH.
    fresh_db.set_strategy_config("bb_stoch_btc_5m", True, {}, profile_id=1)
    fresh_db.set_strategy_config("bb_stoch_btc_5m", True, {}, profile_id=pid2)
    fresh_db.set_strategy_config("bb_stoch_eth_5m", True, {}, profile_id=pid2)
    fresh_db.set_profile_config(1, "bot_status", "running")
    fresh_db.set_profile_config(pid2, "bot_status", "running")

    seen: list[tuple[int, str]] = []

    def fake_process(asset, cfg, *a, profile_id=1, **kw):
        seen.append((profile_id, asset))

    # Each running profile needs a client + alive thread for dispatch to fire.
    class _StubClient:
        def disconnect(self):
            pass

    stop_a = threading.Event()
    stop_b = threading.Event()
    threads = {
        1: threading.Thread(target=stop_a.wait, daemon=True),
        pid2: threading.Thread(target=stop_b.wait, daemon=True),
    }
    for t in threads.values():
        t.start()

    monkeypatch.setattr(bot_main, "process_asset", fake_process)

    try:
        with bot_main._bot_lock:
            bot_main._bot_threads.update(threads)
            bot_main._bot_clients[1] = _StubClient()
            bot_main._bot_clients[pid2] = _StubClient()

        bot_main._on_candle_close_dispatch("BTC", "5m")
        bot_main._on_candle_close_dispatch("ETH", "5m")
        bot_main._on_candle_close_dispatch("SOL", "5m")  # not in any profile
        bot_main._on_candle_close_dispatch("BTC", "15m")  # gated, ignored

        # Dispatch fans out via the thread pool with >1 profile — drain it.
        deadline = time.time() + 5
        while len(seen) < 3 and time.time() < deadline:
            time.sleep(0.05)

        assert (1, "BTC") in seen
        assert (pid2, "BTC") in seen
        assert (pid2, "ETH") in seen
        assert (1, "ETH") not in seen
        assert not any(a == "SOL" for _, a in seen)
        assert not any(tf for tf in seen if "15m" in tf)
    finally:
        stop_a.set(); stop_b.set()
        with bot_main._bot_lock:
            bot_main._bot_threads.clear()
            bot_main._bot_clients.clear()
        for t in threads.values():
            t.join(timeout=2)


def test_on_candle_close_skips_paused_profiles(fresh_db, monkeypatch):
    import main as bot_main

    fresh_db.set_profile_config(1, "assets", json.dumps(["BTC"]))
    fresh_db.set_profile_config(1, "bot_status", "paused")

    seen: list = []
    monkeypatch.setattr(bot_main, "process_asset",
                        lambda *a, **kw: seen.append(kw.get("profile_id")))

    stop_evt = threading.Event()
    t = threading.Thread(target=stop_evt.wait, daemon=True)
    t.start()
    try:
        with bot_main._bot_lock:
            bot_main._bot_threads[1] = t
            bot_main._bot_clients[1] = object()
        bot_main._on_candle_close_dispatch("BTC", "5m")
        assert seen == []
    finally:
        stop_evt.set()
        with bot_main._bot_lock:
            bot_main._bot_threads.clear()
            bot_main._bot_clients.clear()
        t.join(timeout=2)


def test_lock_is_keyed_by_profile_and_asset():
    """Two profiles can grab the same asset's lock without contention."""
    from bot import executor

    l1 = executor._get_asset_lock(1, "BTC")
    l2 = executor._get_asset_lock(2, "BTC")
    l3 = executor._get_asset_lock(1, "BTC")
    assert l1 is not l2
    assert l1 is l3
