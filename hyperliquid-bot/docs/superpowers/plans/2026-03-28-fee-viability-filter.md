# Fee Viability Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Block entry signals where expected TP profit does not cover round-trip fees, and expose per-asset fee viability in the Overview dashboard.

**Architecture:** A pure function `is_fee_viable(atr, price, tp_multiplier, fee_rate)` in `bot/indicators.py` is called inside each strategy's `evaluate()` after signal conditions are met but before returning. Blocked signals are saved to DB. Live fee viability per asset is tracked in a module-level dict in `main.py`, pushed to the dashboard via the existing SocketIO `overview_update` event.

**Tech Stack:** Python 3.10+, pytest, Flask + SocketIO, SQLite (via `bot/db.py`)

---

## File Map

| File | Action | What changes |
|---|---|---|
| `bot/indicators.py` | Modify | Add `is_fee_viable()` function |
| `bot/strategies/mean_reversion.py` | Modify | Import `is_fee_viable`, read `tp_atr_multiplier` in evaluate, add fee check for long and short blocks |
| `bot/strategies/funding_arb.py` | Modify | Import `is_fee_viable`, add fee check for long and short blocks |
| `bot/strategies/order_flow.py` | Modify | Import `is_fee_viable`, add fee check for long and short blocks |
| `main.py` | Modify | Add `_asset_live_status` dict, `get_asset_live_status()`, update in `process_asset` |
| `dashboard/app.py` | Modify | Include `asset_status` in `/api/overview` response and `overview_update` SocketIO push |
| `dashboard/templates/config.html` | Modify | Add `fee_rate_round_trip` field to fieldMap + Risk section HTML |
| `dashboard/templates/overview.html` | Modify | Add "Ativos Monitorados" section with fee viability badges |
| `tests/__init__.py` | Create | Empty (makes tests/ a package) |
| `tests/test_indicators.py` | Create | Tests for `is_fee_viable` |
| `tests/test_strategies.py` | Create | Tests for fee filter in all three strategies |

---

## Task 1: Add `is_fee_viable` to `bot/indicators.py`

**Files:**
- Modify: `bot/indicators.py`
- Create: `tests/__init__.py`
- Create: `tests/test_indicators.py`

- [ ] **Step 1: Install pytest**

Run from `hyperliquid-bot/` directory:
```bash
pip install pytest
```

- [ ] **Step 2: Create `tests/__init__.py`**

Create empty file `tests/__init__.py`.

- [ ] **Step 3: Write the failing tests**

Create `tests/test_indicators.py`:
```python
from bot.indicators import is_fee_viable


def test_is_fee_viable_blocked_spec_example():
    # BTC: ATR=$20, price=$66747, tp=2 → (20/66747)*2 = 0.000599 < 0.0009
    assert is_fee_viable(20.0, 66747.0, 2.0, 0.0009) is False


def test_is_fee_viable_allowed_spec_example():
    # BTC: ATR=$35, price=$66747, tp=2 → (35/66747)*2 = 0.001049 > 0.0009
    assert is_fee_viable(35.0, 66747.0, 2.0, 0.0009) is True


def test_is_fee_viable_default_fee_rate():
    # default fee_rate=0.0009 should match explicit
    assert is_fee_viable(20.0, 66747.0, 2.0) is False
    assert is_fee_viable(35.0, 66747.0, 2.0) is True


def test_is_fee_viable_exact_boundary():
    # (atr/price)*tp == fee_rate → not strictly greater → False
    # atr=30, price=100000, tp=3 → (30/100000)*3 = 0.0009 → not > 0.0009
    assert is_fee_viable(30.0, 100000.0, 3.0, 0.0009) is False


def test_is_fee_viable_just_above_boundary():
    # atr=30.01 → (30.01/100000)*3 = 0.0009003 > 0.0009 → True
    assert is_fee_viable(30.01, 100000.0, 3.0, 0.0009) is True
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
cd hyperliquid-bot
pytest tests/test_indicators.py -v
```

Expected: `ImportError` or `AttributeError: module 'bot.indicators' has no attribute 'is_fee_viable'`

- [ ] **Step 5: Add `is_fee_viable` to `bot/indicators.py`**

Add this function after `calc_volume_avg` and before `compute_all` (after line 30, before line 33):

```python
def is_fee_viable(atr: float, price: float, tp_multiplier: float, fee_rate: float = 0.0009) -> bool:
    """Return True only if expected TP profit exceeds round-trip fee cost."""
    return (atr / price) * tp_multiplier > fee_rate
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_indicators.py -v
```

Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add bot/indicators.py tests/__init__.py tests/test_indicators.py
git commit -m "feat: add is_fee_viable() to indicators and test suite"
```

---

## Task 2: Fee filter in `mean_reversion.py`

**Files:**
- Modify: `bot/strategies/mean_reversion.py`
- Modify: `tests/test_strategies.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_strategies.py`:
```python
import pytest
import pandas as pd
from bot.strategies.mean_reversion import MeanReversionStrategy


INDICATORS_BASE = {
    "ema9": 100.0,
    "ema21": 99.0,
    "rsi2": 10.0,
    "volume": 2.0,
    "volume_avg": 1.0,
    "atr": 0.05,         # very small — (0.05/100)*1.5 = 0.00075 < 0.0009 → BLOCKED
    "close_1m": 100.0,
    "close_5m": 100.0,
}

# ── Mean Reversion ────────────────────────────────────────────────────────────

class TestMeanReversionFeeFilter:
    def setup_method(self):
        self.strategy = MeanReversionStrategy()
        self.cfg = {"fee_rate_round_trip": "0.0009"}
        self.params = {
            "rsi_oversold": "15",
            "rsi_overbought": "85",
            "funding_rate_limit": "0.0005",
            "volume_multiplier": "1.3",
            "tp_atr_multiplier": "1.5",
            "sl_atr_multiplier": "1.0",
        }

    def test_fee_blocked_long_inserts_signal(self, monkeypatch):
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))

        # LONG conditions met: ema9>ema21, rsi2<15, volume>vol_avg*1.3
        indicators = {**INDICATORS_BASE, "ema9": 100.0, "ema21": 99.0, "rsi2": 10.0}
        result = self.strategy.evaluate("BTC", indicators, 0.0, self.cfg, self.params)

        assert result is None
        assert len(signals) == 1
        assert signals[0]["side"] == "long"
        assert "ATR insuficiente" in signals[0]["reason"]
        assert "atr_pct" in signals[0]["reason"]

    def test_fee_blocked_short_inserts_signal(self, monkeypatch):
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))

        # SHORT conditions met: ema9<ema21, rsi2>85, volume>vol_avg*1.3
        indicators = {**INDICATORS_BASE, "ema9": 99.0, "ema21": 100.0, "rsi2": 90.0}
        result = self.strategy.evaluate("BTC", indicators, 0.0, self.cfg, self.params)

        assert result is None
        assert len(signals) == 1
        assert signals[0]["side"] == "short"
        assert "ATR insuficiente" in signals[0]["reason"]

    def test_fee_allowed_long_returns_signal(self, monkeypatch):
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)

        # ATR high enough: (0.1/100)*1.5 = 0.0015 > 0.0009 → ALLOWED
        indicators = {**INDICATORS_BASE, "ema9": 100.0, "ema21": 99.0, "rsi2": 10.0, "atr": 0.1}
        result = self.strategy.evaluate("BTC", indicators, 0.0, self.cfg, self.params)

        assert result is not None
        assert result["side"] == "long"

    def test_fee_check_uses_tp_multiplier_from_params(self, monkeypatch):
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))

        # atr=0.07, price=100, tp=2.0 → (0.07/100)*2 = 0.0014 > 0.0009 → ALLOWED
        # but with tp=1.0 → (0.07/100)*1.0 = 0.0007 < 0.0009 → BLOCKED
        params_tp1 = {**self.params, "tp_atr_multiplier": "1.0"}
        indicators = {**INDICATORS_BASE, "ema9": 100.0, "ema21": 99.0, "rsi2": 10.0, "atr": 0.07}

        result = self.strategy.evaluate("BTC", indicators, 0.0, self.cfg, params_tp1)
        assert result is None
        assert len(signals) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_strategies.py::TestMeanReversionFeeFilter -v
```

Expected: tests fail (fee check not implemented yet)

- [ ] **Step 3: Modify `bot/strategies/mean_reversion.py`**

Add import at the top (after existing imports):
```python
from bot.indicators import is_fee_viable
```

Replace the `evaluate` method body. The full updated method:
```python
def evaluate(self, asset, indicators, funding_rate, cfg, params):
    rsi_oversold = float(params.get("rsi_oversold", self.DEFAULT_PARAMS["rsi_oversold"]))
    rsi_overbought = float(params.get("rsi_overbought", self.DEFAULT_PARAMS["rsi_overbought"]))
    funding_limit = float(params.get("funding_rate_limit", self.DEFAULT_PARAMS["funding_rate_limit"]))
    vol_multiplier = float(params.get("volume_multiplier", self.DEFAULT_PARAMS["volume_multiplier"]))
    tp_mult = float(params.get("tp_atr_multiplier", self.DEFAULT_PARAMS["tp_atr_multiplier"]))
    fee_rate = float(cfg.get("fee_rate_round_trip", 0.0009))

    ema9 = indicators["ema9"]
    ema21 = indicators["ema21"]
    rsi2 = indicators["rsi2"]
    volume = indicators["volume"]
    vol_avg = indicators["volume_avg"]
    vol_threshold = vol_avg * vol_multiplier
    volume_ok = volume > vol_threshold

    now = datetime.now(timezone.utc).isoformat()
    base = {
        "timestamp": now, "asset": asset,
        "executed": 0, "reason": None,
        "ema9": ema9, "ema21": ema21, "rsi2": rsi2,
        "volume": volume, "volume_avg": vol_avg,
        "atr": indicators["atr"], "funding_rate": funding_rate,
        "strategy_name": self.NAME,
    }

    if ema9 > ema21 and rsi2 < rsi_oversold and volume_ok:
        if not is_fee_viable(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate):
            atr_pct = indicators["atr"] / indicators["close_1m"]
            reason = f"ATR insuficiente para cobrir fees (atr_pct={atr_pct:.4%}, necessário={fee_rate / tp_mult:.4%})"
            log.debug(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "long", "reason": reason})
            return None
        if funding_rate > funding_limit:
            reason = f"LONG blocked: funding {funding_rate:.6f} > {funding_limit}"
            log.warning(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "long", "reason": reason})
            return None
        log.info(
            f"[{asset}] LONG signal — EMA9={ema9:.2f}>EMA21={ema21:.2f} "
            f"RSI2={rsi2:.1f}<{rsi_oversold} Vol={volume:.1f}>{vol_threshold:.1f}"
        )
        return {**base, "side": "long"}

    if ema9 < ema21 and rsi2 > rsi_overbought and volume_ok:
        if not is_fee_viable(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate):
            atr_pct = indicators["atr"] / indicators["close_1m"]
            reason = f"ATR insuficiente para cobrir fees (atr_pct={atr_pct:.4%}, necessário={fee_rate / tp_mult:.4%})"
            log.debug(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "short", "reason": reason})
            return None
        if funding_rate < -funding_limit:
            reason = f"SHORT blocked: funding {funding_rate:.6f} < -{funding_limit}"
            log.warning(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "short", "reason": reason})
            return None
        log.info(
            f"[{asset}] SHORT signal — EMA9={ema9:.2f}<EMA21={ema21:.2f} "
            f"RSI2={rsi2:.1f}>{rsi_overbought} Vol={volume:.1f}>{vol_threshold:.1f}"
        )
        return {**base, "side": "short"}

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_strategies.py::TestMeanReversionFeeFilter -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add bot/strategies/mean_reversion.py tests/test_strategies.py
git commit -m "feat: add fee viability filter to mean_reversion strategy"
```

---

## Task 3: Fee filter in `funding_arb.py`

**Files:**
- Modify: `bot/strategies/funding_arb.py`
- Modify: `tests/test_strategies.py`

- [ ] **Step 1: Add failing tests to `tests/test_strategies.py`**

Append to the end of `tests/test_strategies.py`:
```python
from bot.strategies.funding_arb import FundingArbStrategy


class TestFundingArbFeeFilter:
    def setup_method(self):
        self.strategy = FundingArbStrategy()
        self.cfg = {"fee_rate_round_trip": "0.0009"}
        self.params = {
            "funding_long_threshold": "0.001",
            "funding_short_threshold": "0.001",
            "min_volume_mult": "1.2",
            "tp_atr_multiplier": "1.5",
            "sl_atr_multiplier": "1.0",
        }
        # Volume ok: 2.0 > 1.0 * 1.2
        self.indicators = {
            "ema9": 100.0, "ema21": 99.0, "rsi2": 50.0,
            "volume": 2.0, "volume_avg": 1.0,
            "atr": 0.05,        # (0.05/100)*1.5 = 0.00075 < 0.0009 → BLOCKED
            "close_1m": 100.0, "close_5m": 100.0,
        }

    def test_fee_blocked_long_inserts_signal(self, monkeypatch):
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))

        # funding < -0.001 → LONG condition
        result = self.strategy.evaluate("ETH", self.indicators, -0.002, self.cfg, self.params)

        assert result is None
        assert len(signals) == 1
        assert signals[0]["side"] == "long"
        assert "ATR insuficiente" in signals[0]["reason"]

    def test_fee_blocked_short_inserts_signal(self, monkeypatch):
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))

        # funding > 0.001 → SHORT condition
        result = self.strategy.evaluate("ETH", self.indicators, 0.002, self.cfg, self.params)

        assert result is None
        assert len(signals) == 1
        assert signals[0]["side"] == "short"
        assert "ATR insuficiente" in signals[0]["reason"]

    def test_fee_allowed_long_returns_signal(self, monkeypatch):
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)

        # ATR high enough: (0.1/100)*1.5 = 0.0015 > 0.0009
        indicators = {**self.indicators, "atr": 0.1}
        result = self.strategy.evaluate("ETH", indicators, -0.002, self.cfg, self.params)

        assert result is not None
        assert result["side"] == "long"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_strategies.py::TestFundingArbFeeFilter -v
```

Expected: 3 failed

- [ ] **Step 3: Modify `bot/strategies/funding_arb.py`**

Add import after existing imports:
```python
from bot.indicators import is_fee_viable
```

Replace `evaluate` method body:
```python
def evaluate(self, asset, indicators, funding_rate, cfg, params):
    long_thresh = float(params.get("funding_long_threshold", self.DEFAULT_PARAMS["funding_long_threshold"]))
    short_thresh = float(params.get("funding_short_threshold", self.DEFAULT_PARAMS["funding_short_threshold"]))
    min_vol_mult = float(params.get("min_volume_mult", self.DEFAULT_PARAMS["min_volume_mult"]))
    tp_mult = float(params.get("tp_atr_multiplier", self.DEFAULT_PARAMS["tp_atr_multiplier"]))
    sl_mult = float(params.get("sl_atr_multiplier", self.DEFAULT_PARAMS["sl_atr_multiplier"]))
    fee_rate = float(cfg.get("fee_rate_round_trip", 0.0009))

    volume = indicators["volume"]
    vol_avg = indicators["volume_avg"]
    volume_ok = volume > (vol_avg * min_vol_mult)

    now = datetime.now(timezone.utc).isoformat()
    base = {
        "timestamp": now, "asset": asset,
        "executed": 0, "reason": None,
        "ema9": indicators["ema9"], "ema21": indicators["ema21"],
        "rsi2": indicators["rsi2"],
        "volume": volume, "volume_avg": vol_avg,
        "atr": indicators["atr"], "funding_rate": funding_rate,
        "strategy_name": self.NAME,
    }

    if funding_rate < -long_thresh and volume_ok:
        if not is_fee_viable(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate):
            atr_pct = indicators["atr"] / indicators["close_1m"]
            reason = f"ATR insuficiente para cobrir fees (atr_pct={atr_pct:.4%}, necessário={fee_rate / tp_mult:.4%})"
            log.debug(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "long", "reason": reason})
            return None
        log.info(f"[{asset}] FUNDING ARB LONG: rate={funding_rate:.6f} < -{long_thresh}")
        return {**base, "side": "long", "tp_atr_multiplier": tp_mult, "sl_atr_multiplier": sl_mult}

    if funding_rate > short_thresh and volume_ok:
        if not is_fee_viable(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate):
            atr_pct = indicators["atr"] / indicators["close_1m"]
            reason = f"ATR insuficiente para cobrir fees (atr_pct={atr_pct:.4%}, necessário={fee_rate / tp_mult:.4%})"
            log.debug(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "short", "reason": reason})
            return None
        log.info(f"[{asset}] FUNDING ARB SHORT: rate={funding_rate:.6f} > {short_thresh}")
        return {**base, "side": "short", "tp_atr_multiplier": tp_mult, "sl_atr_multiplier": sl_mult}

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_strategies.py::TestFundingArbFeeFilter -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add bot/strategies/funding_arb.py tests/test_strategies.py
git commit -m "feat: add fee viability filter to funding_arb strategy"
```

---

## Task 4: Fee filter in `order_flow.py`

**Files:**
- Modify: `bot/strategies/order_flow.py`
- Modify: `tests/test_strategies.py`

- [ ] **Step 1: Add failing tests to `tests/test_strategies.py`**

Append to end of `tests/test_strategies.py`:
```python
from bot.strategies.order_flow import OrderFlowStrategy


def _make_df_1m_buy(n=3):
    """n candles where close > open (buy pressure)."""
    rows = [{"timestamp": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0, "volume": 200.0}
            for i in range(n)]
    return pd.DataFrame(rows)


def _make_df_1m_sell(n=3):
    """n candles where close < open (sell pressure)."""
    rows = [{"timestamp": i, "open": 101.0, "high": 102.0, "low": 98.0, "close": 100.0, "volume": 200.0}
            for i in range(n)]
    return pd.DataFrame(rows)


class TestOrderFlowFeeFilter:
    def setup_method(self):
        self.strategy = OrderFlowStrategy()
        self.cfg = {"fee_rate_round_trip": "0.0009"}
        self.params = {
            "delta_threshold": "0.62",
            "lookback_periods": "3",
            "min_volume_mult": "1.5",
            "funding_limit": "0.001",
            "tp_atr_multiplier": "1.5",
            "sl_atr_multiplier": "1.0",
        }
        # Volume ok: 2.0 > 1.0 * 1.5 = 1.5
        self.indicators = {
            "ema9": 100.0, "ema21": 99.0, "rsi2": 50.0,
            "volume": 2.0, "volume_avg": 1.0,
            "atr": 0.05,        # (0.05/100)*1.5 = 0.00075 < 0.0009 → BLOCKED
            "close_1m": 100.0, "close_5m": 100.0,
        }

    def test_fee_blocked_long_inserts_signal(self, monkeypatch):
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))

        result = self.strategy.evaluate(
            "SOL", self.indicators, 0.0, self.cfg, self.params, df_1m=_make_df_1m_buy()
        )

        assert result is None
        assert len(signals) == 1
        assert signals[0]["side"] == "long"
        assert "ATR insuficiente" in signals[0]["reason"]

    def test_fee_blocked_short_inserts_signal(self, monkeypatch):
        signals = []
        monkeypatch.setattr("bot.db.insert_signal", lambda s: signals.append(s))

        result = self.strategy.evaluate(
            "SOL", self.indicators, 0.0, self.cfg, self.params, df_1m=_make_df_1m_sell()
        )

        assert result is None
        assert len(signals) == 1
        assert signals[0]["side"] == "short"
        assert "ATR insuficiente" in signals[0]["reason"]

    def test_fee_allowed_long_returns_signal(self, monkeypatch):
        monkeypatch.setattr("bot.db.insert_signal", lambda s: None)

        # ATR high enough: (0.1/100)*1.5 = 0.0015 > 0.0009
        indicators = {**self.indicators, "atr": 0.1}
        result = self.strategy.evaluate(
            "SOL", indicators, 0.0, self.cfg, self.params, df_1m=_make_df_1m_buy()
        )

        assert result is not None
        assert result["side"] == "long"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_strategies.py::TestOrderFlowFeeFilter -v
```

Expected: 3 failed

- [ ] **Step 3: Modify `bot/strategies/order_flow.py`**

Add import after existing imports:
```python
from bot.indicators import is_fee_viable
```

Replace `evaluate` method body:
```python
def evaluate(self, asset, indicators, funding_rate, cfg, params, df_1m=None):
    delta_thresh = float(params.get("delta_threshold", self.DEFAULT_PARAMS["delta_threshold"]))
    lookback = int(params.get("lookback_periods", self.DEFAULT_PARAMS["lookback_periods"]))
    min_vol_mult = float(params.get("min_volume_mult", self.DEFAULT_PARAMS["min_volume_mult"]))
    funding_limit = float(params.get("funding_limit", self.DEFAULT_PARAMS["funding_limit"]))
    tp_mult = float(params.get("tp_atr_multiplier", self.DEFAULT_PARAMS["tp_atr_multiplier"]))
    sl_mult = float(params.get("sl_atr_multiplier", self.DEFAULT_PARAMS["sl_atr_multiplier"]))
    fee_rate = float(cfg.get("fee_rate_round_trip", 0.0009))

    volume = indicators["volume"]
    vol_avg = indicators["volume_avg"]
    volume_ok = volume > (vol_avg * min_vol_mult)

    now = datetime.now(timezone.utc).isoformat()
    base = {
        "timestamp": now, "asset": asset,
        "executed": 0, "reason": None,
        "ema9": indicators["ema9"], "ema21": indicators["ema21"],
        "rsi2": indicators["rsi2"],
        "volume": volume, "volume_avg": vol_avg,
        "atr": indicators["atr"], "funding_rate": funding_rate,
        "strategy_name": self.NAME,
    }

    if df_1m is None or not volume_ok:
        return None

    recent = df_1m.tail(lookback)
    if len(recent) < lookback:
        return None

    buy_vol = recent.loc[recent["close"] >= recent["open"], "volume"].sum()
    sell_vol = recent.loc[recent["close"] < recent["open"], "volume"].sum()
    total_vol = buy_vol + sell_vol
    if total_vol == 0:
        return None

    buy_delta = buy_vol / total_vol

    if buy_delta >= delta_thresh:
        if not is_fee_viable(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate):
            atr_pct = indicators["atr"] / indicators["close_1m"]
            reason = f"ATR insuficiente para cobrir fees (atr_pct={atr_pct:.4%}, necessário={fee_rate / tp_mult:.4%})"
            log.debug(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "long", "reason": reason})
            return None
        if funding_rate > funding_limit:
            reason = f"ORDER_FLOW LONG blocked: funding {funding_rate:.6f}"
            db.insert_signal({**base, "side": "long", "reason": reason})
            return None
        log.info(f"[{asset}] ORDER FLOW LONG: delta={buy_delta:.3f}")
        return {**base, "side": "long", "tp_atr_multiplier": tp_mult, "sl_atr_multiplier": sl_mult}

    if buy_delta <= (1.0 - delta_thresh):
        if not is_fee_viable(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate):
            atr_pct = indicators["atr"] / indicators["close_1m"]
            reason = f"ATR insuficiente para cobrir fees (atr_pct={atr_pct:.4%}, necessário={fee_rate / tp_mult:.4%})"
            log.debug(f"[{asset}] {reason}")
            db.insert_signal({**base, "side": "short", "reason": reason})
            return None
        if funding_rate < -funding_limit:
            reason = f"ORDER_FLOW SHORT blocked: funding {funding_rate:.6f}"
            db.insert_signal({**base, "side": "short", "reason": reason})
            return None
        log.info(f"[{asset}] ORDER FLOW SHORT: delta={buy_delta:.3f}")
        return {**base, "side": "short", "tp_atr_multiplier": tp_mult, "sl_atr_multiplier": sl_mult}

    return None
```

- [ ] **Step 4: Run all strategy tests**

```bash
pytest tests/test_strategies.py -v
```

Expected: 10 passed

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v
```

Expected: 15 passed (5 indicators + 10 strategies)

- [ ] **Step 6: Commit**

```bash
git add bot/strategies/order_flow.py tests/test_strategies.py
git commit -m "feat: add fee viability filter to order_flow strategy"
```

---

## Task 5: Live asset status in `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add module-level dict and accessor after line 30 (after `_stop_event`)**

Add these two items after `_stop_event = threading.Event()`:
```python
_asset_live_status: dict[str, dict] = {}


def get_asset_live_status() -> dict:
    return dict(_asset_live_status)
```

- [ ] **Step 2: Update `process_asset` to populate live status**

After the block that checks `if indicators is None: return` (currently lines 138-139), add:
```python
    # Update live fee viability status for dashboard
    fee_rate = float(db.get_config("fee_rate_round_trip") or 0.0009)
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

The `mr_params` variable is already defined a few lines above (line 132):
```python
mr_params = db.get_strategy_config("mean_reversion").get("params", {})
```
So this reuses the already-fetched value — no extra DB call needed.

- [ ] **Step 3: Verify no import errors**

```bash
cd hyperliquid-bot
python -c "from main import get_asset_live_status; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: track live fee viability per asset in main.py"
```

---

## Task 6: Include asset status in dashboard API and SocketIO

**Files:**
- Modify: `dashboard/app.py`

- [ ] **Step 1: Update `/api/overview` endpoint**

In `api_overview` (currently lines 59-76), add `asset_status` to the returned dict.

Add import inside the function body (same pattern as other deferred imports in this file):
```python
@app.route("/api/overview")
def api_overview():
    from main import get_asset_live_status
    cfg = db.get_all_config()
    stats = db.get_trade_stats()
    open_trades = db.get_open_trades()
    daily_pnl = db.get_daily_pnl()
    total_pnl = db.get_total_pnl()

    return jsonify({
        "bot_status": cfg.get("bot_status", "stopped"),
        "use_testnet": cfg.get("use_testnet", "true"),
        "daily_pnl": round(daily_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(stats["win_rate"], 1),
        "today_count": stats["today_count"],
        "total_closed": stats["total_closed"],
        "open_trades": open_trades,
        "asset_status": get_asset_live_status(),
    })
```

- [ ] **Step 2: Update `background_pusher` to include asset status**

In `background_pusher` (currently lines 169-188), update the `socketio.emit` call:
```python
def background_pusher():
    while True:
        try:
            from main import get_asset_live_status
            cfg = db.get_all_config()
            stats = db.get_trade_stats()
            open_trades = db.get_open_trades()
            daily_pnl = db.get_daily_pnl()
            total_pnl = db.get_total_pnl()

            socketio.emit("overview_update", {
                "bot_status": cfg.get("bot_status", "stopped"),
                "daily_pnl": round(daily_pnl, 2),
                "total_pnl": round(total_pnl, 2),
                "win_rate": round(stats["win_rate"], 1),
                "today_count": stats["today_count"],
                "open_trades": open_trades,
                "asset_status": get_asset_live_status(),
            })
        except Exception:
            pass
        time.sleep(5)
```

- [ ] **Step 3: Verify import works**

```bash
python -c "from dashboard.app import create_app; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add dashboard/app.py
git commit -m "feat: include asset fee viability in overview API and SocketIO push"
```

---

## Task 7: Add `fee_rate_round_trip` field to config.html

**Files:**
- Modify: `dashboard/templates/config.html`

- [ ] **Step 1: Add input field to Risk section HTML**

In `config.html`, inside the "Gerenciamento de Risco" `config-grid` div (after the `cfg-slippage` field, before the closing `</div></div>`), add:
```html
    <div class="config-field">
      <label for="cfg-fee-rate">Fee Rate Round Trip (ex: 0.0009)</label>
      <input type="number" id="cfg-fee-rate" min="0.0001" max="0.01" step="0.0001">
    </div>
```

The section currently ends at:
```html
    <div class="config-field">
      <label for="cfg-slippage">Slippage</label>
      <input type="number" id="cfg-slippage" min="0.001" max="0.05" step="0.001">
    </div>
  </div>
</div>
```

Add the new field between the slippage field and the closing `</div></div>`.

- [ ] **Step 2: Add to `fieldMap` in the JavaScript**

In the `<script>` block, update `fieldMap` (currently lines 115-123) to add the new mapping:
```javascript
const fieldMap = {
  'cfg-address': 'account_address',
  'cfg-secret': 'secret_key',
  'cfg-assets': 'monitored_assets',
  'cfg-risk-pct': 'risk_pct_per_trade',
  'cfg-max-pos': 'max_positions',
  'cfg-max-loss': 'max_daily_loss_pct',
  'cfg-slippage': 'slippage',
  'cfg-fee-rate': 'fee_rate_round_trip',
};
```

That's the only JS change needed — `loadConfig()` and `saveConfig()` both iterate `fieldMap`, so load and save are automatically handled.

- [ ] **Step 3: Verify the page loads**

Start the server and open `http://localhost:8080/config`. The "Fee Rate Round Trip" field should appear in the Risk section. Save config and verify the value is persisted (reload the page — the field should show the saved value).

- [ ] **Step 4: Commit**

```bash
git add dashboard/templates/config.html
git commit -m "feat: add fee_rate_round_trip config field to dashboard"
```

---

## Task 8: Add asset fee viability section to overview.html

**Files:**
- Modify: `dashboard/templates/overview.html`

- [ ] **Step 1: Add HTML section**

In `overview.html`, add a new section between the KPI cards and the "Posições Abertas" section (after the closing `</div>` of `kpi-grid`, before the `<h2>Posições Abertas</h2>`):

```html
<!-- Asset Fee Viability -->
<h2 class="chart-title" style="margin-bottom:12px;">Ativos Monitorados</h2>
<div class="positions-grid" id="asset-status-grid">
  <div class="empty-state"><p>Aguardando primeiro candle...</p></div>
</div>
```

- [ ] **Step 2: Add `renderAssetStatus` JS function**

Inside the `<script>` block, add this function after `renderPositions`:
```javascript
function renderAssetStatus(assetStatus) {
  const grid = document.getElementById('asset-status-grid');
  if (!assetStatus || Object.keys(assetStatus).length === 0) {
    grid.innerHTML = '<div class="empty-state"><p>Aguardando primeiro candle...</p></div>';
    return;
  }
  grid.innerHTML = Object.entries(assetStatus).map(([asset, s]) => {
    const viable = s.fee_viable;
    const atrPct = (s.atr_pct * 100).toFixed(4);
    const reqPct = (s.required_pct * 100).toFixed(4);
    return `
      <div class="position-card">
        <div class="position-header">
          <span class="position-asset">${asset}</span>
          <span class="tag ${viable ? 'side-long' : 'side-short'}">${viable ? 'FEE VIÁ​VEL' : 'BLOQUEADO'}</span>
        </div>
        <dl class="position-details">
          <dt>ATR%</dt><dd>${atrPct}%</dd>
          <dt>Req%</dt><dd>${reqPct}%</dd>
        </dl>
      </div>
    `;
  }).join('');
}
```

> Note: `side-long` (green) is reused for viable, `side-short` (red) for blocked — consistent with existing CSS classes.

- [ ] **Step 3: Call `renderAssetStatus` from `applyOverview`**

In the `applyOverview` function, add a call at the end (after `renderPositions`):
```javascript
function applyOverview(data) {
  // ... existing code ...
  renderPositions(data.open_trades || []);
  renderAssetStatus(data.asset_status || {});
}
```

- [ ] **Step 4: Run the full test suite one final time**

```bash
pytest tests/ -v
```

Expected: 15 passed

- [ ] **Step 5: Manual smoke test**

1. Start the bot: `python run.py`
2. Open `http://localhost:8080/config` — verify "Fee Rate Round Trip" field appears with default `0.0009`
3. Open `http://localhost:8080` — verify "Ativos Monitorados" section appears (shows "Aguardando" if bot not yet processed a candle)
4. Start the bot from Config → after first candle cycle, verify badges appear (green/red per asset)
5. Open Signals page — verify fee-blocked signals appear with "ATR insuficiente para cobrir fees" reason

- [ ] **Step 6: Final commit**

```bash
git add dashboard/templates/overview.html
git commit -m "feat: show per-asset fee viability badges in overview dashboard"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task |
|---|---|
| `is_fee_viable(atr, price, tp_multiplier, fee_rate=0.0009)` in `indicators.py` | Task 1 |
| Call in `mean_reversion` with tp_multiplier from config | Task 2 |
| Call in `funding_arb` with tp_multiplier from config | Task 3 |
| Call in `order_flow` with tp_multiplier from config | Task 4 |
| Log DEBUG when filtered | Tasks 2-4 (all log.debug) |
| Save blocked signal to DB | Tasks 2-4 (db.insert_signal called) |
| `fee_rate` configurable via dashboard | Task 7 |
| Dashboard visual indicator per asset (green/red) | Task 8 |
| Live status pushed via SocketIO | Tasks 5 + 6 |
| No TP/SL logic changes | Confirmed — executor.py untouched |

### Type/signature consistency

- `is_fee_viable` defined in Task 1, called with `(indicators["atr"], indicators["close_1m"], tp_mult, fee_rate)` in Tasks 2-4 — matches signature
- `get_asset_live_status()` defined in Task 5, called in Task 6 inside function bodies — consistent
- `asset_status` key added to both `/api/overview` and `overview_update` in Task 6, consumed as `data.asset_status` in Task 8 — consistent

### No placeholders

All steps contain complete code. No TBDs.
