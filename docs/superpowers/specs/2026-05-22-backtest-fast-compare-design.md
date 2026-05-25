# Backtest Fast + Compare Tab — Design

**Date:** 2026-05-22
**Status:** Approved, ready for implementation planning
**Scope:** Single sub-project — vectorized backtest engine + side-by-side comparison page

## Motivation

The existing `bot/backtest/engine.py` is faithful but slow: it recomputes all indicators on a 600-candle rolling window per iteration, uses `pandas.DataFrame.iterrows()` for the trade outcome loop, and calls `strategy.evaluate()` candle by candle. The Scanner (`bot/backtest/scanner.py`) shows that the same TP/SL bar-by-bar simulation can run on the full history in seconds when indicators are precomputed once and signals are derived as numpy boolean masks.

Goal: a second engine (`engine_fast.py`) that produces results equivalent to `engine.py` for the same strategy/asset/params, fast enough that the user can iterate on parameter changes interactively. A new dashboard page runs both engines side by side so the user can validate fidelity and measure the speedup before adopting the fast engine as the default.

## Non-Goals

- Replacing `engine.py`. The legacy engine stays in place and remains the production backtest until validation completes.
- Caching simulated trades in the fast engine (v1). The fast engine recomputes from scratch on every run.
- Supporting parameter overrides from the UI. Both engines read the same params from the DB via `manager` so the comparison is apples-to-apples.
- Adding new strategies. Coverage matches the 8 strategy families and 29 instances already supported by the legacy engine.

## Architecture

### New module: `bot/backtest/engine_fast.py`

Mirrors the public API of `engine.py`:

- `start_backtest_job(strategy, asset, days, trade_size_usd, fee_rate) -> job_id` — kicks off background thread, returns UUID.
- `get_job(job_id) -> dict` — returns `{status, progress, result, error, elapsed_s}`. `status` is one of `running | done | error`.

Internals:

- Imports `_load_candles_csv`, `_update_csv`, `_resolve_strategy_instance` from `engine.py`. No refactor in v1 — direct import keeps the change surface small.
- Reads strategy params from the DB via `manager.STRATEGY_MAP[name]` and merges `asset_overrides[asset]` exactly like the legacy engine.
- Precomputes indicators **once over the full series** (not on a 600-candle rolling window). See "Numerical fidelity" below.
- Builds long/short signal masks via family-specific vectorized functions.
- Runs the trade simulation loop in numpy (no `iterrows`).

### Signal computation per family

For each strategy family, one function:

```
_signals_<family>(close, high, low, ts, params) -> (sig_long, sig_short, bb_mid_or_None, sl_dist_or_None)
```

- `sig_long`, `sig_short`: boolean numpy arrays, length N (number of candles).
- `bb_mid_or_None`: float numpy array of length N when `params.get("bb_mid_exit") is True`, else `None`.
- `sl_dist_or_None`: float numpy array of length N for strategies that use ATR-based SL distance (currently `ema_cross` with `use_atr_sl=True`), else `None`.

Families and their signal logic (faithful port of the live strategies, already documented in `hyperliquid-bot/CLAUDE.md`):

| Family        | Long signal                                                      | Short signal                                                       | Extras                          |
|---------------|------------------------------------------------------------------|--------------------------------------------------------------------|---------------------------------|
| `bb_reversion`| BBP_prev < th_long AND close_curr > BBL_curr AND close_curr < BBM_curr AND EMA filter AND RSI filter | BBP_prev > th_short AND close_curr < BBU_curr AND close_curr > BBM_curr AND filters | `bb_mid` when enabled            |
| `bb_stoch`    | BBP_curr < th_long AND %K < stoch_os AND %D < stoch_os AND EMA filter | BBP_curr > th_short AND %K > stoch_ob AND %D > stoch_ob AND EMA filter | `bb_mid` when enabled (default off) |
| `stoch_scalp` | prev_K<os AND prev_D<os AND curr_K>curr_D AND prev_K<=prev_D AND EMA filter | prev_K>ob AND prev_D>ob AND curr_K<curr_D AND prev_K>=prev_D AND EMA filter | none                            |
| `ema_cross`   | EMA_fast crosses above EMA_slow AND trend filter                  | EMA_fast crosses below EMA_slow AND trend filter                    | `sl_dist` when `use_atr_sl=True`|
| `rsi_scalp`   | prev_rsi < os AND curr_rsi >= os AND EMA filter                   | prev_rsi > ob AND curr_rsi <= ob AND EMA filter                     | none                            |
| `bb_rsi`      | BBP_curr < th_long AND RSI_curr < os AND EMA filter               | BBP_curr > th_short AND RSI_curr > ob AND EMA filter                | `bb_mid` when enabled            |
| `macd_cross`  | curr_macd > curr_sig AND prev_macd <= prev_sig AND EMA trend filter | curr_macd < curr_sig AND prev_macd >= prev_sig AND EMA trend filter | none                            |
| `williams_r`  | prev_wr < wr_os AND curr_wr >= wr_os AND EMA filter               | prev_wr > wr_ob AND curr_wr <= wr_ob AND EMA filter                 | none                            |

Family dispatch: `_FAMILY_FN = {"bb_reversion": _signals_bb_reversion, ...}`. Instance name (e.g. `bb_stoch_btc`) is resolved to family by `_resolve_strategy_instance` → `instance.NAME` → match the longest-prefix family key. This reuses the same prefix-matching logic the manager uses for dynamic instances.

### Trade simulation loop

```
_simulate_fast(sig_long, sig_short, close, high, low, ts,
               bb_mid, sl_dist, params, fee_rate, trade_size_usd) -> list[trade_dict]
```

- Walks `i` from 0 to N-1, skipping candles where neither `sig_long[i]` nor `sig_short[i]` is true.
- On a signal candle:
  - `entry = close[i]`, `side` derived from which mask fired (long takes precedence if both fire, matching the legacy engine).
  - Compute `tp` and `sl` using the same three modes the legacy engine supports:
    - **ATR mode** (when params indicate ATR-based, currently `ema_cross` with `use_atr_sl=True`): `tp` from `tp_pct`, `sl` from `sl_dist[i]`.
    - **sl_price_hint + rr_ratio mode**: not currently emitted by any in-tree strategy; v1 deferred. If signal logic produces it later, the loop will need to handle it.
    - **Percentage mode**: `tp = entry * (1 ± tp_pct/100)`, `sl = entry * (1 ∓ sl_pct/100)`. Default for all current strategies.
  - Find first TP, SL, BB-mid hits using `numpy.argmax` on boolean slices of `high`/`low`/`bb_mid` starting at `i+1`. `argmax` on a boolean array returns the index of the first `True`, or `0` if no `True`. Distinguish "no hit" from "hit at slice[0]" with an explicit `.any()` check.
  - Outcome = earliest hit. Tie-break inside the same candle (TP and SL hit in same bar): use `close[j] vs entry` rule (long wins if close >= entry), matching `_simulate_trade` in the legacy engine.
  - Trades where neither TP nor SL is ever hit are **discarded** (matches the legacy engine's "no timeout" behavior).
  - Advance `i` to `outcome_index + 1` (no overlap, matches scanner and legacy).
- Trade dict shape matches the legacy engine exactly so `report.compute_metrics` and the dashboard table both work unchanged.

### PnL and fees

- `pnl_pct = ±tp_pct` (win) or `±sl_pct` (loss), with sign by side and outcome.
- `pnl_raw = pnl_pct/100 * trade_size_usd`.
- `pnl = pnl_raw - 2 * fee_rate * trade_size_usd` (open + close fee).
- BB-mid exit: `pnl_pct = (exit_price/entry_price - 1) * 100 * direction`, `pnl` same fee adjustment.

Lighter has zero taker fee, so by default `fee_rate=0` from the UI gives `pnl = pnl_raw`, matching live conditions.

## Numerical fidelity vs the legacy engine

The fast engine computes indicators **on the full series**; the legacy engine computes them on a **rolling 600-candle window** inside the simulation loop. Two consequences:

1. **EMA and RSI seed values differ** in the first few hundred candles after warmup, because the rolling-window version restarts each iteration. After `~3 × period` candles, both converge.
2. **BB, Stoch, and Williams %R** are functions of the last `period` candles only, so they match exactly once warmup is past on both sides.

Net effect: trades opened in the first few hundred candles of the historical window may diverge (different indicator values → different signal candle). Trades after that point should match.

The comparison page surfaces all divergences explicitly. The user accepts or rejects after seeing the panel.

## Dashboard: `/backtest-compare`

### Page layout

```
[ Header: Backtest Compare ]

[ Filters row (single set, drives both engines) ]
  Strategy | Period | Trade Size | Fee Rate | [Run]

[ Comparison summary card ]
  Legacy: 42 trades in 38.2s
  Fast:   42 trades in 0.41s   (~93x faster)
  Δ trades: 0   |   Δ total PnL: $0.12   |   Divergent trades: 1

[ Two column panels ]
  | Legacy panel              | Fast panel               |
  |  KPI cards (7)            |  KPI cards (7)           |
  |  Cumulative PnL chart     |  Cumulative PnL chart    |
  |  Trades table             |  Trades table            |

[ Trade-by-trade diff panel (collapsible) ]
  Each row: entry_time | side | outcome_legacy | outcome_fast | pnl_legacy | pnl_fast | match?
  Matching rows green-collapsed, diverging rows red-expanded.
```

### Backend

- `GET /backtest-compare` → renders `dashboard/templates/backtest_compare.html`.
- `POST /api/backtest/compare/run` → body `{strategy, asset, days, trade_size_usd, fee_rate}` → spawns one legacy job and one fast job in parallel via the existing job systems → returns `{legacy_id, fast_id}`.
- `GET /api/backtest/compare/status/<legacy_id>/<fast_id>` → returns `{legacy: <legacy_job_dict>, fast: <fast_job_dict>}`. Frontend polls this endpoint every 2s until both have status `done` or `error`.

### Frontend

- Single set of controls drives both runs.
- Two Chart.js instances (one per panel).
- Divergence detection client-side: match trades by `entry_time + side`. Tolerance: exact match required. Any difference in `outcome` or `pnl` (rounded to 2 decimals) counts as a divergence.

### Navigation

`base.html` gains a link "Backtest ⚡" pointing to `/backtest-compare`, placed next to the existing "Backtest" link.

## Error handling

- If either job errors, the comparison summary card shows the error for that side and the panel renders `--` placeholders. The other side still displays normally.
- If `_update_csv` fails (Lighter REST down), both engines hit the same error path — surfaced once in the summary, not duplicated.
- Engine-fast specific errors (e.g. unknown strategy family) raise inside the job thread, caught by the job wrapper, surfaced via `job.error`.

## Testing

Unit tests (pytest, `tests/backtest/`):

- `test_engine_fast_smoke.py` — runs `engine_fast` on a small synthetic OHLCV array for one instance per family, asserts trades list is non-empty and shape is correct.
- `test_engine_fast_vs_legacy_bb_stoch.py` — runs both engines on `bb_stoch_btc` over 30 days of real candles, asserts trade count delta ≤ 2 and total PnL delta ≤ 1% (allows for early-warmup drift).
- `test_signals_<family>.py` (one per family) — fixed input arrays, asserts the boolean masks match hand-computed expected values.

Manual validation:

- Run the compare page for each of the 29 instances on 90 days. Document divergence counts in the implementation plan's "Done" criteria.

## Open questions resolved during brainstorming

- **Layout:** side-by-side (not separate tab, not replacement).
- **BB mid exit:** kept and vectorized.
- **Params source:** DB via `manager` (same as legacy engine).
- **Coverage:** all 8 families / 29 instances.
- **Cache:** none in v1.

## Files touched

New:
- `hyperliquid-bot/bot/backtest/engine_fast.py`
- `hyperliquid-bot/dashboard/templates/backtest_compare.html`
- `hyperliquid-bot/tests/backtest/test_engine_fast_smoke.py`
- `hyperliquid-bot/tests/backtest/test_engine_fast_vs_legacy_bb_stoch.py`
- `hyperliquid-bot/tests/backtest/test_signals_<family>.py` (8 files)

Modified:
- `hyperliquid-bot/dashboard/app.py` — three new routes.
- `hyperliquid-bot/dashboard/templates/base.html` — nav link.
- `hyperliquid-bot/CLAUDE.md` — document `engine_fast.py` and the new page (updated at end of implementation).
