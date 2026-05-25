# Lighter WebSocket Candle Feed — Design

**Data:** 2026-05-24
**Autor:** Davi + Claude
**Status:** Draft

## Problema

A leitura de candles no caminho Lighter chega ao bot com latência de **15-30s**. Hoje o fluxo é:

1. `BinanceCandleManager` (WebSocket Binance Spot) dispara evento de "candle 5m fechou"
2. `main.py::process_asset` chama `client.get_candles()` → `LighterExchangeClient.get_candles` → REST `/api/v1/candles`
3. A REST da Lighter tem lag de indexação após o close; o `_drop_open_candle` e o retry stale de 360s existem por causa disso

Consequência: estratégias avaliam o candle 15-30s depois do fechamento real, perdendo entradas com bom preço e divergindo do backtest em ativos voláteis.

Além da latência, o uso da Binance como trigger trouxe complicações que viraram dívida técnica **apenas no caminho Lighter**:
- Mapeamento `_BINANCE_SYMBOL_MAP` (XAU ↔ XAUTUSDT)
- Co-trigger para ativos sem par Binance Spot (WTI, HYPE, LIT) e auto-detect
- Reseed cross-exchange a cada heartbeat
- Dependência de um feed externo (Binance) para operar na Lighter

## Solução

Substituir, **apenas no caminho Lighter**, o `BinanceCandleManager` por um novo `LighterCandleManager` que consome o canal nativo `candle/{market_id}/{resolution}` do WebSocket oficial da Lighter (`wss://mainnet.zklighter.elliot.ai/stream`).

O canal empurra updates em batches de 500ms a cada trade. A detecção de "candle fechou" passa a vir da mudança do campo `t` (timestamp) entre updates, com timer de boundary como backup para ativos sem volume na janela.

A infraestrutura Binance permanece intacta — só não é mais instanciada quando `selected_exchange == "lighter"`. Quando a Hyperliquid voltar a ser usada, otimizamos os candles dela separadamente.

## Escopo

**Em escopo (caminho Lighter):**
- Novo `LighterCandleManager` (threading, mesmo padrão do `BinanceCandleManager`)
- Conexão WS única a `wss://mainnet.zklighter.elliot.ai/stream` com N subscribes (1 por par asset×TF ativo)
- Detecção de candle close via mudança de `t` + boundary timer de backup
- Watchdog + fallback automático para REST `get_candles` quando WS silencia
- Buffer local compartilhado com `LighterExchangeClient` (mesma estrutura `_candle_buffer` atual)
- Remoção do fallback Binance dentro de `LighterExchangeClient.get_candles` (em caso de erro, retry REST Lighter)
- Switch em `main.py::bot_loop` para instanciar o manager correto baseado em `selected_exchange`

**Fora de escopo:**
- Remoção do `BinanceCandleManager`, `fetch_binance_candles`, `_BINANCE_SYMBOL_MAP`, co-trigger, mapas reversos — tudo continua existindo para o `HyperliquidExchangeClient`
- Migração dos candles do `HyperliquidExchangeClient` para o feed nativo da HL (fica para quando voltarmos a usar HL)
- Mudanças em estratégias, scanner, dashboard, executor, risk
- Mudanças no backtest/csv_loader (já usa Lighter REST, não impactado)
- Mudança da fonte de timestamps em logs/DB

## Arquitetura

### `LighterCandleManager` (novo módulo: `bot/exchanges/lighter_ws.py`)

Responsabilidades equivalentes ao `BinanceCandleManager`, adaptadas para o protocolo Lighter:

**Threads:**
- `ws_thread`: mantém socket aberto, processa mensagens recebidas
- `worker_thread`: drena fila interna de eventos de candle close e dispatch via `ThreadPoolExecutor(max_workers=16)` para o `on_candle_close` (mesmo padrão atual)
- `watchdog_thread`: monitora silêncio prolongado por canal; força reconexão se >90s sem qualquer mensagem
- `boundary_thread`: dorme até o próximo boundary de cada TF; quando dispara, checa se algum ativo subscrito não recebeu update desde o último boundary daquele TF — se sim, força fetch REST e emite o evento

**Construtor:**
```python
LighterCandleManager(
    client: LighterExchangeClient,
    assets: list[str],
    intervals: list[str],           # ex: ["5m", "15m", "1h"]
    on_candle_close: Callable[[str, str], None],  # (asset, tf)
)
```

**Métodos públicos (assinatura igual ao Binance manager para drop-in):**
- `start()`, `stop()`, `pause()`, `resume()`
- `update_assets(new_assets: list[str])` — adiciona/remove subscribes incrementalmente
- `get_candles(asset: str, interval: str) -> pd.DataFrame` — lê do buffer local

**Subscribe management:**
- Para cada ativo: resolve `market_id` via `_get_lighter_market_id` (cache já existe em `csv_loader` e `lighter_exchange`)
- Envia `{"type":"subscribe","channel":"candle/<market_id>/<tf>"}` para cada `(asset, tf)`
- Mantém set `_subscriptions: dict[(asset, tf), market_id]` para reenviar em reconexão e para `update_assets`

**Recepção de mensagens:**
- Parse do `channel` (`candle:<market_id>:<tf>`) → resolve `(asset, tf)`
- `update/candle`: pega o último item de `candles[]`, atualiza buffer (`_candle_buffer[asset][tf]`)
  - Se `t` > último `t` armazenado: nova vela começou → a anterior fechou → emite evento `(asset, tf)`
  - Se `t` == último `t`: atualiza OHLCV da vela em formação no buffer (mas **não** emite evento)
- `subscribed/candle`: snapshot inicial — preenche buffer; não emite evento

**Buffer:**
- `_candle_buffer: dict[(asset, tf), pd.DataFrame]`, max 600 rows (mesma constante atual)
- Thread-safe via `_candle_buffer_lock`
- Cold start: ao subscribar, faz REST `client.get_candles(asset, tf, count=500)` para seedar (igual ao warm-up do Binance manager)
- Filtro `_drop_open_candle` aplicado antes de servir df pro `get_candles()` (defesa em profundidade — não confiamos que o estado do buffer está sempre limpo)

**Keepalive:**
- Envia frame de ping a cada 90s (limite Lighter é 2min)
- Reconnect com backoff exponencial 1s → 2 → 4 → ... → max 30s
- Após reconexão, reenvia todos os subscribes ativos e re-seeda buffers (mesma lógica de `_reseed_with_overlap` adaptada)

**Boundary timer (backup para volume baixo):**
- Para cada TF subscrito, calcula próximo boundary: `next = (now_ms // tf_ms + 1) * tf_ms`
- Dorme até `next + 2000ms` (margem de 2s para WS chegar primeiro)
- Ao acordar, para cada `(asset, tf)` daquele TF: se `last_update_ms[asset, tf] < next`, força `client.get_candles(asset, tf, count=1)`, atualiza buffer e emite evento se houve nova vela
- Recalcula próximo boundary e dorme novamente

**Watchdog (fallback REST):**
- Mantém `_last_msg_ms` global (qualquer mensagem do WS)
- Se >90s sem nada: força reconexão (mesma lógica atual)
- Mantém `_last_update_ms[asset, tf]` por canal
- Se um canal específico ficar silente >5min **dentro do horário esperado de atividade** (ou seja, ativo tem volume normalmente nessa janela), loga warning — não força nada, porque o boundary timer já cobre o caso de ausência de update

### Mudanças em `LighterExchangeClient`

- `get_candles(asset, interval, count)`: prioridade passa a ser **ler do buffer compartilhado** se o WS manager estiver ativo e o buffer estiver populado para `(asset, tf)`. Se buffer vazio ou WS inativo, cai para o caminho REST atual.
- O buffer fica vivendo no `LighterExchangeClient` (não no manager) — o manager apenas escreve nele. Assim REST e WS escrevem na mesma estrutura.
- Remover o try/except que cai para `fetch_binance_candles` em caso de erro REST. Em caso de erro, propaga exceção (ou retorna df vazio) — main.py já tem retry stale de 360s.

### Mudanças em `main.py::bot_loop`

```python
if selected_exchange == "lighter":
    candle_mgr = LighterCandleManager(
        client=client,
        assets=active_assets,
        intervals=required_tfs,
        on_candle_close=process_asset_callback,
    )
else:  # hyperliquid
    candle_mgr = BinanceCandleManager(
        assets=active_assets,
        intervals=required_tfs,
        on_candle_close=process_asset_callback,
    )
```

Resto do `bot_loop` (heartbeat, `update_assets`, TP/SL recovery) permanece igual — ambos managers expõem a mesma API.

### Mudanças em `LighterExchangeClient.connect()` / `disconnect()`

- `connect()`: instancia e inicia o `LighterCandleManager` se ainda não criado pelo `main.py`. **Decisão:** o manager continua sendo criado pelo `main.py` (igual hoje), `connect()` não muda.
- `disconnect()`: chama `candle_mgr.stop()` (já é a responsabilidade do main.py hoje).

## Fluxo de dados

```
WS Lighter → ws_thread → parse → buffer + emite evento
                                       ↓
                                 worker_thread → ThreadPoolExecutor
                                                        ↓
                                                  process_asset(asset)
                                                        ↓
                                                  client.get_candles() [lê buffer]
                                                        ↓
                                                  evaluate_all(strategies)
```

Em caso de silêncio de canal (ativo sem trade na janela):

```
boundary_thread → acorda em next_5m_boundary + 2s
                       ↓
                  check last_update por canal
                       ↓
                  silent? → client.get_candles(asset, tf, count=1) via REST
                       ↓
                  update buffer + emite evento
```

Em caso de WS caído:

```
ws_thread morre → watchdog detecta >90s silêncio → reconnect (backoff)
                                                        ↓
                                              reenvia subscribes + reseeda buffers
                                                        ↓
                                              boundary_thread continua emitindo eventos
                                              (REST fallback cobre a janela de gap)
```

## Tratamento de erros

| Cenário | Comportamento |
|---|---|
| WS desconecta | Watchdog reconecta com backoff exponencial; boundary timer cobre eventos perdidos via REST |
| Subscribe rejeitado pela Lighter (market_id inválido) | Log error + remove o par do `_subscriptions`; ativo é tratado como "sem feed" e `client.get_candles` cai pro REST direto |
| REST fallback também falha | Mesmo comportamento atual (`process_asset` recebe df stale ou vazio, retry stale de 360s em main.py já existe) |
| Mensagem malformada | Log warning, ignora; não derruba o ws_thread |
| Buffer overflow (>600 rows) | FIFO: descarta as mais antigas (mesma lógica atual) |
| Cold start sem buffer ainda populado | `process_asset` chama REST direto (caminho atual continua funcionando) |
| Reconnect loop infinito (Lighter fora do ar) | Watchdog para de tentar após N tentativas e marca manager como "degradado"; `process_asset` cai 100% no REST até manager recuperar |

## Testes

**Unit:**
- Parser de `update/candle`: vela em formação (`t` igual) vs nova vela (`t` mudou)
- Boundary calculator: dado `now_ms` e `tf`, retorna `next_boundary_ms` correto para todos os TFs suportados
- Buffer update + `_drop_open_candle` no read path
- `update_assets` adiciona/remove subscribes corretamente

**Integration (sem rede, com mock WS):**
- Sequência: subscribe → snapshot inicial → 3 updates da mesma vela → 1 update com `t` novo → verifica que `on_candle_close` foi chamado exatamente 1 vez
- Reconexão: kill socket → watchdog reconecta → verifica que subscribes foram reenviados
- Boundary timer: sem updates por 5min → boundary timer dispara e força REST

**Manual (testnet ou paper):**
- Rodar paralelo a `BinanceCandleManager` por 24h com log comparativo de "latência WS Lighter vs trigger Binance"
- Validar que estratégias disparam mesmos sinais (mesmo close, mesmo timestamp) em ativos com volume alto (BTC, ETH, SOL)
- Validar boundary timer em ativo de baixo volume (ex: LIT) — forçar uma janela sem trade e confirmar que evento dispara mesmo assim

## Migração / rollout

1. Implementar `LighterCandleManager` sem mexer no `main.py` (módulo novo, não usado)
2. Testes unitários passando
3. Switch em `main.py` controlado por config flag temporária `use_lighter_ws_candles` (default `false`)
4. Ativar em paper/testnet, monitorar 24h
5. Ativar em mainnet com 1 ativo de alto volume
6. Expandir para todos os ativos
7. Remover a config flag após 1 semana estável (passa a ser comportamento padrão)

## Riscos & mitigação

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| WS Lighter instável (drops frequentes) | Média | Médio | Watchdog + boundary timer + fallback REST garantem continuidade; pior caso = mesma latência de hoje |
| Subscribe acumula travamento no socket (limite desconhecido) | Baixa | Alto | Se ocorrer, split em N sockets (1 por grupo de ~5 ativos) — já planejado como contingência |
| Race entre WS update e boundary timer (evento duplicado) | Média | Baixo | Dedup por `last_emitted_t[asset, tf]` — só emite se `t > last_emitted_t` |
| Lighter mudar formato da mensagem (`channel` separator `:` vs `/`) | Baixa | Alto | Parser tolerante a ambos; teste de integração com payload real captura |
| Ativo recém-adicionado via dashboard não recebe subscribe | Média | Médio | `update_assets()` no heartbeat (30s) já cobre — mesmo padrão do Binance manager |

## Não-objetivos

- Não vamos otimizar a latência da REST Lighter (continua como fallback, latência aceitável quando WS está fora)
- Não vamos mudar a fonte de candles do backtest (continua REST, sem necessidade de tempo real)
- Não vamos persistir os candles do WS no CSV (`csv_loader._update_csv` continua sendo o único caminho de escrita no disco)
- Não vamos suportar WS para Hyperliquid neste spec

## Arquivos afetados

- **Novo:** `bot/exchanges/lighter_ws.py` (LighterCandleManager)
- **Modificado:** `main.py` (switch de manager por exchange)
- **Modificado:** `bot/exchanges/lighter_exchange.py` (remover fallback Binance no `get_candles`; ler do buffer compartilhado se WS ativo)
- **Inalterado:** `bot/exchanges/binance_ws.py`, `bot/exchanges/base.py` (`fetch_binance_candles`, `_BINANCE_SYMBOL_MAP`), `bot/exchanges/hyperliquid.py`, todo o caminho HL
- **Testes:** `tests/test_lighter_candle_manager.py` (novo)

## Métrica de sucesso

- Latência mediana entre fechamento real do candle 5m e disparo do `on_candle_close` < 2s (vs 15-30s atual)
- Zero divergências de sinal entre live e backtest em runs de 7 dias em ativos de alto volume
- Taxa de fallback REST (boundary timer disparando porque WS silenciou) < 5% das janelas
