---
tags:
  - projeto
  - trading
  - bot
  - hyperliquid
  - crypto
status: em-desenvolvimento
stack: Python 3.10+, Flask, SocketIO, SQLite, pandas-ta, Chart.js, hyperliquid-python-sdk
ultima-atualizacao: 2026-03-31
---

# RazorHL

## O que é

RazorHL é um bot autônomo de scalping para perpetuals na Hyperliquid. Ele monitora ativos em tempo real via REST e WebSocket, calcula indicadores técnicos, gera sinais de entrada com base em múltiplas estratégias, executa ordens automaticamente e gerencia risco sozinho. Vem com um dashboard web completo (dark theme) pra acompanhar tudo — trades, sinais, logs, configurações — sem precisar abrir terminal. A ideia é rodar continuamente, aprender com os próprios resultados e, no futuro, usar machine learning pra filtrar sinais ruins.

---

## Estratégia core

O bot roda um loop a cada 10 segundos. Pra cada ativo monitorado, ele busca candles de 1m e 5m, calcula indicadores (EMA, RSI, ATR, volume, VWAP, StochRSI) e alimenta um sistema de múltiplas estratégias plugáveis. Cada estratégia avalia independentemente se existe oportunidade de LONG ou SHORT. Quando mais de uma estratégia dispara no mesmo ativo, um sistema de prioridade decide qual sinal vale — `funding_arb` tem prioridade máxima, seguido por `volume_breakout`, `momentum_ema_macd`, etc.

Antes de qualquer sinal virar trade, ele passa por três filtros:
1. **Fee viability** — o ATR precisa ser grande o suficiente pra cobrir as fees (taker × 2 lados). Se o mercado tá muito parado, não vale a pena entrar.
2. **Funding rate** — funding positivo bloqueia long, negativo bloqueia short (na maioria das estratégias).
3. **Risk manager** — máximo 1% do capital por trade, máximo 2 posições abertas, pausa se perda diária >= 5%.

A execução é feita via limit IOC com slippage. TP e SL são colocados como trigger orders direto na exchange.

---

## Arquitetura resumida

```
run.py              ← Entry point (dashboard + bot na mesma instância)
main.py             ← Orquestração do loop do bot (thread separada)
bot/
  ws_client.py      ← Cliente Hyperliquid (REST + WS + reconnect)
  indicators.py     ← Indicadores técnicos via pandas-ta
  strategies/
    manager.py      ← Orquestra 8 estratégias, resolve prioridade
    base.py         ← ABC + helpers compartilhados
    mean_reversion  ← EMA + RSI-2 (única habilitada por padrão)
    funding_arb     ← Arbitragem de funding rate
    order_flow      ← Volume delta / order flow
    vwap_reversion  ← VWAP + StochRSI
    momentum_ema_macd ← EMA50/200 + MACD histogram
    volume_breakout ← BBW consolidation + volume spike
    triple_ema      ← EMA 25/50/100 em candles 2m
    ema200_rsi      ← EMA200 + RSI14 + Outside Bar em 5m
  executor.py       ← Execução de ordens (market IOC, TP/SL)
  risk.py           ← Risk manager (sizing, limites, monitoramento)
  db.py             ← SQLite (trades, config, logs, signals)
  logger.py         ← Logging (console + arquivo + SQLite)
dashboard/
  app.py            ← Flask + SocketIO (porta 8080)
  templates/        ← 5 telas: overview, trades, sinais, logs, config
  static/           ← CSS dark theme + JS (SocketIO, Chart.js)
```

Bot e dashboard compartilham o mesmo banco SQLite (`bot_data.db`). O dashboard roda como servidor Flask na 8080, o bot roda numa thread daemon controlada pelo dashboard (iniciar/pausar/parar).

---

## Brand / Identidade

- **Nome:** RazorHL (Razor + HyperLiquid)
- **Cores:** Dark theme no dashboard (detalhes no `dashboard.css`)
- **Logo:** (confirmar)

---

## Decisões técnicas relevantes

- **Polling > WebSocket para o bot loop** — a API WebSocket da HL tem instabilidades; polling REST a cada 10s é mais confiável. WS é opcional.
- **SQLite com WAL mode** — simples, embutido, thread-safe. Suficiente pro volume de dados de um bot pessoal. Quatro tabelas: `trades`, `config`, `signals`, `logs`.
- **Fee viability como guard universal** — implementado em todas as 8 estratégias. Antes de emitir qualquer sinal, verifica se `(ATR/preço) × TP_multiplier > fee_rate`. Evita trades que não cobrem as taxas.
- **Helper `_insert_fee_block_signal` na BaseStrategy** — deduplica a lógica de bloqueio por fees entre LONG e SHORT na mesma classe. Herdado por todas as estratégias.
- **Prioridade entre estratégias** — quando múltiplas disparam no mesmo ativo, o manager resolve: `funding_arb(0) > volume_breakout(1) > momentum_ema_macd(2) > vwap_reversion(3) > mean_reversion(4) > order_flow(5) > triple_ema(6) > ema200_rsi(7)`.
- **Dois modos de TP/SL** — ATR mode (padrão) e Risk/Reward mode (quando a estratégia manda `sl_price_hint` + `rr_ratio`). Triple EMA e EMA200+RSI usam o segundo modo.
- **PnL via fills API** — não calcula PnL manualmente. Busca fills reais da exchange por `oid` e agrega fee + closedPnl, incluindo fills parciais.
- **Apenas mean_reversion habilitada por padrão** — as outras 7 estratégias existem mas estão desabilitadas. Ativa pelo dashboard conforme necessidade.

---

## Status atual

### Pronto
- Loop do bot estável com reconnect automático e backoff exponencial
- 8 estratégias implementadas e testadas (mean_reversion, funding_arb, order_flow, vwap_reversion, momentum_ema_macd, volume_breakout, triple_ema, ema200_rsi)
- Dashboard completo com 5 telas (overview, trades, sinais, logs, config)
- Sistema de fee viability em todas as estratégias
- Risk manager (sizing, max posições, daily loss limit)
- Logging estruturado (console + arquivo rotacionado + SQLite)
- Suporte a testnet e mainnet (toggle pelo dashboard)
- Suite de testes com pytest (indicadores, estratégias, risk/fees)
- Cards de estratégia no dashboard com stats (trades, wins, win rate, PnL)
- Ativos monitorados no overview com indicador de fee viability (verde/vermelho)

### Em andamento / planejado
- **Fase 1 (Baseline):** validar bot estável no testnet, acumular 50–100 trades
- **Fase 2 (Optuna):** auto-otimização de parâmetros com Optuna (semanas 3–4)
- **Fase 3 (ML):** filtro XGBoost para sinais (mês 2+, requer 200+ trades)

---

## Backlog / Próximos passos

- (preencher conforme necessidade)

---

## Links úteis

- **Repo local:** `D:\Documentos\Trae\RazorHL\hyperliquid-bot\`
- **Dashboard:** `http://localhost:8080` (quando rodando)
- **Testnet HL:** `https://app.hyperliquid-testnet.xyz`
- **Mainnet HL:** `https://app.hyperliquid.xyz`
- **API base (testnet):** `https://api.hyperliquid-testnet.xyz`
- **API base (mainnet):** `https://api.hyperliquid.xyz`
- **Repo remoto:** (confirmar)
- **Deploy:** local (sem deploy cloud por enquanto)
