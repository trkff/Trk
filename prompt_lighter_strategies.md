# Implementação de Estratégias Lighter — Prompt para Claude Code

## Contexto

O bot já possui:
- `bot/strategies/bb_stoch.py` → `BBStochStrategy` (instanciável com nome/params custom)
- `bot/strategies/bb_reversion.py` → `BBReversionStrategy` (idem)
- `bot/strategies/base.py` → `BaseStrategy`
- `bot/strategies/manager.py` → registra instâncias nomeadas que aparecem separadamente no dashboard
- As instâncias bb_reversion_btc, bb_reversion_eth, bb_reversion_sol já existem no manager

O padrão é: uma classe de estratégia + múltiplas instâncias nomeadas, cada uma com params e asset específicos.
Cada instância aparece como um item separado no dashboard.

## O que implementar

Criar dois novos arquivos de estratégia e registrar instâncias por ativo no manager.

---

## 1. `bot/strategies/stoch_scalp.py` — StochScalpStrategy

### Lógica

Entry: Stochastic %K SAI da zona extrema (sem exigir crossover com %D).
- Long:  prev_K < stoch_os  AND curr_K >= stoch_os  AND (close >= EMA(ema_period) ou ema_period==0)
- Short: prev_K > stoch_ob  AND curr_K <= stoch_ob  AND (close <= EMA(ema_period) ou ema_period==0)
  - stoch_ob = 100 - stoch_os (simétrico)

Exit (ordem de prioridade, checado candle a candle):
1. SL — candle low  <= sl_price (long) / candle high >= sl_price (short)
2. TP — candle high >= tp_price (long) / candle low  <= tp_price (short)
Sem saída pelo BB midline. Apenas TP/SL fixos.

### DEFAULT_PARAMS

```python
DEFAULT_PARAMS = {
    "stoch_k":    9,      # período do %K
    "stoch_d":    3,      # suavização %D (apenas para cálculo, não usado no sinal)
    "stoch_os":   40,     # oversold threshold (ob = 100 - os)
    "tp_pct":     0.5,    # take-profit % from entry
    "sl_pct":     0.8,    # stop-loss % from entry
    "ema_period": 50,     # trend filter; 0 = desabilitado
    "assets":     [],
    "asset_overrides": {},
}
```

### Estrutura da classe

```python
class StochScalpStrategy(BaseStrategy):
    NAME = "stoch_scalp"
    DISPLAY_NAME = "Stoch Scalp (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = { ... }

    def __init__(self, name=None, display_name=None, extra_defaults=None): ...
    def _resolve_params(self, asset, params): ...  # mesmo padrão de bb_stoch.py
    def evaluate(self, asset, indicators, funding_rate, cfg, params,
                 df_1m=None, df_5m=None, **kwargs): ...
```

evaluate() deve:
- Retornar None se `not kwargs.get("new_5m", False)` ou `df_5m is None`
- Usar `ta.stoch(..., k=stoch_k, d=stoch_d, smooth_k=1)`
- Usar `ta.ema(df_5m["close"], length=ema_period)` se ema_period > 0
- Verificar mínimo de 2 candles com valores válidos
- Retornar dict com: side, tp_pct, sl_pct, bb_mid=None, bb_mid_exit=False
  (manter campos bb_mid/bb_mid_exit por compatibilidade com engine)

---

## 2. `bot/strategies/ema_cross.py` — EMACrossStrategy

### Lógica

Entry: EMA rápida cruza acima/abaixo da EMA lenta.
- Long:  prev_fast <= prev_slow  AND curr_fast >  curr_slow  AND (close >= EMA_trend ou trend==0)
- Short: prev_fast >= prev_slow  AND curr_fast <  curr_slow  AND (close <= EMA_trend ou trend==0)

SL: dois modos controlados por `use_atr_sl`:
- `use_atr_sl=False`: SL fixo em `sl_pct` % do entry
- `use_atr_sl=True`:  SL distance = ATR(atr_period) * atr_mult (calculado no candle do sinal)
  - sl_price_long  = entry - atr_val * atr_mult
  - sl_price_short = entry + atr_val * atr_mult

Exit (ordem de prioridade):
1. SL — conforme acima
2. TP fixo — `tp_pct` % do entry
Sem saída pelo BB midline.

### DEFAULT_PARAMS

```python
DEFAULT_PARAMS = {
    "ema_fast":    9,
    "ema_slow":   21,
    "ema_trend":   0,      # trend filter period; 0 = desabilitado
    "tp_pct":      1.5,
    "sl_pct":      0.5,    # usado apenas quando use_atr_sl=False
    "use_atr_sl":  False,
    "atr_period":  14,
    "atr_mult":    1.0,
    "assets":      [],
    "asset_overrides": {},
}
```

### Estrutura da classe

```python
class EMACrossStrategy(BaseStrategy):
    NAME = "ema_cross"
    DISPLAY_NAME = "EMA Cross (5m)"
    REQUIRED_TIMEFRAMES = ["5m"]
    DEFAULT_PARAMS = { ... }

    def __init__(self, name=None, display_name=None, extra_defaults=None): ...
    def _resolve_params(self, asset, params): ...
    def evaluate(self, asset, indicators, funding_rate, cfg, params,
                 df_1m=None, df_5m=None, **kwargs): ...
```

evaluate() deve:
- Retornar None se `not kwargs.get("new_5m", False)` ou `df_5m is None`
- Calcular EMA fast, slow, trend (se ativo) com `ta.ema(...)`
- Calcular ATR com `ta.atr(df_5m["high"], df_5m["low"], df_5m["close"], length=atr_period)` se use_atr_sl
- Retornar dict com: side, tp_pct, sl_pct (calculado ou fixo), bb_mid=None, bb_mid_exit=False
  - Se use_atr_sl: incluir também `"atr_sl_dist": float(atr_val * atr_mult)` para o engine usar como SL distance absoluta
  - Nota: verificar como engine.py lida com SL — se usa sl_pct ou sl_price; adaptar conforme necessário sem alterar engine.py

---

## 3. Instâncias a registrar em `bot/strategies/manager.py`

Registrar no mesmo padrão das instâncias bb_reversion_btc/eth/sol existentes.
Cada instância recebe um nome único que aparece no dashboard.

### StochScalp — 3 instâncias

```python
stoch_scalp_xau = StochScalpStrategy(
    name="stoch_scalp_xau",
    display_name="Stoch Scalp XAU (5m)",
    extra_defaults={
        "stoch_k":    9,
        "stoch_os":   40,
        "tp_pct":     0.5,
        "sl_pct":     0.8,
        "ema_period": 50,
        "assets":     ["XAU-USD"],
    }
)

stoch_scalp_wti = StochScalpStrategy(
    name="stoch_scalp_wti",
    display_name="Stoch Scalp WTI (5m)",
    extra_defaults={
        "stoch_k":    5,
        "stoch_os":   30,
        "tp_pct":     1.0,
        "sl_pct":     1.0,
        "ema_period": 50,
        "assets":     ["WTI-USD"],
    }
)

stoch_scalp_ton = StochScalpStrategy(
    name="stoch_scalp_ton",
    display_name="Stoch Scalp TON (5m)",
    extra_defaults={
        "stoch_k":    5,
        "stoch_os":   30,
        "tp_pct":     0.5,
        "sl_pct":     1.0,
        "ema_period": 200,
        "assets":     ["TON-USD"],
    }
)
```

### EMACross — 2 instâncias

```python
ema_cross_hype = EMACrossStrategy(
    name="ema_cross_hype",
    display_name="EMA Cross HYPE (5m)",
    extra_defaults={
        "ema_fast":    9,
        "ema_slow":   21,
        "ema_trend":  200,
        "tp_pct":      1.5,
        "use_atr_sl":  True,
        "atr_period":  14,
        "atr_mult":    1.0,
        "assets":      ["HYPE-USD"],
    }
)

ema_cross_lit = EMACrossStrategy(
    name="ema_cross_lit",
    display_name="EMA Cross LIT (5m)",
    extra_defaults={
        "ema_fast":    9,
        "ema_slow":   21,
        "ema_trend":  50,
        "tp_pct":      0.5,
        "use_atr_sl":  False,
        "sl_pct":      0.5,
        "assets":      ["LIT-USD"],
    }
)
```

### BBStoch — 2 instâncias adicionais (ZEC e TON alternativo)

Usar a classe `BBStochStrategy` já existente:

```python
bb_stoch_zec = BBStochStrategy(
    name="bb_stoch_zec",
    display_name="BB Stoch ZEC (5m)",
    extra_defaults={
        "bb_period":   10,
        "bb_std":      2.0,
        "bbp_long_threshold":  0.05,
        "bbp_short_threshold": 0.95,
        "stoch_long":  30,
        "stoch_short": 70,
        "tp_pct":      0.8,
        "sl_pct":      0.8,
        "bb_mid_exit": False,   # backtest usou TP/SL fixos, sem saída pelo BBM
        "ema_period":  0,
        "assets":      ["ZEC-USD"],
    }
)

bb_stoch_ton = BBStochStrategy(
    name="bb_stoch_ton",
    display_name="BB Stoch TON (5m)",
    extra_defaults={
        "bb_period":   15,
        "bb_std":      1.5,
        "bbp_long_threshold":  0.10,
        "bbp_short_threshold": 0.90,
        "stoch_long":  25,
        "stoch_short": 75,
        "tp_pct":      0.8,
        "sl_pct":      0.8,
        "bb_mid_exit": False,   # backtest usou TP/SL fixos, sem saída pelo BBM
        "ema_period":  0,
        "assets":      ["TON-USD"],
    }
)
```

---

## 4. Arquivos a criar/modificar

| Arquivo | Ação |
|---|---|
| `bot/strategies/stoch_scalp.py` | CRIAR — classe StochScalpStrategy |
| `bot/strategies/ema_cross.py` | CRIAR — classe EMACrossStrategy |
| `bot/strategies/manager.py` | MODIFICAR — adicionar imports e registrar as 7 novas instâncias |

**NÃO modificar:**
- `engine.py`
- `bb_stoch.py` (apenas adicionar instâncias no manager)
- `bb_reversion.py`
- Qualquer outro arquivo

---

## 5. Regras gerais de implementação

- Seguir exatamente o padrão de `bb_stoch.py` para estrutura de classe, imports e logging
- `_resolve_params(asset, params)` segue a ordem: DEFAULT_PARAMS → params globais → asset_overrides[asset]
- Todos os logs devem usar `log.info(...)` e `log.debug(...)` com `[{asset}]` no início
- O dict de retorno do evaluate() deve sempre incluir os campos base:
  timestamp, asset, executed, reason, ema9, ema21, rsi2, volume, volume_avg, atr, funding_rate, strategy_name
- `bb_mid=None` e `bb_mid_exit=False` para compatibilidade com engine
- smooth_k=1 fixo no ta.stoch() (não configurável)
- Verificar se `len(df_5m) >= min_len` antes de calcular indicadores

---

## 6. Contexto de backtest — resultados que motivaram os parâmetros

Backtests em candles reais da Lighter (3 meses, 25.920 candles cada):

| Ativo | Estratégia | WR | PF | TPD | ROI 90d | DD |
|---|---|---|---|---|---|---|
| XAU | Stoch Scalp k=9 os=40 EMA50 TP=0.5% SL=0.8% | 67.0% | 1.271 | 3.0/d | +19.5% | 6.4% |
| WTI | Stoch Scalp k=5 os=30 EMA50 TP=1.0% SL=1.0% | 56.3% | 1.290 | 6.5/d | +74.2% | 14.0% |
| TON | Stoch Scalp k=5 os=30 EMA200 TP=0.5% SL=1.0% | 71.4% | 1.250 | 3.9/d | +25.0% | 11.5% |
| ZEC | BB Stoch bb=10 std=2.0 bbp=0.05 os=30 TP=0.8% SL=0.8% | 53.1% | 1.134 | 9.1/d | +40.8% | 10.4% |
| LIT | EMA Cross (9,21) EMA50 TP=0.5% SL=0.5% | 55.4% | 1.242 | 8.1/d | +47.1% | 8.7% |
| HYPE | EMA Cross (9,21) EMA200 ATR(14)×1.0 TP=1.5% | 26.1% | 1.221 | 6.6/d | +210.8%* | 24.6% |

*ROI do HYPE é em 15 meses (dados desde fev/2025).
Exchange: Lighter (zero fees). Slippage estimado: 0.01–0.025% por lado.
