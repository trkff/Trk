# Fee Viability Filter — Design Spec
**Date:** 2026-03-28
**Status:** Approved

## Problem

When ATR is small (low volatility), the expected gross profit from TP is smaller than round-trip fees (taker 0.045% × 2 sides = 0.09%), causing a loss even when TP is hit. The bot currently has no guard against this.

## Goal

Block entry signals where expected TP profit does not cover round-trip fees, and expose fee viability status per asset in the dashboard.

---

## Fee Viability Formula

```
viable = (ATR / entry_price) × tp_multiplier > fee_rate
```

- `fee_rate` default: `0.0009` (0.09% round trip — taker both sides)
- `tp_multiplier`: read from each strategy's config params
- `ATR`: latest ATR value from `compute_all`
- `entry_price`: `close_1m` from indicators

**Examples:**
- BTC ATR=$20, price=$66,747, tp=2 → 0.0006 < 0.0009 → BLOCKED
- BTC ATR=$35, price=$66,747, tp=2 → 0.00105 > 0.0009 → ALLOWED

---

## Architecture

### 1. `bot/indicators.py` — new pure function

```python
def is_fee_viable(atr: float, price: float, tp_multiplier: float, fee_rate: float = 0.0009) -> bool:
    return (atr / price) * tp_multiplier > fee_rate
```

No side effects. Called by each strategy.

---

### 2. Strategy changes (all three strategies)

**Pattern (identical for all):**
1. Read `fee_rate = float(cfg.get("fee_rate_round_trip", 0.0009))`
2. Read `tp_mult = float(params.get("tp_atr_multiplier", DEFAULT))`
3. Call `is_fee_viable(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate)`
4. If `False`: insert signal to DB with reason, log DEBUG, return `None`

**Special case — `mean_reversion.py`:**
Currently `evaluate()` does not read `tp_atr_multiplier` from params (only the executor uses it). Add this read exclusively for the fee viability check. Default remains `1.5`.

**Per-strategy `tp_atr_multiplier` defaults:**
| Strategy | Default |
|---|---|
| mean_reversion | 1.5 |
| funding_arb | 1.5 |
| order_flow | 1.5 |

**DB signal record when fee blocks:**
```python
reason = f"ATR insuficiente para cobrir fees (atr_pct={atr_pct:.4%}, necessário={fee_rate/tp_mult:.4%})"
db.insert_signal({**base, "side": side, "reason": reason})
```

**Log level:** DEBUG (not WARNING — this is expected behavior, not an anomaly)

---

### 3. `main.py` — live asset status dict

```python
_asset_live_status: dict[str, dict] = {}

def get_asset_live_status() -> dict:
    return dict(_asset_live_status)
```

Updated in `process_asset()` after `compute_all` succeeds:
```python
fee_rate = float(db.get_config("fee_rate_round_trip") or 0.0009)
mr_params = db.get_strategy_config("mean_reversion").get("params", {})
tp_mult = float(mr_params.get("tp_atr_multiplier", 1.5))
atr = indicators["atr"]
price = indicators["close_1m"]
atr_pct = atr / price
_asset_live_status[asset] = {
    "atr_pct": round(atr_pct, 6),
    "required_pct": round(fee_rate / tp_mult, 6),
    "fee_viable": atr_pct * tp_mult > fee_rate,
}
```

> Note: the live status uses mean_reversion's tp_mult for the display. Each strategy applies the check with its own multiplier internally.

---

### 4. `dashboard/app.py` — include in SocketIO push

In `background_pusher`, include asset status:
```python
from main import get_asset_live_status
# inside the emit:
"asset_status": get_asset_live_status(),
```

Also include in `/api/overview` REST response for initial page load.

---

### 5. `dashboard/templates/config.html` — new fee_rate field

Added to the "Gerenciamento de Risco" section:
```html
<div class="config-field">
  <label for="cfg-fee-rate">Fee Rate Round Trip (ex: 0.0009)</label>
  <input type="number" id="cfg-fee-rate" min="0.0001" max="0.01" step="0.0001">
</div>
```

Saved as `fee_rate_round_trip` via the existing `/api/config` POST endpoint.

---

### 6. `dashboard/templates/overview.html` — asset status section

New section below KPI cards, above open positions:

```
┌─────────────────────────────────────────────┐
│ Ativos Monitorados                          │
│  BTC  ATR: 0.0524%  Req: 0.0450%  [FEE VIÁVEL]   (verde)  │
│  ETH  ATR: 0.0310%  Req: 0.0450%  [BLOQUEADO]    (vermelho)│
│  SOL  ATR: 0.0612%  Req: 0.0450%  [FEE VIÁVEL]   (verde)  │
└─────────────────────────────────────────────┘
```

Populated from `data.asset_status` in the `applyOverview` JS function. Shows `--` if no data yet (bot not yet processed first candle).

---

## Config Key

| Key | Type | Default | Description |
|---|---|---|---|
| `fee_rate_round_trip` | float (string in DB) | `0.0009` | Round-trip fee rate (taker × 2) |

---

## Files Changed

| File | Change |
|---|---|
| `bot/indicators.py` | Add `is_fee_viable()` function |
| `bot/strategies/mean_reversion.py` | Read `tp_atr_multiplier` in evaluate + fee filter |
| `bot/strategies/funding_arb.py` | Fee filter |
| `bot/strategies/order_flow.py` | Fee filter |
| `main.py` | `_asset_live_status` dict + `get_asset_live_status()` |
| `dashboard/app.py` | Include `asset_status` in overview push + REST |
| `dashboard/templates/config.html` | `fee_rate_round_trip` input field |
| `dashboard/templates/overview.html` | Asset status section with fee viability badges |

---

## Out of Scope

- Maker order entry to reduce fee_rate to 0.0006 (future improvement)
- Per-asset fee tier configuration
- Changing TP/SL calculation logic
