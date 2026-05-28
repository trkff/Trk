# Strategy Fidelity Checker — Design

**Data:** 2026-05-28
**Branch sugerida:** `feat/fidelity-checker`
**Status:** Spec aprovado, pendente implementation plan

---

## 1. Problema

O bot RazorHL roda 25+ instâncias de estratégias ao vivo na Lighter. Cada uma foi validada via `bot/backtest/engine.py` ou `scanner_v2` antes de virar produção. Mas existe drift estrutural possível entre o que o backtest aprovou e o que o live de fato executa:

- Vela ainda em formação vazando para `df.close.iloc[-1]` (caso real HYPE 5m: backtest viu 62.4694, live viu 62.53).
- Indicadores divergindo silenciosamente (cache stale, warmup diferente, fórmula diferente).
- Sinais perdidos por WS gap ou bloqueados por filtros de risco que o backtest ignora.
- Trades fechando por motivos diferentes (TP vs SL vs bb_mid_exit) devido a prioridade per-candle vs ordem de trigger real na exchange.
- Slippage de execução erodindo edge invisível no relatório do scanner.

Hoje só descobrimos divergência depois que uma estratégia performa abaixo do esperado por dias. Falta uma ferramenta que responda, sob demanda: **"essa estratégia, neste período, está fiel ao backtest? Se não, em quais candles/trades exatamente e por quê?"**

---

## 2. Objetivo

Construir uma página `/fidelity` no dashboard que, sob demanda, rode o backtest canônico (`engine.py`) sobre o mesmo período que o live operou e produza um relatório em 3 camadas — sinais, trades, métricas agregadas — com drill-down até o candle individual e diagnóstico textual da causa provável.

### Não-objetivos (v1)

- Verificação contínua/automática (descartada deliberadamente; v1 é on-demand).
- Histórico de mudanças de params durante o período (usa params atuais; documentado como caveat).
- Histórico de enabled/disabled granular além de um aviso macro.
- Export CSV/JSON dos diffs.
- Comparação entre 2 períodos do mesmo run.

---

## 3. Princípio de design

`bot/backtest/engine.py` é a **fonte da verdade canônica**. A verificação consiste em rodá-lo sobre o mesmo período em que o live operou e cruzar resultados. Nunca duplicar lógica de sinal entre live e checker — se a fórmula da estratégia mudar, ambos os lados refletem juntos.

---

## 4. Arquitetura

```
UI: /fidelity
├── Form: estratégia | período | profile | [Verificar]
├── Cards de score (ordenados pelo pior)
└── Drill-down (tabs: Sinais | Trades | Métricas) + modal lado-a-lado

       ↕  POST /api/fidelity/run  →  job async
       ↕  GET  /api/fidelity/status/<job_id>
       ↕  GET  /api/fidelity/runs[/<id>[/diffs]]

bot/fidelity/checker.py (novo)
└── run_check(strategy, asset, days, profile_id) →
    1. Snapshot live: signals + trades do período (filtrado por profile_id)
    2. Rerun bt: engine._run_backtest(..., return_signals=True) com params do DB
    3. Compare 3 camadas: signal_diff / trade_diff / metric_diff
    4. Persist em fidelity_runs + fidelity_diffs
    5. Atribuir causa provável por heurística → coluna notes

bot/backtest/engine.py (alteração)
└── _run_backtest(..., return_signals=False) novo modo retorna
    (trades, signals_array_with_indicators) quando True

bot/db.py (migrations)
└── M9: ALTER TABLE signals ADD COLUMN indicators_json TEXT
└── M10: CREATE TABLE fidelity_runs, fidelity_diffs

bot/strategies/*.py (8 famílias)
└── evaluate() popula indicators_json no signal dict via helper compartilhado
```

---

## 5. Modelo de dados

### Tabela `fidelity_runs`

| Coluna | Tipo | Descrição |
|---|---|---|
| `id` | INTEGER PK | Auto |
| `created_at` | TEXT | ISO timestamp |
| `profile_id` | INTEGER | Perfil cujos signals/trades foram comparados |
| `strategy` | TEXT | Nome da instância (ex: `bb_stoch_btc_5m`) |
| `asset` | TEXT | Ativo |
| `timeframe` | TEXT | `5m` / `15m` / `30m` / `1h` |
| `period_start_ms` | INTEGER | Início da janela |
| `period_end_ms` | INTEGER | Fim da janela (clampado em `now_ms - tf_ms` pra evitar vela aberta) |
| `params_json` | TEXT | Snapshot dos params usados |
| `live_signals` | INTEGER | Contagem de sinais live no período |
| `bt_signals` | INTEGER | Contagem de sinais do backtest |
| `matched` | INTEGER | Sinais que bateram (mesmo ts, mesmo side, dentro das tolerâncias) |
| `phantom` | INTEGER | Live emitiu, bt não |
| `missed` | INTEGER | Bt emitiu, live não |
| `side_mismatch` | INTEGER | Mesmo ts, side trocado |
| `price_drift` | INTEGER | `|Δsignal_price| > 0.05%` |
| `indicator_drift` | INTEGER | `|Δ| > 1%` em qualquer indicador comparável |
| `fidelity_score` | REAL | 0..1 (fórmula abaixo) |
| `live_metrics_json` | TEXT | `{wr, pf, roi, trades, max_dd, tpd, slip_bps}` |
| `bt_metrics_json` | TEXT | Idem |

### Tabela `fidelity_diffs`

| Coluna | Tipo | Descrição |
|---|---|---|
| `id` | INTEGER PK | Auto |
| `run_id` | INTEGER FK | → fidelity_runs.id |
| `ts_ms` | INTEGER | Candle close do evento |
| `layer` | TEXT | `signal` / `trade` / `metric` |
| `diff_type` | TEXT | `phantom` / `missed` / `side` / `price` / `indicator` / `entry_px` / `exit_px` / `exit_type` / `duration` |
| `side` | TEXT | `long` / `short` / `null` |
| `live_json` | TEXT | Snapshot lado live |
| `bt_json` | TEXT | Snapshot lado backtest |
| `delta_pct` | REAL | Magnitude quando aplicável |
| `notes` | TEXT | Causa provável (heurística textual) |

Índices: `idx_runs_strategy_created`, `idx_diffs_run_layer_type`.

### Score composto

```
fidelity_score =
    0.50 * (matched / max(live_signals, bt_signals, 1))
  + 0.20 * (1 - price_drift / max(matched, 1))
  + 0.15 * (1 - indicator_drift / max(matched, 1))
  + 0.15 * trade_outcome_match_rate
```

`trade_outcome_match_rate` = fração dos trades em que `exit_type` bateu entre live e bt.

Bandas visuais: `≥0.9` verde (Excelente), `≥0.7` amarelo (Bom), `<0.7` vermelho (Investigar).

### Migration: `signals.indicators_json`

`ALTER TABLE signals ADD COLUMN indicators_json TEXT`. Backfill NULL nos antigos. Helper em `BaseStrategy._make_indicators_json(p, df, idx)` que cada `evaluate()` chama e injeta no signal dict; `executor` e `_insert_fee_block_signal` propagam pro `db.insert_signal`. Sinais antigos (com NULL) ficam fora do `indicator_drift` mas contam pra `matched/missed/phantom`.

---

## 6. Pipeline de comparação

### 6.1 Camada 1 — Sinais

**Inputs**
- `live`: `SELECT * FROM signals WHERE strategy_name=? AND timestamp BETWEEN ? AND ? AND profile_id=?` — inclui executados e bloqueados, ambos contam como sinal emitido pela lógica da estratégia.
- `bt`: `engine._run_backtest(..., return_signals=True)` retornando (trades, signals_arr). `signals_arr` é uma estrutura por candle com `{ts_ms, side, signal_price, indicators}` para todo candle onde `_signals_<family>` emitiu long ou short.

**Algoritmo**

```python
live_by_ts = {s.ts_ms: s for s in live_signals}
bt_by_ts = {s.ts_ms: s for s in bt_signals}

for ts in sorted(set(live_by_ts) | set(bt_by_ts)):
    l, b = live_by_ts.get(ts), bt_by_ts.get(ts)
    if l and not b:
        record("signal", "phantom", ts, l, None)
    elif b and not l:
        record("signal", "missed", ts, None, b)
    elif l.side != b.side:
        record("signal", "side", ts, l, b)
    else:
        # matched candidato; checar drift
        dp = abs(l.signal_price - b.signal_price) / b.signal_price
        if dp > PRICE_TOL:
            record("signal", "price", ts, l, b, delta=dp)
        for ind, lv in l.indicators.items():
            if ind in b.indicators:
                rd = abs(lv - b.indicators[ind]) / max(abs(b.indicators[ind]), 1e-9)
                if rd > IND_TOL:
                    record("signal", "indicator", ts, l, b, delta=rd,
                           notes=f"indicator={ind}")
        # senão: matched++
```

**Clampagem:** `period_end_ms = min(user_end_ms, now_ms - tf_ms)` para nunca incluir candle ainda aberto.

### 6.2 Camada 2 — Trades

**Match por proximidade de entry_ts** (mesmo candle ou ±1 vela):

```python
for lt in live_trades:
    bt_match = find_closest(bt_trades, lt.entry_ts, window=tf_ms)
    if not bt_match:
        record("trade", "extra_live", lt.entry_ts, lt, None)
    else:
        if abs(rel(lt.entry_price, bt_match.entry_price)) > PRICE_TOL:
            record("trade", "entry_px", ..., delta=...)
        if lt.exit_type != bt_match.exit_type:
            record("trade", "exit_type", ..., notes=...)
        if abs(rel(lt.exit_price, bt_match.exit_price)) > PRICE_TOL:
            record("trade", "exit_px", ..., delta=...)
        if abs(lt.duration_candles - bt_match.duration_candles) > 0:
            record("trade", "duration", ..., delta=...)

for bt_orphan in bt_trades_sem_match:
    record("trade", "missed_trade", ...)
```

Slippage médio cai naturalmente do delta de `entry_price` (já tem `signal_price` na tabela `trades`).

### 6.3 Camada 3 — Métricas agregadas

`report.compute_metrics(live_trades, initial_capital)` e idem para `bt_trades`. Compara campo a campo. Resultado vai pra `live_metrics_json` / `bt_metrics_json`. Diffs significativos (>5% absoluto em WR/PF/ROI) viram linha em `fidelity_diffs` com `layer=metric`.

### 6.4 Diagnóstico automático (heurística → `notes`)

| diff_type | Heurística |
|---|---|
| `price` | Se Δ típico de 1 candle de magnitude → "vela aberta vazando (`_drop_open_candle`?)" |
| `phantom` | Se candle vizinho tem `indicator_drift` → "indicador divergente"; senão → "live disparou antes do close real" |
| `missed` | Cruza com `signals.reason` no live: se houver bloqueio → "filtro de risco bloqueou: {reason}"; senão → "candle não chegou no live (WS gap/REST atrasado)" |
| `indicator` | Identifica o indicador divergente; se for o primeiro candle do período → "warmup diferente"; senão → "fórmula ou cache" |
| `exit_type` | "Prioridade per-candle do engine (SL>TP>bb_mid) vs ordem real da exchange" |

---

## 7. UI

### `/fidelity`

**Zona 1 — Form** (compacto, topo)
```
[Estratégia ▾]  [Ativo (auto-preenchido)]  [Período ▾: D-1 / 7d / 30d / custom]
[Profile: ativo]  [▶ Verificar fidelidade]  [Histórico ▾]
```
Histórico = dropdown com últimos 20 `fidelity_runs` clicáveis.

**Zona 2 — Cards de score** (após run, ordenado pelo pior primeiro)

```
┌──────────────────────────────────────────────┐
│ bb_stoch_btc_5m                ★ 0.73 (Bom)  │
│ ─────────────────────────────────────────────│
│ Sinais   45/52 matched   3 phantom   4 missed│
│ Preço    91% dentro tol  (4 com drift)       │
│ Indic.   88% dentro tol  (5 com drift)       │
│ Trades   12/14 matched   exit_type 2 diff    │
│ ─────────────────────────────────────────────│
│ Live  WR 64%  PF 1.4  ROI +2.3%  slip 8 bps  │
│ BT    WR 71%  PF 1.6  ROI +3.1%              │
└──────────────────────────────────────────────┘
```

**Zona 3 — Drill-down** (ao clicar num card)

Tabs `[Sinais (N)] [Trades (N)] [Métricas]`.

- **Tab Sinais** — chips de filtro por `diff_type`. Tabela: `ts | tipo | side | signal_price (live/bt/Δ) | indicador divergente | causa provável`. Linha clicável → modal lado-a-lado com TODOS os indicadores e os 3 candles vizinhos.
- **Tab Trades** — `entry_ts | side | entry_px (live/bt/Δ bps) | exit_ts | exit_type | pnl`. Linha clicável → modal mostrando o candle de entry/exit.
- **Tab Métricas** — tabela compacta WR/PF/ROI/Trades/TPD/MaxDD/Slippage lado a lado.

**Auto-refresh:** desligado (run é caro, só roda sob ação do usuário).

### Endpoints

| Verbo + path | Função |
|---|---|
| `GET /api/fidelity/strategies` | Lista instâncias com trades fechados no perfil ativo |
| `POST /api/fidelity/run` | Body `{strategy, days, profile_id}` → `{job_id}` |
| `GET /api/fidelity/status/<job_id>` | `{status, progress, result?}` |
| `GET /api/fidelity/runs?limit=20` | Histórico (header dos runs) |
| `GET /api/fidelity/runs/<run_id>` | Dados completos do card |
| `GET /api/fidelity/runs/<run_id>/diffs?layer=&type=` | Diffs filtrados |

Job async via threading + dict registry (mesmo padrão do scanner_v2). Página excluída do `check_configured` redirect.

---

## 8. Edge cases

| Caso | Tratamento |
|---|---|
| Estratégia disabled durante parte do período | v1: header do run avisa "estratégia esteve disabled X% do período"; não filtra. v2: persistir histórico em `strategy_enabled_log` |
| CSV truncado (não cobre todo o período) | Aborta com erro claro "CSV cobre só X dias; baixe na aba Ativos" — sem comparação parcial silenciosa |
| Restart do bot (gap em signals) | Detecta via gap no heartbeat e marca candles afetados como `excluded` (não contam no score) |
| Params mudaram durante o período | Usa params atuais; documenta como caveat. v2: persistir histórico em `strategy.<n>.params_history` |
| Profiles múltiplos | `profile_id` obrigatório; query filtra por ele em signals/trades |
| Vela aberta | `period_end_ms = min(user, now - tf_ms)` |
| Tolerâncias | Defaults hardcoded em constants no checker (`PRICE_TOL=0.0005`, `IND_TOL=0.01`); expostas em `config` table (`fidelity.price_tol_pct`, `fidelity.indicator_tol_pct`) para ajuste fino |
| Co-trigger assets (WTI, HYPE) | Funciona transparente — backtest e live lêem do mesmo CSV |

---

## 9. Riscos

| Risco | Mitigação |
|---|---|
| `engine.py` precisa de novo modo `return_signals=True` sem quebrar caminho normal | Param opcional default False, retorna tuple ampliada só quando True. Teste de regressão comparando saída v1 vs v2 |
| Snapshot de indicadores aumenta DB | JSON ~200B/sinal; ~7.5MB/mês para 25 estratégias × 50 sinais/dia. Aceitável |
| Tempo de execução do run (engine × N estratégias × 30d) | Engine vetorizado ~1-3s por strategy/asset 30d. Job async com progress impede timeout HTTP |
| Modal lado-a-lado visualmente carregado | Layout 2 colunas; destaque vermelho só nos campos divergentes |
| 8 estratégias precisam ser modificadas pra popular `indicators_json` | Helper centralizado em `BaseStrategy._make_indicators_snapshot(p, df, idx)` reduz boilerplate; teste por estratégia valida que o snapshot inclui os indicadores que `_signals_<family>` usa no engine |

---

## 10. Escopo v1 (entrega)

- Migration M9: `signals.indicators_json TEXT`
- Migration M10: tabelas `fidelity_runs` + `fidelity_diffs` + índices
- Helper `BaseStrategy._make_indicators_snapshot(...)` + uso em todas as 8 famílias
- Modo `return_signals=True` em `engine._run_backtest` (com teste de regressão)
- Módulo `bot/fidelity/checker.py` com `run_check(...)` + 3 camadas de diff + heurísticas
- Endpoints `/api/fidelity/*` + job async
- Página `/fidelity` (HTML + JS + CSS) com cards, tabs, modal
- Score composto com bandas verde/amarelo/vermelho
- Histórico dos últimos 20 runs
- Constantes `fidelity.price_tol_pct` e `fidelity.indicator_tol_pct` no config table (defaults 0.0005 e 0.01)

---

## 11. Fora do escopo v1

- Histórico de mudanças de params (`params_history`)
- Histórico granular de enabled/disabled (`strategy_enabled_log`)
- Modo contínuo / passivo (worker em background)
- Cron diário
- Export CSV/JSON dos diffs
- Comparação entre 2 períodos do mesmo run
- Análise multi-estratégia num único card (cross-strategy)
