## Objetivo
Implementar suporte a parâmetros por ativo (asset_overrides) no BBStochStrategy e configurar
os parâmetros otimizados de backtest para BTC, ETH e SOL.

## Contexto
Arquivo: bot/strategies/bb_stoch.py
O campo `asset_overrides` já existe em DEFAULT_PARAMS mas não é usado no evaluate().
Precisa ser lido e aplicado antes de calcular os indicadores, exatamente como
bb_reversion.py faz com _resolve_params().
O fix `close_curr < bbm_curr` (long) e `close_curr > bbm_curr` (short) já está aplicado
nas linhas de long_bb e short_bb — não remover.

## O que fazer
1. Adicionar método `_resolve_params(asset, params)` em BBStochStrategy que:
   - Parte dos DEFAULT_PARAMS como base
   - Aplica os campos de `params` por cima
   - Se existir `params["asset_overrides"][asset]`, aplica por cima de tudo
   - Retorna o dict final de parâmetros efetivos

2. No início de evaluate(), substituir todas as leituras individuais de params por
   uma chamada a `p = self._resolve_params(asset, params)` e ler de `p`.

3. Adicionar suporte a filtro EMA opcional: se `ema_period` estiver em p e for > 0,
   calcular ta.ema(df_5m["close"], length=ema_period) e bloquear longs quando
   close_curr < ema_val e shorts quando close_curr > ema_val. Se ema_period == 0
   ou ausente, ignorar o filtro.

4. Atualizar DEFAULT_PARAMS com os novos campos:
   - "ema_period": 0          # 0 = sem filtro EMA
   - "stoch_short": 75        # já existe, manter
   - "bbp_short_threshold": 0.9  # já existe, manter

5. Configurar asset_overrides no arquivo de config/dashboard da estratégia bb_stoch
   com os seguintes parâmetros por ativo:

   BTC:
     bb_period: 20, bb_std: 2.0
     bbp_long_threshold: 0.10, bbp_short_threshold: 0.90
     stoch_long: 30, stoch_short: 70
     sl_pct: 0.5, tp_pct: 2.0
     ema_period: 0

   ETH:
     bb_period: 20, bb_std: 1.5
     bbp_long_threshold: 0.15, bbp_short_threshold: 0.85
     stoch_long: 25, stoch_short: 75
     sl_pct: 0.5, tp_pct: 2.0
     ema_period: 0

   SOL:
     bb_period: 15, bb_std: 1.5
     bbp_long_threshold: 0.10, bbp_short_threshold: 0.90
     stoch_long: 30, stoch_short: 70
     sl_pct: 0.5, tp_pct: 2.0
     ema_period: 200

## Lógica / Regras
- A resolução de params segue a ordem de precedência (menor para maior):
  DEFAULT_PARAMS → params globais da estratégia → asset_overrides[asset]
- smooth_k=1 no ta.stoch() é fixo e não deve virar parâmetro configurável.
- stoch_short deve ser calculado como `100 - stoch_long` por padrão se não
  especificado explicitamente no override (mantém simetria do scan).

## Observações
- Verificar como o bot armazena a config da estratégia (DB ou YAML) para saber
  onde exatamente inserir os asset_overrides — pode ser via dashboard ou direto
  no arquivo de config.
- Não alterar engine.py nem qualquer outro arquivo fora de bb_stoch.py e o config
  da estratégia.
