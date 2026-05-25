# Prompt: Estratégias 1H — RazorHL

## Contexto

RazorHL tem estratégias existentes de scalp (1m/2m/5m). Vamos portar três delas para **1H** criando novos arquivos — sem modificar as originais. A lógica é idêntica, só muda o timeframe, o RR e o gatilho de candle.

Leia antes de começar:
- `bot/strategies/triple_ema.py` — referência de triple EMA
- `bot/strategies/momentum_ema_macd.py` — referência de momentum MACD
- `bot/strategies/ema200_rsi.py` — referência de EMA200+RSI
- `bot/strategies/manager.py` — como registrar e despachar
- `main.py` — como candles são buscados e passados para `evaluate_all()`

---

## Objetivo

Criar três estratégias 1H e a infraestrutura necessária:

1. `triple_ema_1h.py` — Triple EMA 25/50/100 em 1H com pullback + trigger
2. `momentum_macd_1h.py` — EMA50/200 + MACD histogram zero-cross em 1H
3. `ema200_rsi_1h.py` — EMA200 + RSI14 + Outside Bar em 1H

---

## Fase 1 — Infraestrutura 1H em `main.py`

### Fetch de candles 1H

Em `process_asset()`, adicionar após os fetches existentes:

```python
df_1h = client.get_candles(asset, "1h", count=300)
```

### Rastrear timestamp 1H (gatilho por candle fechado)

As estratégias 1H só devem disparar quando um **novo candle de 1H fechou**. Adicionar ao lado de `last_1m_ts` em `bot_loop()`:

```python
last_1h_ts: dict[str, int] = {}
```

Em `process_asset()` (receber por parâmetro seguindo o mesmo padrão de `last_1m_ts`):

```python
latest_1h_ts = int(df_1h["timestamp"].iloc[-1]) if not df_1h.empty else 0
new_1h = latest_1h_ts > last_1h_ts.get(asset, 0)
if new_1h:
    last_1h_ts[asset] = latest_1h_ts
```

Passar `df_1h=df_1h, new_1h=new_1h` para `evaluate_all()`.

---

## Fase 2 — Atualizar `manager.py` e `base.py`

### `base.py`

Adicionar `df_1h=None, new_1h=False` na assinatura abstrata:

```python
def evaluate(self, asset, indicators, funding_rate, cfg, params,
             df_1m=None, df_5m=None, df_2m=None, df_4h=None, df_1d=None,
             df_1h=None, new_1h=False) -> dict | None:
```

Verificar que as estratégias existentes não quebram — adicionar os novos kwargs nas subclasses se necessário.

### `manager.py`

Adicionar `df_1h=None, new_1h=False` na assinatura de `evaluate_all()`.

Importar e registrar as três novas estratégias em `REGISTERED_STRATEGIES`.

Adicionar dispatch para cada uma:

```python
elif strategy.NAME in ("triple_ema_1h", "momentum_macd_1h", "ema200_rsi_1h"):
    signal = strategy.evaluate(asset, indicators, funding_rate, cfg, params,
                               df_1h=df_1h, new_1h=new_1h)
```

Adicionar em `STRATEGY_PRIORITY` (prioridade após as estratégias de scalp existentes):

```python
"triple_ema_1h": 9,
"momentum_macd_1h": 10,
"ema200_rsi_1h": 11,
```

---

## Fase 3 — `bot/strategies/triple_ema_1h.py`

**Port direto de `triple_ema.py`** — mesma lógica, timeframe 1H.

Diferenças em relação ao original:
- Recebe `df_1h` em vez de `df_2m`
- Guarda na primeira linha: `if not new_1h or df_1h is None or len(df_1h) < 102: return None`
- `NAME = "triple_ema_1h"`, `DISPLAY_NAME = "Triple EMA 25/50/100 (1H)"`
- `rr_ratio = 2.0` (maior que o 1.5 do scalp — 1H precisa de RR maior)
- `tp_atr_multiplier = 2.0` no DEFAULT_PARAMS (para `is_fee_viable`)
- Volume: usar volume do próprio `df_1h` (média dos últimos 20 candles 1H), não o `indicators["volume_avg"]` que é do 1m

```python
DEFAULT_PARAMS = {
    "pullback_threshold": 0.003,
    "vol_multiplier": 1.2,
    "tp_atr_multiplier": 2.0,
    "funding_rate_limit": 0.0005,
}
```

**Volume em 1H** — calcular internamente:
```python
vol_avg_1h = df_1h["volume"].rolling(20).mean().iloc[-1]
volume_ok = df_1h["volume"].iloc[-1] > vol_avg_1h * vol_multiplier
```

Não usar `indicators["volume"]` nem `indicators["volume_avg"]` — esses são do 1m.

---

## Fase 4 — `bot/strategies/momentum_macd_1h.py`

**Port direto de `momentum_ema_macd.py`** — mesma lógica, timeframe 1H.

Diferenças:
- Recebe `df_1h` em vez de `df_5m`
- Guarda: `if not new_1h or df_1h is None or len(df_1h) < 202: return None`
- `NAME = "momentum_macd_1h"`, `DISPLAY_NAME = "Momentum EMA+MACD (1H)"`
- Calcular EMA50, EMA200, MACD sobre `df_1h["close"]`
- Volume interno via `df_1h` (mesmo padrão acima)
- `rr_ratio = 2.0` — este usa ATR mode (sem `sl_price_hint`), manter `tp_atr_multiplier` e `sl_atr_multiplier`
- Aumentar `tp_atr_multiplier = 3.0` e `sl_atr_multiplier = 1.5` (candles 1H têm ATR maior que 5m)

```python
DEFAULT_PARAMS = {
    "vol_multiplier": 1.2,
    "tp_atr_multiplier": 3.0,
    "sl_atr_multiplier": 1.5,
    "funding_rate_limit": 0.0005,
}
```

---

## Fase 5 — `bot/strategies/ema200_rsi_1h.py`

**Port direto de `ema200_rsi.py`** — mesma lógica, timeframe 1H.

Diferenças:
- Recebe `df_1h` em vez de `df_5m`
- Guarda: `if not new_1h or df_1h is None or len(df_1h) < 202: return None`
- `NAME = "ema200_rsi_1h"`, `DISPLAY_NAME = "EMA200 + RSI14 + Outside Bar (1H)"`
- Calcular EMA200 e RSI14 sobre `df_1h["close"]`
- Outside bar sobre `df_1h.iloc[-1]` e `df_1h.iloc[-2]`
- Volume interno via `df_1h`
- `rr_ratio = 2.5` (outside bar em 1H é setup mais forte, merece RR maior)
- `tp_atr_multiplier = 2.5` no DEFAULT_PARAMS

```python
DEFAULT_PARAMS = {
    "rsi_period": 14,
    "vol_multiplier": 1.2,
    "tp_atr_multiplier": 2.5,
    "funding_rate_limit": 0.0005,
}
```

---

## Regras de Implementação

1. **Não modificar** nenhuma estratégia existente — apenas criar arquivos novos
2. **TDD**: escrever testes em `tests/strategies/test_triple_ema_1h.py`, `test_momentum_macd_1h.py`, `test_ema200_rsi_1h.py` antes do código de produção
3. Todas as novas estratégias **desabilitadas por padrão**
4. Volume SEMPRE calculado a partir do `df_1h` — nunca usar `indicators["volume_avg"]` que é do 1m
5. Guarda `new_1h` como **primeira linha** do evaluate — sem processar nada se candle 1H não fechou
6. Seguir exatamente o padrão de bloqueio de fee e funding das estratégias originais

---

## Ordem de Implementação

1. Testes → `trend_bias.py` (se ainda não existir do prompt anterior)
2. Infraestrutura: `main.py` + `base.py` + `manager.py`
3. Testes → `triple_ema_1h.py` → implementar
4. Testes → `momentum_macd_1h.py` → implementar
5. Testes → `ema200_rsi_1h.py` → implementar
6. `pytest` full suite verde

---

## Critério de Sucesso

- `pytest` passa sem erros
- Com `new_1h=False`, todas as três estratégias retornam `None` imediatamente
- Com `new_1h=True` e dados sintéticos com setup válido, retornam signal correto
- As três aparecem no dashboard como estratégias desabilitadas
- Nenhuma estratégia existente foi modificada (verificar via `git diff`)
