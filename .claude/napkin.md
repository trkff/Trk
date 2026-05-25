# Napkin Runbook

## Curation Rules
- Re-prioritize on every read.
- Keep recurring, high-value notes only.
- Max 10 items per category.
- Each item includes date + "Do instead".

## Execution & Validation (Highest Priority)
1. **[2026-03-30] df_1m uses datetime as INDEX, not column**
   Do instead: Use `df.resample("2min")` without `on=` param. Check `ws_client.py` for df structure before resampling.

2. **[2026-03-30] Hook reminder: update CLAUDE.md when modifying Python files**
   Do instead: Always update CLAUDE.md architecture, strategy docs, and param tables when adding/modifying strategies or executor behavior.

## Shell & Command Reliability
1. **[2026-03-30] pytest runs from hyperliquid-bot/ subdirectory**
   Do instead: `cd hyperliquid-bot && python -m pytest tests/ -v`

## Lighter Exchange Gotchas
1. **[2026-05-22] Lighter TP/SL exigem ORDER_TYPE_*_LIMIT + TIF_GOOD_TILL_TIME**
   Do instead: Em `place_tp_sl`, usar `ORDER_TYPE_TAKE_PROFIT_LIMIT` (5) e `ORDER_TYPE_STOP_LOSS_LIMIT` (3) com `time_in_force=TIF_GOOD_TILL_TIME` + `order_expiry=-1`. As variantes market (2/4) retornam "OrderTimeInForce is not valid" com GTT e são canceladas imediatamente com IOC. `price` = trigger ± slippage. Mapear `'take-profit-limit'` e `'stop-loss-limit'` na consulta de active orders.

## Domain Behavior Guardrails
1. **[2026-03-30] Strategy evaluate() signature includes df_1m, df_5m as kwargs**
   Do instead: When adding new df types (df_2m), pass via new kwarg in manager dispatch, not by overloading existing params.

2. **[2026-03-30] Fee viability check BEFORE funding check in all strategies**
   Do instead: Always check `is_fee_viable()` first, then funding rate. Use `_insert_fee_block_signal` helper from BaseStrategy.

3. **[2026-03-30] Strategy test pattern: monkeypatch ta.ema/ta.macd for deterministic results**
   Do instead: Import strategy module, monkeypatch `_te.ta.ema` with fake series of correct length matching df length.
