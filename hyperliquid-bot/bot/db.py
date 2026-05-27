"""
SQLite database layer for the Hyperliquid scalping bot.
Tables: trades, config, logs, signals.
All configuration is stored in SQLite (no .env files).
"""

import sqlite3
import threading
import json
import time
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path(__file__).resolve().parent.parent / "bot_data.db"

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL,
        side TEXT NOT NULL,
        entry_price REAL NOT NULL,
        exit_price REAL,
        size REAL NOT NULL,
        pnl REAL,
        pnl_pct REAL,
        status TEXT NOT NULL DEFAULT 'open',
        entry_time TEXT NOT NULL,
        exit_time TEXT,
        ema9 REAL,
        ema21 REAL,
        rsi2 REAL,
        volume REAL,
        atr REAL,
        funding_rate REAL,
        tp_price REAL,
        sl_price REAL,
        order_id TEXT,
        strategy TEXT DEFAULT 'mean_reversion'
    );

    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        level TEXT NOT NULL,
        module TEXT NOT NULL,
        message TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        asset TEXT NOT NULL,
        side TEXT NOT NULL,
        executed INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        ema9 REAL,
        ema21 REAL,
        rsi2 REAL,
        volume REAL,
        volume_avg REAL,
        atr REAL,
        funding_rate REAL,
        strategy_name TEXT DEFAULT 'mean_reversion'
    );

    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        exchange TEXT NOT NULL DEFAULT 'lighter',
        lighter_wallet_address TEXT,
        lighter_public_key TEXT,
        lighter_private_key TEXT,
        hyperliquid_address TEXT,
        hyperliquid_secret TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
    CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);
    CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
    CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
    CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
    CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
    """)
    conn.commit()
    migrate_db()


_MR_LEGACY_KEYS = {
    "ema_fast": 9, "ema_slow": 21, "rsi_period": 2,
    "atr_period": 14, "volume_avg_period": 20,
    "rsi_oversold": 15, "rsi_overbought": 85,
    "funding_rate_limit": 0.0005, "volume_multiplier": 1.3,
    "tp_atr_multiplier": 1.5, "sl_atr_multiplier": 1.0,
}


# M8 — keys that stay GLOBAL (not moved into profile.<id>.* namespace)
_M8_GLOBAL_KEYS = {
    "selected_exchange", "use_lighter_ws_candles", "flask.secret_key",
    "_migration_strategy_names_5m", "_migration_dynamic_strategy_5m",
    "_migration_multi_profile",
    # Legacy credential keys consumed by M8 to seed the Default profile and then deleted
    "account_address", "secret_key",
    "lighter_account_index", "lighter_api_key_private", "lighter_api_key_index",
}

# M8 — key prefixes/exact-keys that ARE per-profile and must be namespaced
_M8_PROFILE_PREFIXES = (
    "strategy.",
    "risk.",
    "sizing.",
)
_M8_PROFILE_EXACT_KEYS = {
    "bot_status",
    "assets",
    "lighter.client_order_counter",
}


def migrate_db():
    """Apply all schema and data migrations (safe to run on existing DBs)."""
    conn = get_conn()

    # M1 — add strategy_name column to signals
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()]
    if "strategy_name" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN strategy_name TEXT DEFAULT 'mean_reversion'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_name)")
        conn.commit()

    # M4 — add strategy column to trades
    trade_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if "strategy" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN strategy TEXT DEFAULT 'mean_reversion'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy)")
        conn.commit()

    # M3 — add fees and funding columns to trades
    trade_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if "fees" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN fees REAL DEFAULT 0.0")
        conn.commit()
    if "funding" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN funding REAL DEFAULT 0.0")
        conn.commit()

    # M5 — add signal_price column to trades
    trade_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if "signal_price" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN signal_price REAL")
        conn.commit()

    # M2 — migrate indicator period params from global config to mean_reversion params
    placeholders = ",".join("?" * len(_MR_LEGACY_KEYS))
    old_rows = conn.execute(
        f"SELECT key, value FROM config WHERE key IN ({placeholders})",
        list(_MR_LEGACY_KEYS.keys()),
    ).fetchall()
    if old_rows:
        params_row = conn.execute(
            "SELECT value FROM config WHERE key = 'strategy.mean_reversion.params'"
        ).fetchone()
        current_params = {}
        if params_row:
            try:
                current_params = json.loads(params_row["value"])
            except json.JSONDecodeError:
                pass
        for row in old_rows:
            if row["key"] not in current_params:
                try:
                    v = float(row["value"])
                    current_params[row["key"]] = int(v) if v == int(v) else v
                except (ValueError, TypeError):
                    current_params[row["key"]] = row["value"]
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            ("strategy.mean_reversion.params", json.dumps(current_params), json.dumps(current_params)),
        )
        conn.execute(
            f"DELETE FROM config WHERE key IN ({placeholders})",
            list(_MR_LEGACY_KEYS.keys()),
        )
        conn.commit()

    # M6 — rename instâncias hardcoded para incluir _5m no nome (alinha com multi-TF)
    _migrate_legacy_strategy_names_to_5m(conn)

    # M7 — rename instâncias dinâmicas legadas (criadas pelo scanner antes do multi-TF)
    _migrate_legacy_dynamic_instances_to_5m(conn)

    # M8a — add profile_id columns to trades/signals/logs
    for table in ("trades", "signals", "logs"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "profile_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN profile_id INTEGER DEFAULT 1")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_profile ON {table}(profile_id)")
            conn.commit()

    # M8b — multi-profile support: seed Default profile + namespace per-profile keys
    _migrate_to_multi_profile(conn)

    # M8c — fix profiles credential column names (renamed from the
    # SDK-style placeholders shipped in M8b to the actual user-input field
    # names this codebase uses: lighter_wallet_address/public_key/private_key).
    _fix_profile_credential_columns(conn)

    # Safety sweep — re-runs on every init_db (no marker gate). Catches
    # stray legacy keys an older process may have written into the global
    # namespace after M8b ran (e.g. a stale pm2-managed bot still calling
    # the old set_strategy_config that wrote `strategy.<name>.enabled`
    # directly). Idempotent on a clean DB; no-op when nothing matches.
    _sweep_stray_profile_keys(conn)


_LEGACY_STRATEGY_NAMES_TO_5M = [
    "bb_reversion_btc", "bb_reversion_eth", "bb_reversion_sol",
    "bb_stoch_btc", "bb_stoch_eth", "bb_stoch_sol", "bb_stoch_zec", "bb_stoch_ton",
    "stoch_scalp_xau", "stoch_scalp_wti", "stoch_scalp_ton",
    "ema_cross_hype", "ema_cross_lit",
    "rsi_scalp_btc", "rsi_scalp_eth", "rsi_scalp_sol", "rsi_scalp_ton",
    "bb_rsi_btc", "bb_rsi_eth", "bb_rsi_sol", "bb_rsi_zec", "bb_rsi_ton",
    "macd_cross_btc", "macd_cross_eth", "macd_cross_sol",
    "williams_r_xau", "williams_r_wti", "williams_r_ton",
]


def _migrate_legacy_strategy_names_to_5m(conn):
    """Migration one-shot: renomeia instâncias legadas para incluir _5m no sufixo.
    Move config keys (strategy.<old>.params, .enabled, .scanner_metrics) → <new>,
    e atualiza trades.strategy + signals.strategy_name. Idempotente via marker."""
    marker = conn.execute(
        "SELECT value FROM config WHERE key = '_migration_strategy_names_5m'"
    ).fetchone()
    if marker and marker["value"] == "done":
        return

    suffixes = (".params", ".enabled", ".scanner_metrics")
    for old in _LEGACY_STRATEGY_NAMES_TO_5M:
        new = f"{old}_5m"
        for suf in suffixes:
            ok = f"strategy.{old}{suf}"
            nk = f"strategy.{new}{suf}"
            # Se já existe new, não mexe (segurança)
            existing_new = conn.execute("SELECT 1 FROM config WHERE key = ?", (nk,)).fetchone()
            if existing_new:
                continue
            row = conn.execute("SELECT value FROM config WHERE key = ?", (ok,)).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?)",
                    (nk, row["value"]),
                )
                conn.execute("DELETE FROM config WHERE key = ?", (ok,))
        # Atualiza trades e signals
        conn.execute("UPDATE trades SET strategy = ? WHERE strategy = ?", (new, old))
        conn.execute("UPDATE signals SET strategy_name = ? WHERE strategy_name = ?", (new, old))

    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        ("_migration_strategy_names_5m", "done", "done"),
    )
    conn.commit()


# Prefixos conhecidos de estratégias, ordenados por len DESC para longest-match
# (bb_reversion antes de bb_stoch antes de bb_rsi).
_KNOWN_PREFIXES = sorted([
    "bb_reversion", "bb_stoch", "bb_rsi",
    "stoch_scalp", "ema_cross", "rsi_scalp",
    "macd_cross", "williams_r",
], key=lambda x: -len(x))

_SUPPORTED_TFS_FOR_MIGRATION = {"5m", "15m", "30m", "1h"}


def _migrate_legacy_dynamic_instances_to_5m(conn):
    """Migration one-shot: renomeia instâncias dinâmicas legadas (criadas pelo scanner
    antes do multi-TF) adicionando `_5m` após o asset.

    Casos:
      - `bb_rsi_sol_60_26_5` (com tag, sem TF) → `bb_rsi_sol_5m_60_26_5`
      - `bb_stoch_xau` (sem tag, sem TF — instância dinâmica sem hardcoded equivalente) → `bb_stoch_xau_5m`
      - `bb_stoch_btc_5m` (já novo formato) → ignorado
      - `bb_stoch_btc_15m_57_36` (já novo formato) → ignorado

    Idempotente via marker. Roda DEPOIS de M6 (que cobre os 28 hardcoded)."""
    marker = conn.execute(
        "SELECT value FROM config WHERE key = '_migration_dynamic_strategy_5m'"
    ).fetchone()
    if marker and marker["value"] == "done":
        return

    rows = conn.execute(
        "SELECT key FROM config WHERE key LIKE 'strategy.%.params'"
    ).fetchall()

    renames: list[tuple[str, str]] = []
    for row in rows:
        key = row["key"]
        inst = key[len("strategy."):-len(".params")]
        # Identifica prefixo (longest-first)
        prefix = None
        for p in _KNOWN_PREFIXES:
            if inst.startswith(p + "_"):
                prefix = p
                break
        if not prefix:
            continue
        rest = inst[len(prefix) + 1:]
        parts = rest.split("_")
        asset = parts[0]
        # Se o token logo após o asset já é um TF, está no novo formato → skip
        if len(parts) >= 2 and parts[1] in _SUPPORTED_TFS_FOR_MIGRATION:
            continue
        # Legacy: construir novo nome com _5m logo após o asset
        if len(parts) == 1:
            new_inst = f"{prefix}_{asset}_5m"
        else:
            tag = "_".join(parts[1:])
            new_inst = f"{prefix}_{asset}_5m_{tag}"
        renames.append((inst, new_inst))

    suffixes = (".params", ".enabled", ".scanner_metrics")
    for old, new in renames:
        for suf in suffixes:
            ok = f"strategy.{old}{suf}"
            nk = f"strategy.{new}{suf}"
            existing_new = conn.execute("SELECT 1 FROM config WHERE key = ?", (nk,)).fetchone()
            if existing_new:
                continue
            r = conn.execute("SELECT value FROM config WHERE key = ?", (ok,)).fetchone()
            if r:
                conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", (nk, r["value"]))
                conn.execute("DELETE FROM config WHERE key = ?", (ok,))
        conn.execute("UPDATE trades SET strategy = ? WHERE strategy = ?", (new, old))
        conn.execute("UPDATE signals SET strategy_name = ? WHERE strategy_name = ?", (new, old))

    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        ("_migration_dynamic_strategy_5m", "done", "done"),
    )
    conn.commit()


def _is_m8_profile_key(key: str) -> bool:
    """Return True if `key` is per-profile (must be namespaced by M8b)."""
    if key in _M8_GLOBAL_KEYS:
        return False
    if key.startswith("profile."):
        return False
    if key.startswith("last_ts."):
        return False
    if key in _M8_PROFILE_EXACT_KEYS:
        return True
    return any(key.startswith(p) for p in _M8_PROFILE_PREFIXES)


def _migrate_to_multi_profile(conn):
    """M8b — seed Default profile (id=1) and namespace per-profile config keys.

    Idempotent via the `_migration_multi_profile=done` marker.
    """
    marker = conn.execute(
        "SELECT value FROM config WHERE key = '_migration_multi_profile'"
    ).fetchone()
    if marker and marker["value"] == "done":
        return

    now = int(time.time() * 1000)

    # 1. Create Default profile from legacy global credentials, if not present.
    # The legacy keys in `config` use the names: lighter_wallet_address,
    # lighter_public_key, lighter_private_key, account_address, secret_key.
    existing = conn.execute("SELECT id FROM profiles WHERE id = 1").fetchone()
    if existing is None:
        cred_keys = (
            "account_address", "secret_key",
            "lighter_wallet_address", "lighter_public_key", "lighter_private_key",
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
               (id, name, exchange, lighter_wallet_address, lighter_public_key,
                lighter_private_key, hyperliquid_address, hyperliquid_secret,
                created_at, updated_at)
               VALUES (1, 'Default', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exchange,
                creds.get("lighter_wallet_address"),
                creds.get("lighter_public_key"),
                creds.get("lighter_private_key"),
                creds.get("account_address"),
                creds.get("secret_key"),
                now, now,
            ),
        )

    # 2. Backfill profile_id=1 on rows inserted before the DEFAULT was wired
    for table in ("trades", "signals", "logs"):
        conn.execute(f"UPDATE {table} SET profile_id = 1 WHERE profile_id IS NULL")

    # 3. Namespace per-profile config keys → profile.1.<key>
    keys_to_move = []
    for row in conn.execute("SELECT key, value FROM config").fetchall():
        if _is_m8_profile_key(row["key"]):
            keys_to_move.append((row["key"], row["value"]))
    for k, v in keys_to_move:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"profile.1.{k}", v),
        )
        conn.execute("DELETE FROM config WHERE key = ?", (k,))

    # 4. Legacy credential keys stay in `config` for now — `is_configured()` and
    # the current Lighter exchange client still read them as globals. Phase 4
    # will switch those reads to the Default profile row, after which a
    # follow-up migration can drop the keys safely.

    # 5. Set marker
    conn.execute(
        "INSERT INTO config (key, value) VALUES ('_migration_multi_profile', 'done') "
        "ON CONFLICT(key) DO UPDATE SET value = 'done'"
    )
    conn.commit()


def _sweep_stray_profile_keys(conn):
    """Move any post-M8 stray per-profile config keys into `profile.1.*`.

    Runs on every `init_db()` (no marker gate). Idempotent: rows are only
    moved when they pass `_is_m8_profile_key` AND `profile.1.<key>` is not
    already populated. This is a defensive sweep — the normal path is for
    the running code to write to the namespaced keys directly.
    """
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    moved = 0
    for row in rows:
        k = row["key"]
        if not _is_m8_profile_key(k):
            continue
        new_key = f"profile.1.{k}"
        existing = conn.execute(
            "SELECT value FROM config WHERE key = ?", (new_key,)
        ).fetchone()
        if existing is None:
            # No namespaced copy yet — promote the stray to be it
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)",
                (new_key, row["value"]),
            )
        # Drop the stray either way (the namespaced version is authoritative)
        conn.execute("DELETE FROM config WHERE key = ?", (k,))
        moved += 1
    if moved:
        conn.commit()


_M8C_COLUMN_RENAMES = (
    ("lighter_account_index",  "lighter_wallet_address"),
    ("lighter_api_key_index",  "lighter_public_key"),
    ("lighter_api_key_private","lighter_private_key"),
)


def _fix_profile_credential_columns(conn):
    """M8c — rename the wrong column names shipped in M8b and backfill the
    Default profile credentials from the legacy global config keys.

    Idempotent via `_migration_fix_profile_cred_cols=done`.
    """
    marker = conn.execute(
        "SELECT value FROM config WHERE key = '_migration_fix_profile_cred_cols'"
    ).fetchone()
    if marker and marker["value"] == "done":
        return

    cols = {r[1] for r in conn.execute("PRAGMA table_info(profiles)").fetchall()}
    # 1. Rename old columns to new names if necessary (SQLite >= 3.25)
    for old, new in _M8C_COLUMN_RENAMES:
        if old in cols and new not in cols:
            try:
                conn.execute(f"ALTER TABLE profiles RENAME COLUMN {old} TO {new}")
            except sqlite3.OperationalError:
                # SQLite too old for RENAME COLUMN — fall back to ADD + UPDATE + leave old col.
                conn.execute(f"ALTER TABLE profiles ADD COLUMN {new} TEXT")
                conn.execute(f"UPDATE profiles SET {new} = {old}")
            cols.add(new)
            cols.discard(old)
        elif old in cols and new in cols:
            # Both exist (someone re-added one). Move data into new and stop.
            conn.execute(f"UPDATE profiles SET {new} = COALESCE({new}, {old})")
            # Old column is left in place; harmless.
            cols.discard(old)

    # 2. Ensure all three target columns exist on existing DBs that never had M8b
    for _, new in _M8C_COLUMN_RENAMES:
        if new not in cols:
            conn.execute(f"ALTER TABLE profiles ADD COLUMN {new} TEXT")
            cols.add(new)

    # 3. Backfill Default profile from legacy global keys if its row is empty.
    row = conn.execute(
        "SELECT lighter_wallet_address, lighter_public_key, lighter_private_key, "
        "hyperliquid_address, hyperliquid_secret FROM profiles WHERE id = 1"
    ).fetchone()
    if row is not None:
        # Pull whatever still lives in the config table
        def _cfg(k):
            r = conn.execute("SELECT value FROM config WHERE key = ?", (k,)).fetchone()
            return r["value"] if r else None
        updates = {}
        for col, legacy_key in (
            ("lighter_wallet_address", "lighter_wallet_address"),
            ("lighter_public_key",     "lighter_public_key"),
            ("lighter_private_key",    "lighter_private_key"),
            ("hyperliquid_address",    "account_address"),
            ("hyperliquid_secret",     "secret_key"),
        ):
            if not row[col]:
                v = _cfg(legacy_key)
                if v:
                    updates[col] = v
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE profiles SET {sets} WHERE id = 1", list(updates.values()))

    conn.execute(
        "INSERT INTO config (key, value) VALUES ('_migration_fix_profile_cred_cols', 'done') "
        "ON CONFLICT(key) DO UPDATE SET value = 'done'"
    )
    conn.commit()


# ── Config helpers ──────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "account_address": "",
    "secret_key": "",
    "selected_exchange": "hyperliquid",
    "lighter_wallet_address": "",
    "lighter_public_key": "",
    "lighter_private_key": "",
    "use_testnet": "true",
    "monitored_assets": '["BTC","ETH","SOL"]',
    "risk_pct_per_trade": "1.0",
    "max_positions": "2",
    "max_daily_loss_pct": "5.0",
    "debug_logging": "false",
    "bot_status": "stopped",
    "slippage": "0.005",
    "strategy.mean_reversion.enabled": "true",
    "strategy.mean_reversion.params": '{"ema_fast": 9, "ema_slow": 21, "rsi_period": 2, "atr_period": 14, "volume_avg_period": 20, "rsi_oversold": 15, "rsi_overbought": 85, "funding_rate_limit": 0.0005, "volume_multiplier": 1.3, "tp_atr_multiplier": 1.5, "sl_atr_multiplier": 1.0}',
    "strategy.funding_arb.enabled": "false",
    "strategy.funding_arb.params": '{"funding_long_threshold": 0.001, "funding_short_threshold": 0.001, "min_volume_mult": 1.2}',
    "strategy.order_flow.enabled": "false",
    "strategy.order_flow.params": '{"delta_threshold": 0.62, "lookback_periods": 3, "min_volume_mult": 1.5, "funding_limit": 0.001}',
    "strategy.triple_ema_1h.enabled": "false",
    "strategy.triple_ema_1h.params": '{"pullback_threshold": 0.003, "vol_multiplier": 1.2, "tp_atr_multiplier": 2.0, "funding_rate_limit": 0.0005}',
    "strategy.momentum_macd_1h.enabled": "false",
    "strategy.momentum_macd_1h.params": '{"vol_multiplier": 1.2, "tp_atr_multiplier": 3.0, "sl_atr_multiplier": 1.5, "funding_rate_limit": 0.0005}',
    "strategy.ema200_rsi_1h.enabled": "false",
    "strategy.ema200_rsi_1h.params": '{"rsi_period": 14, "vol_multiplier": 1.2, "tp_atr_multiplier": 2.5, "funding_rate_limit": 0.0005}',
}


def get_config(key: str) -> str | None:
    row = get_conn().execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_CONFIG.get(key)


def get_all_config() -> dict:
    rows = get_conn().execute("SELECT key, value FROM config").fetchall()
    cfg = dict(DEFAULT_CONFIG)
    for r in rows:
        cfg[r["key"]] = r["value"]
    return cfg


def set_config(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    conn.commit()


def set_configs(kvs: dict):
    conn = get_conn()
    for k, v in kvs.items():
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
            (k, str(v), str(v)),
        )
    conn.commit()


# ── Profile-scoped config helpers ─────────────────────────────────────────
# Profile-scoped configs live under the `profile.<id>.` key prefix. These
# helpers bypass DEFAULT_CONFIG, so a missing key returns None (instead of the
# global default) — callers that need a default should handle it explicitly.

def get_profile_config(profile_id: int, key: str) -> str | None:
    row = get_conn().execute(
        "SELECT value FROM config WHERE key = ?",
        (f"profile.{profile_id}.{key}",),
    ).fetchone()
    return row["value"] if row else None


def set_profile_config(profile_id: int, key: str, value):
    set_config(f"profile.{profile_id}.{key}", str(value))


def set_profile_configs(profile_id: int, kvs: dict):
    set_configs({f"profile.{profile_id}.{k}": v for k, v in kvs.items()})


# ── last candle timestamp persistence ─────────────────────────────────────
# Persistem por TF/asset no config table com key `last_ts.<tf>.<asset>`.
# Evita que restart do bot dispare falso `new_<tf>` na primeira detecção pós-restart
# (problema: o dict em memória zera; primeira leitura faz `latest_ts > 0` sempre True).

def get_last_candle_ts(tf: str) -> dict[str, int]:
    """Carrega o dict {asset: last_ts_ms} para o timeframe dado."""
    prefix = f"last_ts.{tf}."
    rows = get_conn().execute(
        "SELECT key, value FROM config WHERE key LIKE ?", (f"{prefix}%",)
    ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        asset = r["key"][len(prefix):]
        try:
            out[asset] = int(r["value"])
        except (TypeError, ValueError):
            continue
    return out


def set_last_candle_ts(tf: str, asset: str, ts: int) -> None:
    """Persiste o último ts conhecido para (tf, asset)."""
    set_config(f"last_ts.{tf}.{asset}", str(int(ts)))


# ── lighter client_order_index counter (persistido) ───────────────────────
# O counter é usado pelo bot para gerar `client_order_index` único em cada
# `signer.create_order(...)`. Precisa ser monotônico ACROSS restarts — senão
# o lookup em /accountInactiveOrders fica ambíguo (várias txs com mesmo coi).
_COI_KEY = "lighter.client_order_counter"


def get_lighter_coi_counter(profile_id: int = 1) -> int:
    """Read the Lighter client_order_index counter for a profile.

    Pre-M8 the counter lived under the global key `lighter.client_order_counter`;
    M8 moved it to `profile.<id>.lighter.client_order_counter`. The counter is
    per Lighter account_index on the exchange, so each profile owns its own.
    """
    raw = get_profile_config(profile_id, "lighter.client_order_counter")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def set_lighter_coi_counter(n: int, profile_id: int = 1) -> None:
    set_profile_config(profile_id, "lighter.client_order_counter", str(int(n)))


# ── Profile CRUD ────────────────────────────────────────────────────
# Each profile owns its own Lighter/HL credentials and isolates strategies,
# trades, signals, logs and bot status via config keys under `profile.<id>.*`.

_PROFILE_CRED_FIELDS = (
    "lighter_wallet_address", "lighter_public_key", "lighter_private_key",
    "hyperliquid_address", "hyperliquid_secret",
)
# Public listing — strips out private keys and HL secret. Use get_profile(id)
# when you need the full row (e.g. to build an exchange client).
_PROFILE_PUBLIC_FIELDS = (
    "id", "name", "exchange",
    "lighter_wallet_address",
    "hyperliquid_address",
    "created_at", "updated_at",
)


def list_profiles() -> list[dict]:
    rows = get_conn().execute(
        "SELECT " + ", ".join(_PROFILE_PUBLIC_FIELDS) + " FROM profiles ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_profile(profile_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    return dict(row) if row else None


def _check_unique_lighter_wallet(wallet_address, exclude_id):
    """Reject two profiles pointing at the same Lighter wallet.

    The Lighter COI counter is per account_index, which is derived from the
    wallet address. Two profiles sharing a wallet would fight over the same
    counter and corrupt the /accountInactiveOrders lookup.
    """
    if not wallet_address:
        return
    row = get_conn().execute(
        "SELECT id FROM profiles WHERE lighter_wallet_address = ? AND id != ?",
        (str(wallet_address), exclude_id if exclude_id is not None else -1),
    ).fetchone()
    if row:
        raise ValueError(
            f"lighter_wallet_address '{wallet_address}' is already used by profile {row['id']}"
        )


def create_profile(*, name: str, exchange: str = "lighter", credentials: dict | None = None) -> int:
    if not name or not name.strip():
        raise ValueError("name is required")
    if exchange not in ("lighter", "hyperliquid"):
        raise ValueError(f"unknown exchange: {exchange}")
    creds = {k: (credentials or {}).get(k) for k in _PROFILE_CRED_FIELDS}
    _check_unique_lighter_wallet(creds.get("lighter_wallet_address"), exclude_id=None)
    now = int(time.time() * 1000)
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO profiles
           (name, exchange, lighter_wallet_address, lighter_public_key,
            lighter_private_key, hyperliquid_address, hyperliquid_secret,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name.strip(), exchange,
         creds["lighter_wallet_address"], creds["lighter_public_key"],
         creds["lighter_private_key"], creds["hyperliquid_address"],
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
        fields.append("name = ?"); values.append(name.strip())
    if exchange is not None:
        if exchange not in ("lighter", "hyperliquid"):
            raise ValueError(f"unknown exchange: {exchange}")
        fields.append("exchange = ?"); values.append(exchange)
    if credentials:
        if "lighter_wallet_address" in credentials:
            _check_unique_lighter_wallet(
                credentials["lighter_wallet_address"], exclude_id=profile_id
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
    # Cascade: drop profile-scoped trades/signals/logs and all namespaced config keys
    conn.execute("DELETE FROM trades WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM signals WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM logs WHERE profile_id = ?", (profile_id,))
    conn.execute(
        "DELETE FROM config WHERE key LIKE ?", (f"profile.{profile_id}.%",)
    )
    conn.commit()


def is_configured() -> bool:
    exchange = get_config("selected_exchange") or "hyperliquid"
    if exchange == "lighter":
        wallet = get_config("lighter_wallet_address")
        pubkey = get_config("lighter_public_key")
        privkey = get_config("lighter_private_key")
        return bool(wallet and pubkey and privkey)
    addr = get_config("account_address")
    key = get_config("secret_key")
    return bool(addr and key)


# ── Trades helpers ──────────────────────────────────────────────────

def insert_trade(trade: dict) -> int:
    conn = get_conn()
    trade.setdefault("open_fee", 0.0)
    trade.setdefault("strategy", "mean_reversion")
    trade.setdefault("signal_price", None)
    trade.setdefault("profile_id", 1)
    cur = conn.execute("""
        INSERT INTO trades (profile_id, asset, side, entry_price, size, status, entry_time,
                            ema9, ema21, rsi2, volume, atr, funding_rate, tp_price, sl_price, order_id,
                            fees, strategy, signal_price)
        VALUES (:profile_id, :asset, :side, :entry_price, :size, 'open', :entry_time,
                :ema9, :ema21, :rsi2, :volume, :atr, :funding_rate, :tp_price, :sl_price, :order_id,
                :open_fee, :strategy, :signal_price)
    """, trade)
    conn.commit()
    return cur.lastrowid


def close_trade(trade_id: int, exit_price: float, pnl: float, pnl_pct: float,
                fees: float = 0.0, funding: float = 0.0):
    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE trades SET exit_price = ?, pnl = ?, pnl_pct = ?, status = 'closed',
                          exit_time = ?, fees = ?, funding = ?
        WHERE id = ?
    """, (exit_price, pnl, pnl_pct, now, fees, funding, trade_id))
    conn.commit()


def get_open_trades(profile_id: int | None = None) -> list[dict]:
    """Open trades, optionally scoped to a single profile.

    profile_id=None returns every profile's open trades (used by audit scripts);
    pass an explicit id from the bot loop.
    """
    if profile_id is None:
        rows = get_conn().execute(
            "SELECT * FROM trades WHERE status = 'open'"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT * FROM trades WHERE status = 'open' AND profile_id = ?",
            (profile_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_trades(limit: int = 100, offset: int = 0, asset: str = None,
               side: str = None, date_from: str = None, date_to: str = None,
               strategy: str = None) -> list[dict]:
    query = "SELECT * FROM trades WHERE 1=1"
    params = []
    if asset:
        query += " AND asset = ?"
        params.append(asset)
    if side:
        query += " AND side = ?"
        params.append(side)
    if date_from:
        query += " AND entry_time >= ?"
        params.append(date_from)
    if date_to:
        query += " AND entry_time <= ?"
        params.append(date_to)
    if strategy:
        query += " AND strategy = ?"
        params.append(strategy)
    query += " ORDER BY entry_time DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = get_conn().execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_today_trades() -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = get_conn().execute(
        "SELECT * FROM trades WHERE entry_time >= ? ORDER BY entry_time DESC",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_daily_pnl() -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = get_conn().execute(
        "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE status = 'closed' AND exit_time >= ?",
        (today,),
    ).fetchone()
    return row["total"]


def get_total_pnl() -> float:
    row = get_conn().execute(
        "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE status = 'closed'"
    ).fetchone()
    return row["total"]


def get_trade_stats() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_conn()
    total_closed = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status='closed'").fetchone()["c"]
    wins = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status='closed' AND pnl > 0").fetchone()["c"]
    today_count = conn.execute("SELECT COUNT(*) as c FROM trades WHERE entry_time >= ?", (today,)).fetchone()["c"]
    return {
        "total_closed": total_closed,
        "wins": wins,
        "win_rate": (wins / total_closed * 100) if total_closed > 0 else 0,
        "today_count": today_count,
    }


def get_strategy_stats(days: int | None = None, profile_id: int = 1) -> list[dict]:
    """Return trades count, wins, win_rate and pnl per strategy for one profile.
    Includes strategies that have open trades so cards appear immediately.
    wins/pnl are computed from closed trades only; total counts all trades.
    Optional `days` limits closed-trade stats to the last N days.
    """
    conn = get_conn()
    date_filter = ""
    if days is not None:
        date_filter = f"AND (status = 'open' OR entry_time >= datetime('now', '-{int(days)} days'))"
    rows = conn.execute(f"""
        SELECT strategy,
               COUNT(*) as total,
               SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_count,
               SUM(CASE WHEN status = 'closed' AND pnl > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_total,
               COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END), 0) as pnl,
               AVG(CASE
                   WHEN signal_price IS NOT NULL AND signal_price > 0 AND side = 'long'
                       THEN (entry_price - signal_price) / signal_price * 100
                   WHEN signal_price IS NOT NULL AND signal_price > 0 AND side = 'short'
                       THEN (signal_price - entry_price) / signal_price * 100
               END) as avg_slippage_pct
        FROM trades
        WHERE profile_id = ? AND strategy IS NOT NULL {date_filter}
        GROUP BY strategy
    """, (profile_id,)).fetchall()
    enabled_prefix = f"profile.{profile_id}.strategy."
    enabled_rows = conn.execute(
        "SELECT key, value FROM config WHERE key LIKE ?",
        (f"{enabled_prefix}%.enabled",),
    ).fetchall()
    enabled_map = {
        er["key"][len(enabled_prefix):].rsplit(".enabled", 1)[0]: (er["value"] == "true")
        for er in enabled_rows
    }
    return [
        {
            "strategy": r["strategy"],
            "total": r["total"],
            "open_count": r["open_count"],
            "closed_total": r["closed_total"],
            "wins": r["wins"],
            "win_rate": round(r["wins"] / r["closed_total"] * 100, 1) if r["closed_total"] > 0 else 0,
            "pnl": round(r["pnl"], 2),
            "avg_slippage_pct": round(r["avg_slippage_pct"], 4) if r["avg_slippage_pct"] is not None else None,
            "enabled": enabled_map.get(r["strategy"], False),
        }
        for r in rows
    ]


# ── Signals helpers ─────────────────────────────────────────────────

def insert_signal(signal: dict) -> int:
    conn = get_conn()
    signal.setdefault("strategy_name", "mean_reversion")
    signal.setdefault("profile_id", 1)
    cur = conn.execute("""
        INSERT INTO signals (profile_id, timestamp, asset, side, executed, reason,
                             ema9, ema21, rsi2, volume, volume_avg, atr, funding_rate,
                             strategy_name)
        VALUES (:profile_id, :timestamp, :asset, :side, :executed, :reason,
                :ema9, :ema21, :rsi2, :volume, :volume_avg, :atr, :funding_rate,
                :strategy_name)
    """, signal)
    conn.commit()
    return cur.lastrowid


def get_signals(limit: int = 100, offset: int = 0, strategy_name: str = None) -> list[dict]:
    query = "SELECT * FROM signals WHERE 1=1"
    params = []
    if strategy_name:
        query += " AND strategy_name = ?"
        params.append(strategy_name)
    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = get_conn().execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_strategy_config(strategy_name: str, profile_id: int = 1) -> dict:
    enabled_key = f"profile.{profile_id}.strategy.{strategy_name}.enabled"
    params_key = f"profile.{profile_id}.strategy.{strategy_name}.params"
    enabled_raw = get_config(enabled_key)
    if enabled_raw is None:
        # First time this strategy is seen on this profile — persist default so
        # restarts respect it. Default OFF: usuário precisa ativar explicitamente.
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


# ── Logs helpers ────────────────────────────────────────────────────

def insert_log(timestamp: str, level: str, module: str, message: str,
               profile_id: int | None = None):
    """Insert a log row. profile_id=None marks the row as global (candle manager,
    migrations, anything not owned by a specific profile)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO logs (profile_id, timestamp, level, module, message) VALUES (?, ?, ?, ?, ?)",
        (profile_id, timestamp, level, module, message),
    )
    conn.commit()


def get_logs(limit: int = 200, level: str = None,
             profile_id: int | None = None) -> list[dict]:
    """Return recent logs.

    profile_id=None returns every row (global view).
    profile_id=<id> returns rows owned by that profile OR with NULL profile_id
    (global logs always appear, so candle-manager messages remain visible).
    """
    query = "SELECT * FROM logs WHERE 1=1"
    params = []
    if level:
        query += " AND level = ?"
        params.append(level)
    if profile_id is not None:
        query += " AND (profile_id IS NULL OR profile_id = ?)"
        params.append(profile_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = get_conn().execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ── Cumulative PnL for charts ──────────────────────────────────────

def get_cumulative_pnl() -> list[dict]:
    rows = get_conn().execute("""
        SELECT exit_time, pnl,
               SUM(pnl) OVER (ORDER BY exit_time) as cumulative_pnl
        FROM trades
        WHERE status = 'closed' AND exit_time IS NOT NULL
        ORDER BY exit_time
    """).fetchall()
    return [dict(r) for r in rows]


def get_pnl_distribution() -> list[float]:
    rows = get_conn().execute(
        "SELECT pnl FROM trades WHERE status = 'closed' AND pnl IS NOT NULL"
    ).fetchall()
    return [r["pnl"] for r in rows]
