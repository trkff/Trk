# Napkin Runbook

## Curation Rules
- Re-prioritize on every read.
- Keep recurring, high-value notes only.
- Max 10 items per category.
- Each item includes date + "Do instead".

## Execution & Validation (Highest Priority)

1. **[2026-03-28] Rodar pytest a partir de `hyperliquid-bot/`**
   Do instead: `cd hyperliquid-bot && pytest tests/ -v` — não tem pytest no requirements.txt, instalar com `pip install pytest` antes.

2. **[2026-03-28] Importar `is_fee_viable` nas estratégias, não reimplementar inline**
   Do instead: `from bot.indicators import is_fee_viable` — sem circular import pois `bot.indicators` não importa strategies.

## Shell & Command Reliability

1. **[2026-03-28] Bot roda via `python run.py` na pasta `hyperliquid-bot/`**
   Do instead: sempre executar a partir do diretório `hyperliquid-bot/` para os imports relativos funcionarem.

## Execution & Validation — evaluate_all
1. **[2026-03-28] evaluate_all() retorna list[dict], não dict | None**
   Do instead: iterar `for signal in evaluate_all(...)` em process_asset — nunca `if signal is None`.

2. **[2026-03-28] Prioridade de estratégias por ativo: funding_arb > mean_reversion > order_flow**
   Do instead: usar `STRATEGY_PRIORITY` dict no manager.py; sort por prioridade e retornar só o primeiro.

## Domain Behavior Guardrails

1. **[2026-03-28] Sinais bloqueados (fee, funding) devem ser salvos no DB via `db.insert_signal`**
   Do instead: ao bloquear sinal em qualquer estratégia, sempre chamar `db.insert_signal({**base, "side": side, "reason": reason})` antes de `return None` — padrão estabelecido para funding blocks, obrigatório para fee blocks também.

2. **[2026-03-28] `mean_reversion.evaluate()` NÃO lia `tp_atr_multiplier` dos params antes da feature de fee**
   Do instead: após Task 2 do plano, o evaluate passa a ler `tp_mult = float(params.get("tp_atr_multiplier", ...))` — considerar isso ao modificar a estratégia futuramente.

3. **[2026-03-28] `fee_rate_round_trip` salvo como string na config table (padrão do DB)**
   Do instead: sempre fazer `float(cfg.get("fee_rate_round_trip", 0.0009))` ao ler — nunca assumir que é float.

4. **[2026-03-28] `_asset_live_status` em `main.py` é dict global (thread-safe via GIL do CPython)**
   Do instead: acessar via `get_asset_live_status()` no dashboard usando import dentro de função para evitar circular import — padrão já usado no arquivo (`from main import start_bot` dentro de route).

## User Directives

1. **[2026-03-28] Usuário prefere comunicação em pt-BR**
   Do instead: sempre responder em português brasileiro.
