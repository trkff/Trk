# Multi-Profile Support for RazorHL

**Status:** Design approved, pending implementation plan
**Date:** 2026-05-27
**Owner:** Davi

## Summary

Add multi-profile support to RazorHL so a single local instance can run several Lighter accounts in parallel. Each profile carries its own credentials, strategies, trades, signals, logs and bot state. The candle manager, downloaded CSVs, scanner engine and backtest engine are shared across profiles. A dropdown in the dashboard header switches the UI context between profiles; bots of inactive (UI-sense) profiles keep running in the background.

No real authentication is added — RazorHL keeps running locally, profile selection is cosmetic UX. OAuth can be retrofitted later if the app is ever hosted on a VPS.

## Goals

- Run N Lighter accounts in parallel from one RazorHL process.
- Each profile owns its strategies, trades, signals, logs, bot status, risk/sizing config and Lighter credentials.
- A single shared `LighterCandleManager` feeds all profiles; CSVs in `candles/` stay global.
- Backtest and Scanner remain shared engines but operate on the active profile's strategies/results.
- Profile management UI: list, create, rename, edit credentials, delete.
- Zero data loss for current single-profile users — existing data migrates into a "Default" profile.

## Non-goals

- OAuth / Google login / password auth (deferred until hosted deployment).
- Billing, rate limits, public sign-up.
- Cross-profile aggregated dashboards (each profile is viewed independently).
- Hyperliquid multi-profile beyond carrying its existing single-credential model into the same per-profile structure.

## Architecture

### Data model

**New table:**

```sql
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'lighter',  -- 'lighter' | 'hyperliquid'
    lighter_account_index TEXT,
    lighter_api_key_private TEXT,
    lighter_api_key_index TEXT,
    hyperliquid_address TEXT,
    hyperliquid_secret TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

`UNIQUE(lighter_account_index)` enforced at the application layer (allows NULL across multiple HL-only profiles).

**Existing tables gain `profile_id INTEGER`:**

- `trades.profile_id` (NOT NULL after migration; default 1)
- `signals.profile_id` (NOT NULL after migration; default 1)
- `logs.profile_id` (NULL allowed — global logs from candle manager/migrations)

**Config keys — per-profile keys get namespaced under `profile.<id>.`:**

| Old key | New key |
|---|---|
| `strategy.<name>.params` | `profile.<id>.strategy.<name>.params` |
| `strategy.<name>.enabled` | `profile.<id>.strategy.<name>.enabled` |
| `strategy.<name>.scanner_metrics` | `profile.<id>.strategy.<name>.scanner_metrics` |
| `bot_status` | `profile.<id>.bot_status` |
| `assets` | `profile.<id>.assets` |
| `risk.*` | `profile.<id>.risk.*` |
| `sizing.*` | `profile.<id>.sizing.*` |
| `lighter.client_order_counter` | `profile.<id>.lighter.client_order_counter` |

**Config keys that stay global (unchanged):**

- `last_ts.<tf>.<asset>` — boundary detector, profile-agnostic
- `selected_exchange` — used as default when creating new profiles
- `use_lighter_ws_candles`
- All `_migration_*` markers

### What is shared vs per-profile

**Shared (singleton across all profiles):**

- `LighterCandleManager` (one WS connection, union of all active profiles' assets)
- Candle CSVs in `candles/`
- In-memory candle buffer in `LighterExchangeClient.get_candles` cache *(see open question below)*
- Scanner engine, backtest engine, Ativos tab
- Global config (last_ts, selected_exchange, ws flags)

**Per-profile (isolated):**

- Lighter credentials, account index, API key
- Active strategies, their params, enable flags and `scanner_metrics`
- Trades, signals, logs
- Bot status (`running` / `paused` / `stopped`)
- Risk and sizing config
- Lighter COI counter (per account_index on the exchange)
- Open Lighter positions and TP/SL trigger orders (live on exchange under that account)

### Bot loop

Today `main.py` keeps one global `_bot_thread` running `bot_loop()`. It becomes:

```python
_bot_threads: dict[int, threading.Thread]    # profile_id -> worker
_bot_clients: dict[int, LighterExchangeClient]
_bot_status: dict[int, str]                   # mirrors profile.<id>.bot_status
candle_mgr: LighterCandleManager | None       # singleton
```

**Startup (`run.py`):** read all profiles from DB; for each with `bot_status in {running, paused}`, call `start_bot(profile_id)`. Same auto-resume behaviour as today, but per profile.

**`start_bot(profile_id)`:**

1. Load credentials → instantiate `LighterExchangeClient` for this profile.
2. Persist `_bot_clients[profile_id]`.
3. Spawn `bot_loop(profile_id)` thread.
4. If `candle_mgr is None`, create it with the union of assets across all running profiles; otherwise call `candle_mgr.update_assets(union_assets())`.

**`on_candle_close(asset, interval)`** (candle manager callback, global, gated `interval=="5m"`):

For each `profile_id` in `_bot_threads` with status `running`, if `asset` is in that profile's asset list, enqueue `(profile_id, asset)` on the existing work queue. Workers in the pool drain the queue and call `process_asset(profile_id, asset, cfg)`.

**`process_asset(profile_id, asset, cfg)`** — same logic as today, but:

- All exchange IO uses `_bot_clients[profile_id]`.
- `manager.evaluate_all(...)` receives the strategies of this profile.
- `executor.open_position(profile_id, ...)` inserts the trade with `profile_id`.
- Execution lock becomes `_open_locks: dict[tuple[int, str], Lock]` keyed by `(profile_id, asset)` — two profiles may open the same asset in the same candle, single profile still cannot duplicate.

**`stop_bot(profile_id)`:** stop the thread, close that profile's Lighter client, recompute `union_assets()` and call `candle_mgr.update_assets(...)`. Tear down `candle_mgr` only when no profile is running.

### Dashboard / UI

**Profile selector — sticky in the header, top-left of every page:**

```
[Conta Principal ▼]
   ├─ Conta Principal      ●   running
   ├─ Teste Multi-TF       ○   stopped
   ├─ Conta Hedge          ◐   paused
   ├──────────────────────
   ├─ ✎ Renomear perfil ativo
   ├─ ⚙ Editar credenciais
   ├─ 🗑 Excluir perfil ativo
   └─ + Novo perfil
```

- Status dot is driven by `bot_status` for each profile, updated in real-time over SocketIO.
- Selecting a profile just changes the UI context — running bots of other profiles continue untouched.
- Active profile id stored in Flask session cookie (`session["active_profile_id"]`).
- `before_request` sets `g.profile_id = session.get("active_profile_id")`; if missing or invalid → redirect to `/profiles/new` (first-run flow).

**Pages and how they react to the active profile:**

| Page | Per-profile? |
|---|---|
| Overview | Yes — strategy cards / KPIs of active profile |
| Análise | Yes — trades & equity of active profile |
| Estratégias | Yes — strategies enabled on active profile |
| Config (credenciais, risk, sizing) | Yes — edits active profile |
| Scanner | Shared engine; "Aplicar resultado" creates instance on active profile |
| Backtest | Shared engine; runs over **active profile's** active strategies |
| Ativos | Shared — CSVs are global |
| **Profiles (new)** | Global — CRUD of profiles |

**New endpoints:**

```
GET    /api/profiles                  → list (id, name, exchange, bot_status)
POST   /api/profiles                  → create (name, exchange, credentials)
PATCH  /api/profiles/<id>             → rename / update credentials
DELETE /api/profiles/<id>             → delete (blocked if open positions or last profile)
POST   /api/profiles/<id>/activate    → set session.active_profile_id
POST   /api/profiles/<id>/bot/start   → idem for /pause and /stop
```

**SocketIO:** existing events (`trade_update`, `signal_update`, `log_update`, `bot_status`) gain `profile_id` in their payload. Clients filter by `active_profile_id` before rendering. The status dot in the profile dropdown listens to `bot_status` for *every* profile (not filtered).

**Profile deletion safety:**

- Blocked if any `trades` row for that profile has `exit_time IS NULL`.
- Blocked if it is the last remaining profile (must keep at least one).
- Double-confirmation modal.
- Cascade: delete this profile's trades/signals/logs and all `profile.<id>.*` config keys.

### Migration (M8)

One-shot, idempotent via marker `_migration_multi_profile=done`. Runs in `init_db` **after** existing M6/M7:

1. Create `profiles` table.
2. Insert profile `id=1, name="Default"`. Populate credentials from current global config (`account_address`, `secret_key`, `lighter_*`, `selected_exchange`).
3. Add `profile_id INTEGER DEFAULT 1` column to `trades`, `signals`, `logs`.
4. Backfill existing rows with `profile_id=1` (DEFAULT handles new inserts; explicit UPDATE not required for SQLite ADD COLUMN with default).
5. For every config key matching the namespaced patterns above, copy value to `profile.1.<key>` and delete the original.
6. Set marker.

Idempotent guarantees: if marker exists, do nothing. All steps wrapped in a single transaction.

### Database access layer

`bot/db.py` extensions:

- `get_profile_config(profile_id, key)`, `set_profile_config(profile_id, key, val)` — internally prefix with `profile.<id>.`.
- `get_strategy_config(name, profile_id)`, `set_strategy_config(name, profile_id, ...)` — profile-aware variants of today's helpers.
- `get_open_trades(profile_id)`, `insert_trade(profile_id, ...)`, `get_strategy_stats(profile_id)` — all trade/signal/log helpers take an explicit `profile_id`.
- `list_profiles()`, `get_profile(id)`, `create_profile(...)`, `update_profile(id, ...)`, `delete_profile(id)`.
- Global helpers (`get_last_candle_ts`, `set_lighter_coi_counter` etc.) — COI counter helper now takes `profile_id`; last_candle_ts unchanged.

Old single-profile helpers are kept temporarily for the phased rollout (see below), then removed in Phase 4.

## Rollout phases

The work splits into 5 phases so the bot keeps running in production while the refactor lands.

1. **DB + Migration M8.** Add `profiles` table, `profile_id` columns, run M8 on next boot. Bot still hardcodes `profile_id=1`. Smoke test: bot starts, trades flow into DB tagged `profile_id=1`, all dashboards still work.
2. **Access layer refactor.** Plumb `profile_id` parameter through `bot/db.py`, `executor.py`, `risk.py`, `manager.py`, `main.py`, all backtest/scanner call sites. Every site passes literal `1`. Still single-profile in practice.
3. **Profile CRUD + UI selector.** New endpoints, `/profiles` page, dropdown in header, Flask session `active_profile_id`. UI now reads `g.profile_id` everywhere. Single profile in DB still, but selector is live.
4. **Multi-bot execution.** `_bot_threads` / `_bot_clients` dicts, candle manager union-assets, per-profile lock keys, `start_bot(profile_id)` / `stop_bot(profile_id)`. Now creating Conta 2 actually spawns a second bot in parallel.
5. **Polish.** SocketIO payloads carry `profile_id`, dropdown status dots refresh live, profile deletion safety, validation on duplicate `lighter_account_index`.

## Testing

- **Migration:** snapshot DB before/after M8; assert every old key is copied + deleted, marker set, rerun is no-op.
- **DB layer:** unit tests for each new helper with two profiles.
- **Multi-bot isolation:** mock two Lighter clients, fire a 5m close on a shared asset, verify each profile opens its own trade tagged with the correct `profile_id` and that `cleanup_orphan_triggers` only touches the owning account.
- **Lock granularity:** two profiles, same asset, same boundary — both succeed; same profile, same asset, two queue entries — second aborts on lock contention (regression for the existing executor lock).
- **Deletion guard:** profile with open trade → DELETE returns 409; last profile → DELETE returns 409.

## Risks and mitigations

- **Duplicate `lighter_account_index` across profiles** would make two bots fight over the same COI counter and produce phantom cancel reasons (see [feedback_lighter_coi_persist_restart.md](../../../../.claude/projects/C--Users-User-Documents-Vibe-Code-RazorHL/memory/feedback_lighter_coi_persist_restart.md)). Mitigation: app-layer uniqueness check on create/update of profile, even though SQL allows it.
- **Candle buffer cache key.** `LighterExchangeClient._candle_buffer` is per-client; with N clients each maintains its own buffer (extra memory, no correctness issue). Acceptable. *Open question: pull the candle buffer up to the candle manager so it is truly shared. Decide during Phase 4.*
- **SocketIO room scoping.** Today every dashboard client gets every event. With multiple profiles running in parallel a client filters events client-side by `profile_id`. Acceptable for local single-user; if VPS ever happens, switch to SocketIO rooms keyed by `profile_id`.
- **Migration on broken DB.** If `init_db` crashes mid-M8 the marker is not set and the transaction rolls back; next boot retries. No partial-state risk.

## Open questions

1. **Candle buffer cache:** keep one per `LighterExchangeClient` (today's structure) or hoist into the singleton candle manager and have clients read from it? Default to "keep as-is" in Phase 4, revisit if memory becomes an issue.
2. **Logs UI:** today the logs view shows everything. Should it default to active-profile only with a "Show all profiles" toggle, or always all? Defaulting to active-profile is cleaner; add toggle for global view.

These do not block implementation — both have safe defaults.
