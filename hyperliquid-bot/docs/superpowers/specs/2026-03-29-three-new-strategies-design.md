# Design: Três Novas Estratégias — VWAPReversion, MomentumEMAMACD, VolumeBreakout

**Data:** 2026-03-29
**Status:** Aprovado
**Arquivos afetados:** `bot/indicators.py`, `bot/strategies/vwap_reversion.py` (novo), `bot/strategies/momentum_ema_macd.py` (novo), `bot/strategies/volume_breakout.py` (novo), `bot/strategies/manager.py`, `main.py`, `dashboard/templates/config.html`

---

## Contexto

O bot já possui 3 estratégias (`mean_reversion`, `funding_arb`, `order_flow`) com arquitetura plugável via `BaseStrategy` ABC e `manager.py`. Este design adiciona 3 novas estratégias seguindo exatamente os mesmos padrões.

---

## 1. Mudanças em `bot/indicators.py` — `compute_all()`

### Indicadores adicionados

| Campo | Fonte | Cálculo |
|---|---|---|
| `vwap` | df_1m (hoje) | `ta.vwap(H, L, C, V)` filtrado para UTC-hoje via datetime index |
| `stochrsi_k` | df_1m | `ta.stochrsi(close, length=stochrsi_period)` — valor atual |
| `stochrsi_d` | df_1m | `ta.stochrsi(...)` — %D atual |
| `stochrsi_k_prev` | df_1m | `ta.stochrsi(...)` — %K iloc[-2] |
| `stochrsi_d_prev` | df_1m | `ta.stochrsi(...)` — %D iloc[-2] |

`stochrsi_period` lido de `cfg.get("stochrsi_period", 14)`.

### Tratamento de NaN — campos opcionais

VWAP e StochRSI são tratados como **opcionais**: se NaN, o campo entra no dict como `None`, mas `compute_all()` **não** retorna `None`. Os indicadores obrigatórios existentes (ema9, ema21, rsi2, atr, vol_avg) continuam bloqueando com retorno `None` quando NaN.

Motivação: VWAP reseta a meia-noite UTC — nos primeiros candles do dia seria NaN. Tratar como bloqueante silenciaria `mean_reversion` e `funding_arb` sem motivo.

### BBW — não entra em `compute_all()`

`VolumeBreakout` precisa de uma **série** de N valores de BBW para detectar consolidação, não apenas o valor atual. Por isso calcula BBW internamente a partir de `df_1m`. Adicionar `bbw` ao `compute_all()` seria duplicação não utilizada que poderia bloquear estratégias se NaN.

### Merge de params em `main.py`

Para que `stochrsi_period` configurável na `vwap_reversion` chegue ao `compute_all()`, `main.py` também faz merge de `vwap_reversion` params no `effective_cfg` (mesmo padrão já existente para `mean_reversion`).

---

## 2. `bot/strategies/vwap_reversion.py`

**NAME** = `"vwap_reversion"`
**DISPLAY_NAME** = `"VWAP Reversion"`

### DEFAULT_PARAMS

```python
{
    "vwap_threshold": 0.3,      # % máxima de distância do VWAP (ex: 0.3 = 0.3%)
    "stochrsi_period": 14,
    "stochrsi_oversold": 20,
    "stochrsi_overbought": 80,
    "vol_multiplier": 1.3,
    "tp_atr_multiplier": 1.5,
    "sl_atr_multiplier": 1.0,
    "funding_rate_limit": 0.0005,
}
```

### Lógica LONG

Guard inicial: `if indicators.get("vwap") is None or indicators.get("stochrsi_k") is None: return None`

Condições:
1. `0 <= (close - vwap) / vwap <= vwap_threshold / 100` — pullback de cima para o VWAP
2. `stochrsi_k_prev < stochrsi_d_prev` AND `stochrsi_k >= stochrsi_d` — K cruza D para cima
3. `stochrsi_k < stochrsi_oversold` — zona oversold
4. `volume > vol_avg * vol_multiplier` — confirmação de volume
5. `is_fee_viable()` — guard (via `_insert_fee_block_signal` helper)
6. `funding_rate <= funding_rate_limit` — guard de funding

### Lógica SHORT (inversa)

1. `-(vwap_threshold / 100) <= (close - vwap) / vwap <= 0` — pullback de baixo para o VWAP
2. K cruza D para baixo (K_prev > D_prev AND K <= D)
3. `stochrsi_k > stochrsi_overbought`
4. Volume spike
5. `is_fee_viable()` (via helper)
6. `funding_rate >= -funding_rate_limit`

### Padrões reutilizados

- `_insert_fee_block_signal` helper (idêntico ao de `mean_reversion`)
- `base` dict com campos padrão de signal
- Return `{**base, "side": "long/short", "tp_atr_multiplier": ..., "sl_atr_multiplier": ...}`

---

## 3. `bot/strategies/momentum_ema_macd.py`

**NAME** = `"momentum_ema_macd"`
**DISPLAY_NAME** = `"Momentum EMA + MACD"`

### DEFAULT_PARAMS

```python
{
    "vol_multiplier": 1.2,
    "tp_atr_multiplier": 2.5,
    "sl_atr_multiplier": 1.2,
    "funding_rate_limit": 0.0005,
}
```

### Arquitetura (opção C)

Recebe `df_5m` diretamente na assinatura `evaluate(..., df_5m=None)`. Calcula EMA50, EMA200 e MACD **internamente**, sem depender de `compute_all()`.

Guard inicial: `if df_5m is None or len(df_5m) < 202: return None` — silencioso.

### Cálculos internos

```python
ema50  = ta.ema(df_5m["close"], length=50)
ema200 = ta.ema(df_5m["close"], length=200)
macd_df = ta.macd(df_5m["close"], fast=12, slow=26, signal=9)
# coluna do histograma: MACDh_12_26_9
hist = macd_df["MACDh_12_26_9"]
```

Verifica NaN em `ema50[-1]`, `ema200[-1]`, `hist[-1]`, `hist[-2]` → return None se qualquer um for NaN.

### Lógica LONG

1. `ema50[-1] > ema200[-1]` — tendência de alta
2. `hist[-2] <= 0` AND `hist[-1] > 0` — zero-cross do histograma para cima
3. `volume > vol_avg * vol_multiplier`
4. `is_fee_viable()`
5. `funding_rate <= funding_rate_limit`

### Lógica SHORT (inversa)

1. `ema50[-1] < ema200[-1]`
2. `hist[-2] >= 0` AND `hist[-1] < 0` — zero-cross para baixo
3-5. Igual ao LONG

---

## 4. `bot/strategies/volume_breakout.py`

**NAME** = `"volume_breakout"`
**DISPLAY_NAME** = `"Volume Breakout"`

### DEFAULT_PARAMS

```python
{
    "bbw_threshold": 0.02,
    "consolidation_periods": 10,
    "vol_multiplier": 2.0,
    "tp_atr_multiplier": 2.0,
    "sl_atr_multiplier": 1.0,
    "funding_rate_limit": 0.0005,
}
```

### Arquitetura

Recebe `df_1m` diretamente (mesmo padrão de `order_flow`). Calcula BBW internamente.

Guard inicial: `if df_1m is None or len(df_1m) < consolidation_periods + 20: return None`

### Cálculos internos

```python
bb = ta.bbands(df_1m["close"], length=20, std=2)
# colunas: BBU_20_2.0, BBM_20_2.0, BBL_20_2.0
bbw = (bb["BBU_20_2.0"] - bb["BBL_20_2.0"]) / bb["BBM_20_2.0"]

recent_bbw   = bbw.iloc[-(consolidation_periods):]
consolidating = (recent_bbw < bbw_threshold).all() and recent_bbw.notna().all()

range_high = df_1m["high"].iloc[-consolidation_periods:].max()
range_low  = df_1m["low"].iloc[-consolidation_periods:].min()
close      = df_1m["close"].iloc[-1]
```

### Lógica LONG

1. `consolidating` — últimos N candles com BBW < threshold
2. `close > range_high` — rompimento acima do range
3. `volume > vol_avg * vol_multiplier`
4. `is_fee_viable()`
5. `funding_rate <= funding_rate_limit`

### Lógica SHORT (inversa)

1. `consolidating`
2. `close < range_low`
3-5. Igual ao LONG

---

## 5. `bot/strategies/manager.py`

### Registro

```python
from bot.strategies.vwap_reversion import VWAPReversionStrategy
from bot.strategies.momentum_ema_macd import MomentumEMAMACDStrategy
from bot.strategies.volume_breakout import VolumeBreakoutStrategy

REGISTERED_STRATEGIES = [
    FundingArbStrategy(),
    VolumeBreakoutStrategy(),
    MomentumEMAMACDStrategy(),
    VWAPReversionStrategy(),
    MeanReversionStrategy(),
    OrderFlowStrategy(),
]

STRATEGY_PRIORITY = {
    "funding_arb": 0,
    "volume_breakout": 1,
    "momentum_ema_macd": 2,
    "vwap_reversion": 3,
    "mean_reversion": 4,
    "order_flow": 5,
}
```

### `evaluate_all()` — assinatura atualizada

```python
def evaluate_all(asset, indicators, funding_rate, cfg, df_1m=None, df_5m=None) -> list[dict]:
```

Dispatch:
- `df_1m` → `order_flow` e `volume_breakout`
- `df_5m` → `momentum_ema_macd`
- Demais estratégias: sem df extra

---

## 6. `main.py` — mudanças mínimas

```python
# df_5m com candles suficientes para EMA200
df_5m = client.get_candles(asset, "5m", count=210)

# Merge dos params de vwap_reversion para stochrsi_period em compute_all()
vr_params = db.get_strategy_config("vwap_reversion").get("params", {})
effective_cfg = {**cfg, **mr_params, **vr_params}

# Passa df_5m para evaluate_all
signals = evaluate_all(asset, indicators, funding_rate, effective_cfg, df_1m=df_1m, df_5m=df_5m)
```

---

## 7. `dashboard/templates/config.html`

Adicionar três funções de render:

- `renderVWAPReversionFields(strategy)` — campos: `vwap_threshold`, `stochrsi_period`, `stochrsi_oversold`, `stochrsi_overbought`, `vol_multiplier`, `tp_atr_multiplier`, `sl_atr_multiplier`, `funding_rate_limit`
- `renderMomentumEMAMACDFields(strategy)` — campos: `vol_multiplier`, `tp_atr_multiplier`, `sl_atr_multiplier`, `funding_rate_limit`
- `renderVolumeBreakoutFields(strategy)` — campos: `bbw_threshold`, `consolidation_periods`, `vol_multiplier`, `tp_atr_multiplier`, `sl_atr_multiplier`, `funding_rate_limit`

Atualizar dispatch em `renderStrategies()`:

```js
s.name === 'vwap_reversion'    ? renderVWAPReversionFields(s) :
s.name === 'momentum_ema_macd' ? renderMomentumEMAMACDFields(s) :
s.name === 'volume_breakout'   ? renderVolumeBreakoutFields(s) :
```

---

## Regras transversais

- `is_fee_viable()` chamado antes do funding check em todas as estratégias
- Sinais bloqueados (fee ou funding) sempre persistidos via `db.insert_signal()`
- Novas estratégias desabilitadas por padrão (`enabled=false` no DB inicial)
- Não alterar lógica das estratégias existentes
