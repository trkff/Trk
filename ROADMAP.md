# RazorHL — Plano de Implementação de Machine Learning

> Hyperliquid Scalping Bot · Evolução em 3 fases

---

## Visão Geral

Este documento descreve o roadmap de evolução do RazorHL desde o modo puramente técnico até um sistema com aprendizado de máquina. A progressão é dividida em 3 fases, cada uma dependendo dos dados acumulados pela fase anterior.

```
FASE 1 (Semanas 1–2)  →  FASE 2 (Semanas 3–4)  →  FASE 3 (Mês 2+)
  Baseline Técnico         Auto-Otimização           Machine Learning
```

---

## Fase 1 — Baseline Técnico
**Período: Semanas 1 e 2**

### Objetivo
Robô rodando estável no testnet com dados reais do mainnet, acumulando histórico de trades e sinais no SQLite. Nenhum ML ainda — foco total em validação técnica.

### Tarefas
- [ ] Corrigir o erro de compatibilidade do SDK com o testnet
- [ ] Configurar leitura de dados do mainnet + execução no testnet
- [ ] Validar que os 3 módulos de estratégia estão gerando sinais
- [ ] Confirmar que todos os trades estão sendo salvos corretamente no SQLite
- [ ] Monitorar o dashboard diariamente — logs, sinais, trades
- [ ] Observar win rate, duração média dos trades e P&L acumulado
- [ ] Ajustar manualmente os parâmetros pelo dashboard se necessário

### Métricas de sucesso
- Bot ficou online por pelo menos 5 dias sem crashes
- Tabela `signals` registrando todos os sinais (executados e bloqueados)
- Win rate calculável (não importa o valor, só que existe dado)
- Sem erros críticos nos logs por 48h seguidas

### Entregável
> Mínimo de **50–100 trades** registrados no SQLite com indicadores completos

---

## Fase 2 — Auto-Otimização com Optuna
**Período: Semanas 3 e 4**

### Objetivo
Ativar o módulo Optuna para analisar os trades acumulados e ajustar automaticamente os thresholds dos indicadores. O robô começa a aprender com seus próprios resultados.

### Tarefas
- [ ] Implementar módulo `bot/optimizer.py` com Optuna
- [ ] Definir o espaço de busca: RSI oversold/overbought, volume multiplier, funding limit, TP/SL multipliers
- [ ] Configurar job semanal automático (toda segunda-feira à meia-noite)
- [ ] Usar os trades da última semana do SQLite como dataset de otimização
- [ ] Métrica a maximizar: Sharpe Ratio (lucro / volatilidade dos retornos)
- [ ] Salvar os melhores parâmetros de volta na tabela `config` do SQLite
- [ ] Exibir no dashboard o histórico de otimizações e evolução dos parâmetros
- [ ] Adicionar toggle no dashboard para ativar/desativar a auto-otimização

### Instalação
```bash
pip install optuna
```

### Configuração do Optuna
| Parâmetro | Valor |
|---|---|
| Estratégia de busca | TPE Sampler (padrão) |
| Número de trials | 100 por ciclo |
| Pruning | MedianPruner (elimina runs ruins cedo) |
| Frequência | Semanal (todo domingo à meia-noite) |

### Espaço de busca
| Parâmetro | Mínimo | Máximo | Atual |
|---|---|---|---|
| RSI Oversold (Long) | 5 | 25 | 15 |
| RSI Overbought (Short) | 75 | 95 | 85 |
| Volume Multiplier | 1.0 | 2.0 | 1.3 |
| Funding Rate Limite | 0.0001 | 0.001 | 0.0005 |
| TP (x ATR) | 1.0 | 3.0 | 1.5 |
| SL (x ATR) | 0.5 | 2.0 | 1.0 |

### Entregável
> Primeiro ciclo de otimização automática executado com **melhora mensurável no win rate**

---

## Fase 3 — Machine Learning com XGBoost
**Período: Mês 2 em diante**

### Objetivo
Com centenas de trades registrados e parâmetros otimizados pelo Optuna, treinar um classificador XGBoost que aprende a prever se um sinal tem alta ou baixa probabilidade de ser lucrativo. O ML vira um filtro adicional antes da execução.

### Tarefas
- [ ] Implementar módulo `bot/ml_filter.py` com XGBoost
- [ ] Preparar dataset: cada linha = um sinal do SQLite com todos os indicadores + resultado (win/loss)
- [ ] Definir features de entrada (ver abaixo)
- [ ] Label: `1` se o trade foi lucrativo, `0` se foi perda
- [ ] Treinar com 80% dos dados, validar com 20% (split temporal — sem data leakage)
- [ ] Threshold de confiança: só executar trades com probabilidade > 60%
- [ ] Re-treinar automaticamente toda semana com novos dados
- [ ] Salvar modelo serializado em `bot/models/xgboost_filter.pkl`
- [ ] Exibir no dashboard: accuracy do modelo, feature importance, distribuição de confiança

### Features de entrada
```
rsi2, ema9, ema21, delta_ema (ema9 - ema21),
volume_ratio (volume / volume_avg), atr,
funding_rate, hora_do_dia, dia_da_semana
```

### Instalação
```bash
pip install xgboost scikit-learn joblib
```

### Requisitos mínimos de dados
> ⚠️ Não iniciar a Fase 3 sem atender todos os critérios abaixo

- Mínimo de **200 trades fechados** no SQLite (win + loss)
- Distribuição equilibrada: pelo menos **40% de cada classe** (win/loss)
- Diversidade de condições de mercado (não só bull ou só bear)
- Se não tiver dado suficiente, continuar na Fase 2 até acumular mais

### Entregável
> Modelo com **accuracy > 55%** no conjunto de validação e redução mensurável de trades perdedores

---

## Resumo do Cronograma

| Fase | Período | Foco Principal |
|---|---|---|
| Fase 1 | Semanas 1–2 | Bot estável, acumulando dados. Validação técnica completa. |
| Fase 2 | Semanas 3–4 | Optuna ativado. Thresholds se ajustam automaticamente toda semana. |
| Fase 3 | Mês 2+ | XGBoost filtrando sinais. Só entra nos trades com alta confiança. |

---

> *A prioridade nas primeiras semanas não é lucro — é consistência. Um robô que não perde dinheiro sistematicamente já está na frente de 90% dos traders.*
