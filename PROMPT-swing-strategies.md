# Prompt: Swing Trading Strategies — RazorHL

## Contexto do Projeto

RazorHL é um bot de scalping Python para perpétuos na Hyperliquid. Stack: Python 3.10+, Flask+SocketIO, SQLite, pandas-ta.

**Arquitetura de estratégias existente (ler antes de codar):**

- `bot/strategies/base.py` — `BaseStrategy` ABC com `evaluate(self, asset, indicators, funding_rate, cfg, params, df_1m=None, df_5m=None)`
- `bot/strategies/manager.py` — `evaluate_all(asset, indicators, funding_rate, cfg, df_1m, df_5m, df_2m, df_15m)` → despacha por `strategy.NAME`
- `main.py` — `process_asset()` busca `df_1m`, `df_5m`, `df_15m`, chama `evaluate_all()`, rastreia `last_1m_ts` por ativo
- `bot/strategies/ema200_rsi.py` — **referência de implementação**: usa `sl_price_hint + rr_ratio`, `_insert_fee_block_signal`, padrão de bloqueio de funding

Leia `bot/strategies/ema200_rsi.py` e `bot/strategies/manager.py` completos antes de começar.

---

## Objetivo

Adicionar duas estratégias de **swing trading** com **confluência multi-timeframe** ao RazorHL:

1. **EMA Pullback Daily** (`ema_pullback_daily`) — compra pullback na EMA 20 com tendência diária
2. **RSI Divergence 4H** (`rsi_divergence_4h`) — detecta divergência RSI em 4H filtrada pelo diário

Mais um módulo compartilhado de suporte:

3. **`trend_bias.py`** — retorna `BULLISH / BEARISH / NEUTRAL` baseado no Daily

---

## Fase 1 — Infraestrutura Multi-Timeframe

### 1.1 — Fetch de candles 4H e 1D em `main.py`

Em `process_asset()`, adicionar após o fetch dos candles existentes:

```python
df_4h = client.get_candles(asset, "4h", count=300)
df_1d = client.get_candles(asset, "1d", count=300)
```

`HLClient.get_candles()` já suporta esses intervalos via Hyperliquid REST API. Não alterar `ws_client.py`.

### 1.2 — Rastrear timestamps 4H e 1D (gatilho por candle fechado)

Swing strategies só devem disparar quando o **candle do seu timeframe fechou** — não a cada tick de 1m. Adicionar tracking em `process_asset()`:

```python
# Em bot_loop(), criar os dicts ao lado de last_1m_ts:
last_4h_ts: dict[str, int] = {}
last_1d_ts: dict[str, int] = {}

# Em process_asset(), receber por parâmetro ou via nonlocal — usar o mesmo padrão de last_1m_ts
latest_4h_ts = int(df_4h["timestamp"].iloc[-1]) if not df_4h.empty else 0
latest_1d_ts = int(df_1d["timestamp"].iloc[-1]) if not df_1d.empty else 0

new_4h = latest_4h_ts > last_4h_ts.get(asset, 0)
new_1d = latest_1d_ts > last_1d_ts.get(asset, 0)

if new_4h:
    last_4h_ts[asset] = latest_4h_ts
if new_1d:
    last_1d_ts[asset] = latest_1d_ts
```

Passar `new_4h` e `new_1d` para `evaluate_all()`.

### 1.3 — Atualizar assinaturas

**`base.py`** — adicionar `df_4h=None, df_1d=None` na assinatura abstrata:

```python
def evaluate(self, asset, indicators, funding_rate, cfg, params,
             df_1m=None, df_5m=None, df_2m=None, df_4h=None, df_1d=None) -> dict | None:
```

**IMPORTANTE**: Todas as estratégias existentes implementam `evaluate()`. Adicionar `**kwargs` na assinatura do `ABC` — ou adicionar `df_2m=None, df_4h=None, df_1d=None` em todas as subclasses existentes para não quebrar. Verificar qual abordagem não quebra os testes.

**`manager.py`** — adicionar na assinatura de `evaluate_all()`:

```python
def evaluate_all(asset, indicators, funding_rate, cfg,
                 df_1m=None, df_5m=None, df_2m=None, df_15m=None,
                 df_4h=None, df_1d=None, new_4h=False, new_1d=False) -> list[dict]:
```

No dispatch loop, adicionar os novos casos:

```python
elif strategy.NAME == "ema_pullback_daily":
    signal = strategy.evaluate(asset, indicators, funding_rate, cfg, params,
                               df_1d=df_1d, new_1d=new_1d)
elif strategy.NAME == "rsi_divergence_4h":
    signal = strategy.evaluate(asset, indicators, funding_rate, cfg, params,
                               df_4h=df_4h, df_1d=df_1d, new_4h=new_4h)
```

Registrar ambas em `REGISTERED_STRATEGIES` e `STRATEGY_PRIORITY` (prioridade baixa: 10 e 11 — swing não deve sobrescrever scalp).

---

## Fase 2 — `bot/strategies/trend_bias.py`

Módulo puro (sem I/O, sem estado global). Retorna o viés de tendência baseado no Daily.

```python
"""
Shared trend-bias helper for swing strategies.
Returns BULLISH / BEARISH / NEUTRAL based on daily candles.
Pure function — no I/O, no state.
"""
from enum import Enum
import pandas_ta as ta


class TrendBias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def get_trend_bias(df_1d, ema_period: int = 200) -> TrendBias:
    """
    BULLISH  → close[-1] > EMA(ema_period) on Daily
    BEARISH  → close[-1] < EMA(ema_period) on Daily
    NEUTRAL  → insuficient data
    """
    if df_1d is None or len(df_1d) < ema_period + 1:
        return TrendBias.NEUTRAL

    ema = ta.ema(df_1d["close"], length=ema_period)
    if ema is None or ema.iloc[-1] != ema.iloc[-1]:  # NaN check
        return TrendBias.NEUTRAL

    close = df_1d["close"].iloc[-1]
    ema_val = ema.iloc[-1]

    if close > ema_val:
        return TrendBias.BULLISH
    return TrendBias.BEARISH
```

---

## Fase 3 — `bot/strategies/ema_pullback_daily.py`

### Lógica completa

**Timeframe**: Daily (df_1d)
**Gatilho**: apenas quando `new_1d=True`

**Confluências LONG (todas devem ser verdadeiras):**
1. `trend_bias` = BULLISH (close_1d > EMA 200 Daily)
2. Close atual tocou a zona EMA 20 Daily: `abs(close - ema20) / close < pullback_zone_pct` (default 0.5%)
3. Candle atual fechou ACIMA da EMA 20 (pullback rejeitado, não rompeu)
4. RSI 14 Daily não sobrecomprado: `rsi_1d < rsi_max_long` (default 65)
5. Volume diário ≥ `vol_avg_20d × vol_multiplier` (evita dias de liquidez zero)

**Confluências SHORT (espelho):**
1. `trend_bias` = BEARISH
2. Close tocou zona EMA 20 Daily por cima
3. Candle fechou ABAIXO da EMA 20
4. RSI 14 Daily não sobrevendido: `rsi_1d > rsi_min_short` (default 35)
5. Volume ok

**SL/TP:**
- `sl_price_hint` = low do candle diário atual (LONG) ou high (SHORT)
- `rr_ratio = 2.5` (swing precisa de RR maior que scalp)

**DEFAULT_PARAMS:**
```python
DEFAULT_PARAMS = {
    "ema_fast_period": 20,       # EMA de pullback (Daily)
    "ema_trend_period": 200,     # EMA de tendência (Daily) — usado via trend_bias
    "rsi_period": 14,            # RSI Daily
    "rsi_max_long": 65,          # Bloqueia long acima desse RSI
    "rsi_min_short": 35,         # Bloqueia short abaixo desse RSI
    "pullback_zone_pct": 0.005,  # 0.5% de distância da EMA20 para considerar pullback
    "vol_multiplier": 0.8,       # Dias de baixíssimo volume bloqueiam (< 0.8× média)
    "tp_atr_multiplier": 3.0,    # Usado APENAS no is_fee_viable()
    "funding_rate_limit": 0.0005,
}
```

**Implementação — seguir exatamente o padrão de `ema200_rsi.py`:**
- Calcular indicadores internamente via pandas-ta
- Verificar `new_1d`; se `False`, retornar `None` imediatamente (sem log)
- `is_fee_viable()` antes de qualquer log de sinal
- `_insert_fee_block_signal()` para bloqueios de fee
- Bloquear funding excessivo igual às outras estratégias
- Log informativo no sinal: mostrar `close`, `ema20`, `rsi_1d`

---

## Fase 4 — `bot/strategies/rsi_divergence_4h.py`

### Lógica completa

**Timeframe**: 4H (df_4h)
**Gatilho**: apenas quando `new_4h=True`

**Detecção de divergência (simplificada para bot):**

Para divergência **bullish** (LONG):
1. Encontrar o mínimo de close nos últimos `lookback_candles` candles 4H (índice `i_low`)
2. Encontrar o mínimo de close nos `lookback_candles` candles ANTES de `i_low` (índice `i_prev_low`)
3. Divergência confirmada se: `close[i_low] < close[i_prev_low]` AND `rsi[i_low] > rsi[i_prev_low]`
4. O candle `i_low` deve ser recente: `i_low >= len(df_4h) - 3` (divergência nos últimos 3 candles)

Para divergência **bearish** (SHORT) — espelho:
1. Máximo de close nos últimos `lookback_candles`
2. `close[i_high] > close[i_prev_high]` AND `rsi[i_high] < rsi[i_prev_high]`
3. `i_high >= len(df_4h) - 3`

**Confluências LONG (todas devem ser verdadeiras):**
1. Divergência bullish detectada (acima)
2. `trend_bias(df_1d)` ≠ BEARISH — não operar contra a tendência diária (BULLISH ou NEUTRAL ok)
3. Close 4H está abaixo ou na zona da EMA 50 4H: `close <= ema50_4h * (1 + level_zone_pct)` — divergência perto de suporte é mais forte
4. Volume no candle de divergência: `vol[i_low] >= vol_avg × vol_multiplier`

**Confluências SHORT:**
1. Divergência bearish detectada
2. `trend_bias(df_1d)` ≠ BULLISH
3. Close 4H está acima ou na zona da EMA 50 4H
4. Volume ok

**SL/TP:**
- `sl_price_hint` = low do candle de divergência `i_low` − pequena margem (LONG) ou high `i_high` + margem (SHORT)
- Margem: `atr_4h × 0.1` para evitar SL no exato low
- `rr_ratio = 2.0`

**DEFAULT_PARAMS:**
```python
DEFAULT_PARAMS = {
    "rsi_period": 14,
    "lookback_candles": 20,      # Janela para buscar os dois mínimos/máximos
    "ema_level_period": 50,      # EMA 4H usada como nível de suporte/resistência
    "level_zone_pct": 0.01,      # 1% de distância da EMA50 para considerar "na zona"
    "vol_multiplier": 1.0,       # Volume no candle de divergência ≥ vol_avg × este valor
    "tp_atr_multiplier": 3.0,    # Usado APENAS no is_fee_viable()
    "funding_rate_limit": 0.0005,
    "rr_ratio": 2.0,
}
```

**Implementação:**
- Calcular ATR 14 em 4H internamente (para margem do SL)
- Calcular volume médio dos últimos 20 candles 4H internamente
- Verificar `new_4h`; se `False`, retornar `None` imediatamente
- Seguir exatamente o padrão de `ema200_rsi.py` para fee check, funding block, log e estrutura do signal dict

---

## Regras de Implementação

**Obrigatório seguir (mesmas regras do projeto):**

1. Arquivo ≤ 300 linhas, função ≤ 40 linhas — se necessário, extrair helper
2. **TDD**: escrever testes em `tests/strategies/test_ema_pullback_daily.py` e `tests/strategies/test_rsi_divergence_4h.py` ANTES do código de produção
3. **Não alterar** nenhuma estratégia existente além de adicionar `df_4h=None, df_1d=None` na assinatura se necessário
4. Usar `get_logger(f"strategies.{self.NAME}")` para logging
5. `NAME` e `DISPLAY_NAME` para o dashboard (ex: `"EMA Pullback Daily (1D)"`)
6. Ambas as estratégias **desabilitadas por padrão** (`strategy.<name>.enabled = false`)
7. Registrar em `REGISTERED_STRATEGIES` e `STRATEGY_PRIORITY` em `manager.py`

---

## Ordem de Implementação

1. Testes unitários para `trend_bias.py` → implementar `trend_bias.py`
2. Testes unitários para `ema_pullback_daily.py` → implementar
3. Testes unitários para `rsi_divergence_4h.py` → implementar
4. Atualizar `base.py` (assinatura)
5. Atualizar `manager.py` (dispatch + registro + prioridade)
6. Atualizar `main.py` (fetch 4H/1D + tracking + passar flags)
7. Rodar `pytest` — suíte completa verde antes de concluir

---

## Critério de Sucesso por Fase

**Fase 1**: `pytest` passa sem erros; `main.py` busca `df_4h` e `df_1d` sem quebrar o loop
**Fase 2**: `test_trend_bias.py` passa; função pura sem I/O
**Fase 3**: `test_ema_pullback_daily.py` passa; estratégia retorna `None` quando `new_1d=False`; retorna sinal apenas com todas as confluências presentes
**Fase 4**: `test_rsi_divergence_4h.py` passa; divergência detectada corretamente em dado sintético
**Final**: `pytest` full suite verde + estratégias aparecem no dashboard com status "disabled"
