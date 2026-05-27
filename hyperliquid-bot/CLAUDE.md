# RazorHL — Hyperliquid Scalping Bot

## Visao Geral

Bot autonomo de scalping para perpetuals na Hyperliquid, com dashboard web completo para monitoramento, analise e configuracao. O sistema roda continuamente, monitora ativos via REST/WebSocket, calcula indicadores tecnicos, gera sinais de entrada, executa ordens e gerencia risco automaticamente.

**Stack:** Python 3.10+ | Flask + SocketIO | SQLite | Chart.js | hyperliquid-python-sdk

---

## REGRA CRÍTICA — PnL e fills devem vir da API

**Nunca estime, suponha ou calcule PnL/entry/exit a partir de mid price, signal price ou qualquer valor que não seja um fill confirmado pela exchange.** Todo dado que vai para a tabela `trades` (entry_price, exit_price, size, fees, pnl, funding) tem que ser puxado diretamente da API de fills da exchange (`get_recent_fills`, `user_fills_by_time` etc.) usando o `oid`/`txHash`/`tradeId` da ordem.

Implicações:
- `market_open`/`market_close` só podem retornar `filled` depois de confirmar que a posição mudou na exchange (via `get_open_positions`) E que existe fill real associado.
- Se a ordem for IOC e não casar (cancelada por slippage/liquidez), retornar `{"statuses":[{"error":"unfilled"}]}` para o executor abortar — NÃO inserir trade no banco com valores estimados.
- `avgPx` no retorno tem que ser a média ponderada dos fills reais (somar `sz * px` / somar `sz`), nunca o mid usado para montar o limite IOC.
- Se o oid da abertura não casar com o tradeId dos fills (caso Lighter txHash), usar fallback por janela temporal (`get_recent_fills(asset, since_ms)`) mas ainda assim ler valores reais — nunca preencher com o que foi pedido.
- Vale o mesmo para fechamento: `closedPnl`, `exit_price` e `fees` vêm dos fills do close, nunca de cálculo derivado de mid/preço de trigger.

Trades fantasma já aconteceram porque a `market_open` da Lighter retornava `filled` sintético independente do resultado on-chain — ordens IOC canceladas viravam trades no banco com `entry_price = mid estimado` e PnL inventado.

---

## Arquitetura

```
run.py                  <- Entry point (dashboard + bot); após criar o app Flask, verifica se `bot_status` era `"running"` ou `"paused"` no DB e chama `start_bot()` automaticamente — garante que o bot retoma ao reiniciar via pm2 sem intervenção manual
main.py                 <- Bot loop orchestration — event-driven via candle manager WebSocket; `bot_loop()` chama `get_required_timeframes()` antes de instanciar o candle manager; **switch de manager**: lê `selected_exchange` (default "lighter") e `use_lighter_ws_candles` (default "true") do cfg — se `selected_exchange=="lighter"` AND `use_lighter_ws_candles=="true"`, instancia `LighterCandleManager(client=client, assets=..., intervals=..., on_candle_close=...)`; caso contrário instancia `BinanceCandleManager(initial_assets, on_candle_close=..., intervals=...)`; `candle_mgr` é global `BinanceCandleManager | LighterCandleManager | None`; só subscreve TFs que estratégias habilitadas precisam — incluindo 15m/30m/1h quando alguma instância tem `params['timeframe']` setado; heartbeat de 30s (verifica status, assets, TP/SL); `on_candle_close(asset, interval)` **só roda quando `interval=="5m"`** (early return em qualquer outro TF — sem esse gate, o `LighterCandleManager` que emite close para CADA tf subscrito faria `process_asset` rodar 2-3× em boundaries que coincidem com TFs maiores, ex HH:30 = 5m + 30m, logando "New 5m candle closed" duplicado por ativo; como 15m/30m/1h/4h/1d são múltiplos de 5m, basta o 5m close pra disparar e o `_detect_new` interno detecta os TFs maiores). `process_asset(asset, cfg, last_15m_ts, last_30m_ts, last_1h_ts, last_4h_ts, last_1d_ts)` é chamado pelo worker thread a cada 5m close — busca TODOS os TFs (5m, 15m, 30m, 1h, 4h, 1d) via `client.get_candles()` se o TF estiver em `candle_mgr.intervals`; `candle_mgr` é usado APENAS como trigger de candle close (evento 5m alinha boundaries de TFs maiores), não como fonte de dados; `new_5m=True` sempre; helper `_detect_new(tf_label, tf_key, df, store)` compara último timestamp do df com store por asset — detecta `new_15m`, `new_30m`, `new_1h`, `new_4h`, `new_1d`; **store é persistido em SQLite** via `db.get_last_candle_ts(tf)` (carrega na boot) e `db.set_last_candle_ts(tf, asset, ts)` (escreve em cada detecção positiva) — chaves no `config` table com prefix `last_ts.<tf>.<asset>`. Sem isso, cada restart zerava o dict em memória e a primeira chamada de `process_asset(asset)` pós-restart fazia `latest_ts > 0` ser sempre True, disparando falso `new_<tf>` no meio da hora/dia. Caso real: VVV 22:40 — bot reiniciou às 22:30, primeiro 5m boundary do VVV foi 22:40, `last_1h_ts={}` → falso positivo → `stoch_scalp_vvv_1h` disparou trade fora do boundary 1h real; todos propagados para `evaluate_all` junto com `df_5m, df_15m, df_30m, df_1h, df_4h, df_1d`; `check_bb_mid_exit(asset, df_5m)` a cada 5m close; retry de candle stale: após buscar df_5m, se o candle mais recente tiver mais de 360s (6min), dorme 5s e retenta uma vez — protege co-trigger assets (WTI, HYPE) que podem ter lag no Lighter REST
bot/
  db.py                 <- SQLite (trades, config, logs, signals); helpers `get_last_candle_ts(tf) -> dict[asset, ts_ms]` e `set_last_candle_ts(tf, asset, ts)` para persistir o último candle close detectado por TF/asset (usados em `main.py` para sobreviver a restart sem disparar falso `new_<tf>`). Armazena no `config` table com chave `last_ts.<tf>.<asset>`. Também: `get_lighter_coi_counter()` / `set_lighter_coi_counter(n)` persistem o contador de `client_order_index` da Lighter — sem isso o counter zera em cada restart, COIs viram ambíguos no histórico onchain e o lookup de cancel reason em `/accountInactiveOrders` pode retornar status de uma tx antiga com mesmo coi (caso real: NEAR 2026-05-26 15:40, coi=9 reusado, lookup pegou reason de outra tx). Chave: `lighter.client_order_counter`
  logger.py             <- Logging system (console + file + SQLite)
  exchanges/
    base.py             <- BaseExchangeClient ABC + `fetch_binance_candles(asset, interval, count)` — função compartilhada que busca candles da Binance Spot REST (pública, sem auth); usada por ambos os clientes; `_BINANCE_SYMBOL_MAP = {"XAU": "XAUTUSDT"}` — assets cujo símbolo Binance difere do padrão {asset}USDT
    binance_ws.py       <- BinanceCandleManager para WebSocket de candles; construtor aceita `intervals` param (default `["5m"]`) — só semeia e subscreve os TFs passados; `_DEFAULT_SEED_COUNTS` define contagens por TF (5m=500, outros=300); property `intervals` expõe a lista ativa; 3 threads (ws_thread, worker_thread, watchdog_thread) + `ThreadPoolExecutor(max_workers=16)` para dispatch paralelo; `_worker_loop` drena toda a fila e submete cada ativo ao executor em paralelo — elimina processamento sequencial e reduz latência entre trigger e avaliação; `_safe_callback` envolve `on_candle_close` com tratamento de exceção; métodos: start/stop/pause/resume/get_candles/update_assets; `_reseed_with_overlap` busca 50 candles e merge com dedup (pula co-trigger assets); `_reconnect()` re-seed + fecha + reabre WS; `_watchdog_loop()` monitora silêncio >90s; `_BINANCE_SYMBOL_MAP = {"XAU": "XAUT"}` — mapeamento de nome interno → base Binance (XAU usa XAUTUSDT); `_BINANCE_REVERSE_MAP` — reverso, usado em `_parse_kline_event` para converter XAUT→XAU; `_COTRIGGER_ASSETS = {"WTI","HYPE","LIT"}` (set mutável seedado com hardcoded; **auto-cresce** quando `_try_seed_asset` falha em todos os intervalos para um ativo — significa que o ativo não tem equivalente na Binance Spot, é promovido a co-trigger via `_mark_as_cotrigger(asset)` com log warning); sem equivalente na Binance Spot: excluídos do seed/WS; disparados uma vez por boundary 5m em `_on_message` quando qualquer ativo Binance fecha candle (co-piggyback no BTC/ETH/SOL); `_cotrigger_lock` + `_last_cotrigger_boundary` garantem disparo único por boundary; `_try_seed_asset(asset)` → bool: tenta seed REST 3x por intervalo, marca como co-trigger se 5m falhar; `_seed_buffer` (startup) e `update_assets` (heartbeat) usam o mesmo helper — `update_assets` reprocessa qualquer asset em `self._assets` que ainda não tenha buffer (recovery automático para ativos baixados pela aba Ativos que não existem na Binance — basta restart do bot ou próximo heartbeat após adicionar)
    factory.py          <- create_exchange_client() → instancia HyperliquidClient ou LighterExchangeClient conforme selected_exchange no config
    hyperliquid.py      <- Cliente ativo da Hyperliquid (REST + WS); candles via Binance (get_candles delega para fetch_binance_candles); correções de fills/funding/ordens devem ser feitas aqui
    lighter.py          <- LighterExchangeClient
    lighter_client.py   <- Cliente REST da Lighter; `get_active_orders(account_index, auth, market_id=None)` → lista ordens abertas via `/api/v1/accountActiveOrders` (inclui triggers TP/SL com campo `type`: 'take-profit'/'stop-loss'); `get_top_of_book(market_id, auth, levels=5)` → `{bids: [(px, sz), ...], asks: [...], bid_sum, ask_sum}` para diagnóstico de profundidade (tenta `remaining_base_amount`, `base_amount`, `size`, `amount`, `remaining_amount` como nome do campo de size); `get_inactive_orders(account_index, auth, market_id=None, limit=10)` → lista de ordens canceladas/filled via `/api/v1/accountInactiveOrders` com campo `status` exato do matching engine. **Lista completa de status (do SDK Python oficial elliottech/lighter-python)**: `in-progress, pending, open, filled, canceled, canceled-post-only, canceled-reduce-only, canceled-position-not-allowed, canceled-margin-not-allowed, canceled-too-much-slippage, canceled-not-enough-liquidity, canceled-self-trade, canceled-expired, canceled-oco, canceled-child, canceled-liquidation, canceled-invalid-balance`
    lighter_exchange.py <- Abstração de ordens Lighter; `get_candles` usa buffer incremental: cold start busca `count` candles completos; warm updates buscam apenas `_CANDLE_WARM_FETCH=3` candles e fazem merge no buffer local (`_CANDLE_BUFFER_MAX=600` rows por (asset, interval)); buffer zerado no `disconnect()`; thread-safe via `_candle_buffer_lock`; fallback para Binance em caso de erro; **`_drop_open_candle(df, interval, now_ms=None)`** descarta a vela ainda em formação antes de mergear no buffer — Lighter REST devolve a vela aberta como última linha com `c`=mark atual, e estratégias fazem `df.close.iloc[-1]` (sem o filtro acabavam avaliando vela parcial, causando divergência clássica live↔backtest: ex. HYPE 5m close=62.4694 no backtest mas live via 62.53 = mark 11s dentro da vela seguinte). Filtro: `ts < (now_ms // interval_ms) * interval_ms`. `_INTERVAL_MS` cobre 1m/5m/15m/30m/1h/4h/12h/1d; intervalos desconhecidos passam direto sem filtro. Mesmo filtro replicado em `csv_loader._fetch_lighter_candles_since` e `csv_loader.download_full_history` (sobre o df final, antes do to_csv) para o CSV nunca persistir vela aberta; get_recent_fills compara timestamps em ms (Lighter retorna ms, não segundos — não dividir since_ms por 1000); LIGHTER_TAKER_FEE_RATE=0.0 (Lighter tem taxa zero — fee sempre 0.0 nos fills); `place_tp_sl` usa `ORDER_TYPE_TAKE_PROFIT_LIMIT` (5) e `ORDER_TYPE_STOP_LOSS_LIMIT` (3) com `TIF_GOOD_TILL_TIME` + `order_expiry=-1` (28 dias); as variantes market (TAKE_PROFIT=4, STOP_LOSS=2) só aceitam `TIF_IOC` e são canceladas imediatamente após envio (erro "OrderTimeInForce is not valid" se forçado GTT); o `price` é o limite worst-case com slippage aplicado ao `trigger_price`; `get_open_trigger_order_types(asset)` → set{'tp','sl'} com ordens ativas via `get_active_orders` — retorna {'tp','sl'} em caso de erro para evitar re-placement espúrio; **`list_active_trigger_orders(asset)`** → lista de dicts `[{order_index, type, trigger_price, is_ask, base_amount}]` (necessário para cancelar uma ordem específica — `cancel_order` da Lighter exige `order_index`, não `client_order_index`); **`cancel_order(asset, order_index)`** → bool: envia tx de cancel via SignerClient.cancel_order com nonce desync recovery; **`cleanup_orphan_triggers(asset)`** → int: cancela TP/SL órfãs no asset (triggers reduce-only ativas SEM posição correspondente). Acúmulo dessas órfãs causa `canceled-reduce-only` em novos `market_open` porque a Lighter NÃO tem OCO nativo: quando uma trigger executa, a outra leg da OCO sintética fica pendurada indefinidamente. Se há posição ativa no asset, é no-op (deixa o recovery cuidar). Chamado em 3 lugares: (a) `executor.open_position` antes de cada `market_open` (pre-flight); (b) `risk.check_open_positions_tp_sl` logo após registrar close de trade (limpa a leg que sobrou); (c) script utilitário `cleanup_orphan_triggers.py` (varredura manual). Override da base: HL não precisa (OCO nativo), retorna 0; **`market_open` confirma fill real**: lê `get_best_prices` (best_bid/best_ask) — IOC usa `best_ask*(1+slip)` para BUY e `best_bid*(1-slip)` para SELL (slippage é tolerância acima do best, NÃO acima do mid; usar mid quebrava em ativos com spread > slip → IOC nunca cruzava nada e a tx era cancelada onchain sem aparecer no order history); snapshot de `get_open_positions` antes do envio, `_place_order` IOC, dorme `FILL_WAIT_SEC` (2.5s) e em seguida faz polling até `FILL_POLL_TIMEOUT_SEC` (6.0s extras, intervalo `FILL_POLL_INTERVAL_SEC`=1.0s) checando `get_open_positions` — sai cedo no primeiro snapshot que mostra posição na direção esperada com tamanho > before_size (Lighter é zk-rollup com batches; tx pode demorar mais que 2.5s para ser indexada, gerando falsos `unfilled` que descartavam fills reais); se nenhum poll confirmar, retorna `{"statuses":[{"error":"unfilled"}]}` (executor aborta); loga `bid/ask/spread%/limit/slip` + diagnóstico de profundidade (`need=<size>`, `reachable=<soma do size dentro do limit>`, top 5 níveis do lado que vai casar, flag `⚠ THIN` se reachable < need) antes de enviar e número de polls após confirmar; **retry escalonado de slippage** via `SLIP_RETRY_MULTIPLIERS = (1.0, 2.5, 5.0)` — cada tentativa releva o book, recalcula limit com `slippage × mult`, envia IOC, faz polling completo (FILL_WAIT_SEC + FILL_POLL_TIMEOUT_SEC). Se cancelar, sobe pro próximo multiplicador. Sobrevive a race conditions na batch da zk-rollup onde múltiplos bots agressivos no mesmo lado varrem o book entre snapshot e inclusão da tx (típico em rompimentos de BB onde vários sinais disparam ao mesmo tempo). Cada log usa prefixo `tryN/M` para identificar a tentativa. Erros de assinatura/SDK (não-fill) abortam imediatamente — slippage maior não resolve. Após cada tentativa cancelada, busca a ordem em `/accountInactiveOrders` correlacionando por **`client_order_index`** (o schema Order da Lighter NÃO tem `tx_hash` — campos disponíveis são `order_index`, `client_order_index`, `nonce` etc.; o bot captura o COI usado em `_place_order` e devolve no dict junto com `txHash`). O lookup faz polling curto (até ~6s, intervalo 1.5s) porque o endpoint indexa com lag — sem o polling, todas as 6 tentativas voltavam `unknown (tx not in inactive orders yet)`. Loga o `status` real do matching engine no formato `Lighter reason: <status>`. O log da tentativa também inclui `coi=<index>` para correlacionar manualmente se necessário. **`_client_order_counter` é persistido em SQLite** via `db.get_lighter_coi_counter()` (boot) e `db.set_lighter_coi_counter(n)` (a cada `_next_client_order_index`, sob lock `_client_order_counter_lock`) — sem persistência, cada restart zerava o counter e os COIs eram reusados entre sessões; o lookup por `client_order_index` em `/accountInactiveOrders` retornava o status de uma tx ANTIGA com mesmo coi, dando reason errado no log (caso real: NEAR 2026-05-26 15:40 coi=9 reusado após restart, `Lighter reason: canceled-reduce-only` veio de outra tx, motivo real do fail ficou oculto). **Hard cancels** (`LIGHTER_HARD_CANCEL_STATUSES`: margin-not-allowed, position-not-allowed, invalid-balance, self-trade, post-only, reduce-only) abortam o retry imediatamente — slippage maior não resolve restrições de risco. **Soft cancels** (too-much-slippage, not-enough-liquidity, unknown) seguem para próxima tentativa. Após esgotar todos os multiplicadores, loga `exhausted N slippage retries (up to X)` e retorna `{"error":"unfilled"}`; `avgPx` retornado é média ponderada de `get_recent_fills` (helper `_weighted_avg_fill_px(asset, since_ms, is_buy, expected_sz)` filtra por side B/A), com fallback para `position.avgEntryPrice` se a API de fills estiver atrasada; `totalSz` é a diferença real de posição (antes vs depois), não o tamanho pedido; **`market_close` usa avgPx real**: mesmo helper aplicado aos fills do close (side oposto), com fallback para mid apenas se indexação estiver atrasada (PnL fallback recupera valor real); limite IOC do close também usa `best_bid`/`best_ask` (SELL → best_bid*(1-slip); BUY → best_ask*(1+slip)) pela mesma razão do `market_open`
  indicators.py         <- Technical indicators (pandas-ta) + is_fee_viable()
  signals.py            <- DEPRECATED — delega para strategies.manager
  executor.py           <- Order execution (market, TP/SL); `open_position` é serializado por asset via `_open_locks: dict[str, threading.Lock]` + helper `_get_asset_lock(asset)` — evita duplicação de trades quando dois workers (WS push + boundary fallback, ou race de dedup na fila) disparam o mesmo close 5m em paralelo; usa `lock.acquire(blocking=False)` para abortar imediatamente se outro worker já está executando ordem para o mesmo asset; dentro do lock re-verifica `db.get_open_trades()` para abortar se outro worker acabou de inserir trade para o asset; `finally: lock.release()` garante liberação no caminho de erro
  risk.py               <- Risk management
  strategies/
    base.py             <- BaseStrategy ABC; `REQUIRED_TIMEFRAMES: list[str] = ["5m"]` é fallback estático — em produção `manager.get_required_timeframes()` lê `params['timeframe']` de cada instância enabled; inclui `_insert_fee_block_signal` helper; assinatura `evaluate(..., df_1m=None, df_5m=None, df_15m=None, df_30m=None, df_1h=None, df_4h=None, df_1d=None, **kwargs)`. **Multi-TF**: exporta `SUPPORTED_TFS=("5m","15m","30m","1h")` e helper `select_tf_df(p, kwargs, *, name=None, asset=None, df_5m=, df_15m=, df_30m=, df_1h=)` → `(tf, df|None)`. Cada estratégia chama `select_tf_df(p, kwargs, name=self.NAME, asset=asset, df_5m=df_5m, ...)` no topo do evaluate: lê `params['timeframe']`, confere o trigger `new_{tf}` em kwargs, e retorna o df correspondente; se ainda não é o close do TF (ou df ausente), retorna None e o evaluate sai cedo. `name`/`asset` são placeholders aceitos para logging futuro (atualmente não usados). Cada estratégia depois usa a variável local `df` (renomeada de `df_5m`) em todo o corpo. DEFAULT_PARAMS de cada estratégia agora inclui `"timeframe": "5m"`
    manager.py          <- Orquestra estratégias; `evaluate_all(...)` aceita `df_5m`, `df_15m`, `df_30m`, `df_1h`, `df_4h`, `df_1d` + flags `new_5m`, `new_15m`, `new_30m`, `new_1h`, `new_4h`, `new_1d` e propaga todos para cada `strategy.evaluate(...)` (filtra por strategy_assets; merge asset_overrides[asset] em params antes de chamar evaluate); `get_required_timeframes()` → union do `params['timeframe']` de cada instância enabled (fallback para `REQUIRED_TIMEFRAMES` da classe); sempre inclui "5m"; `get_active_assets(global_assets)` → union dos assets das estratégias habilitadas; REGISTERED_STRATEGIES = [bb_reversion_btc_5m/eth_5m/sol_5m, bb_stoch_btc_5m/eth_5m/sol_5m/zec_5m/ton_5m, stoch_scalp_xau_5m/wti_5m/ton_5m, ema_cross_hype_5m/lit_5m, rsi_scalp_btc_5m/eth_5m/sol_5m/ton_5m, bb_rsi_btc_5m/eth_5m/sol_5m/zec_5m/ton_5m, macd_cross_btc_5m/eth_5m/sol_5m, williams_r_xau_5m/wti_5m/ton_5m] (todas hardcoded com `_5m` no nome — após migration M6 que renomeou instâncias legadas); STRATEGY_MAP = {s.NAME: s for s in REGISTERED_STRATEGIES}; **Instâncias dinâmicas multi-TF**: `register_dynamic_instance(scanner_strategy, asset, tag=None, timeframe="5m", _legacy_no_tf_in_name=False)` cria instância e adiciona em REGISTERED_STRATEGIES + STRATEGY_MAP. Nome novo: `{prefix}_{asset}_{tf}[_{tag_slug}]` (ex: `bb_stoch_btc_15m`, `bb_stoch_wti_5m_57_36_5_1`). Nome legado (com `_legacy_no_tf_in_name=True`): `{prefix}_{asset}[_{tag_slug}]` — só usado pelo loader para preservar entradas DB pré-multi-TF. `extra_defaults` da instância inclui `assets=[asset]` E `timeframe=tf`. DISPLAY_NAME inclui o TF (ex: `BB Stoch BTC (15m)`). `_SUPPORTED_TFS={"5m","15m","30m","1h"}`. `_slug()` normaliza tag (lowercase, [a-z0-9_], max 24). `_load_dynamic_strategies()` parser: detecta `{prefix}_{asset}_{tf}[_{tag}]` quando parts[1] está em `_SUPPORTED_TFS`; senão trata como legado (`{prefix}_{asset}[_{tag}]` com tf=5m, `_legacy_no_tf_in_name=True`)
    live_filters.py     <- **Filtros ADX/Sessão/ATR aplicados na live** (mesma semântica do scanner v2). 3 funções puras: `passes_session(params, ts_ms_curr) -> bool` (filtro UTC hour-based, (0,24)=no-op), `passes_adx(params, df, is_trend_strategy) -> bool` (mean-reversion bloqueia quando ADX>=min, trend bloqueia quando ADX<min, period=0=no-op, NaN warmup deixa passar), `apply_atr_tp_sl(params, df, signal) -> dict` (quando `atr_tp_mode=True`, sobrescreve `tp_pct = atr*atr_tp_mult/close` e `sl_pct = atr*atr_sl_mult/close`). Wrapper `apply_live_filters(p, df, signal, is_trend_strategy=False)` aplica os 3 na ordem e retorna signal modificado ou None se algum filtro bloqueou. Cada uma das 8 estratégias chama `return apply_live_filters(p, df, {...}, is_trend_strategy=...)` no lugar de `return {...}` em LONG e SHORT. **EMA_Cross e MACD_Cross usam `is_trend_strategy=True`**; as outras 6 famílias passam `False`. ts_ms extraído de `df["timestamp"]` (Lighter REST) ou `df["ts_ms"]` (fallback). Tests em `tests/strategies/test_live_filters.py` (12 testes). **DEFAULT_PARAMS de TODAS as 8 estratégias** agora inclui 7 campos novos com defaults off: `adx_period=0`, `adx_min=0`, `session_start=0`, `session_end=24`, `atr_tp_mode=False`, `atr_tp_mult=1.0`, `atr_sl_mult=1.0`, `atr_period=14`. **`scanner._translate_params` propaga os 7 campos** via dict comum `_live_filters` spread em cada return — combos aprovados pelo scanner v2 com filtros ON executam o que foi backtested na live.
    bb_reversion.py     <- BB Reversion (5m) — REQUIRED_TIMEFRAMES=["5m"]; mean reversion via retorno à BB; 3 presets; TP/SL em %; BB mid exit; gatilho new_5m=True; DEFAULT_PARAMS inclui `assets: []`, `asset_overrides: {}`, `bbp_long_threshold: 0.05`, `bbp_short_threshold: 0.95`; `__init__(name, display_name, extra_defaults)` permite criar instâncias nomeadas com parâmetros padrão distintos; 3 instâncias pré-configuradas no manager: bb_reversion_btc (BTC/EMA50/bbp<0.10/TP2%/SL0.8%), bb_reversion_eth (ETH/EMA50/bbp<0.15/TP1%/SL1%), bb_reversion_sol (SOL/EMA200/semRSI/bbp<0.05/TP2%/SL0.5%)
    bb_stoch.py         <- BB + Stoch (5m) — REQUIRED_TIMEFRAMES=["5m"]; mean reversion + Stochastic; BB mid exit; gatilho new_5m=True; `__init__(name, display_name, extra_defaults)` igual ao BBReversionStrategy; `_resolve_params(asset, params)` = `{**self.DEFAULT_PARAMS, **params}`; filtro EMA opcional via `ema_period` (0=desabilitado); **lógica de sinal replica o scan**: BBP_curr < threshold AND %K < stoch_long AND %D < stoch_long (sem crossover, condição simultânea no candle atual); smooth_k=3 (equivalente ao talib slowk_period=3 do scan); 5 instâncias em manager.py: bb_stoch_btc, bb_stoch_eth, bb_stoch_sol, bb_stoch_zec (ZEC-USD, BB10/2.0, bbp<0.05, stoch30/70, TP0.8%/SL0.8%, semEMA), bb_stoch_ton (TON-USD, BB15/1.5, bbp<0.10, stoch25/75, TP0.8%/SL0.8%, semEMA)
    stoch_scalp.py      <- Stoch Scalp (5m) — REQUIRED_TIMEFRAMES=["5m"]; entrada quando %K cruza %D enquanto ambos estão na zona extrema (crossover); Long: prev_K < os AND prev_D < os AND curr_K > curr_D AND prev_K <= prev_D; Short: prev_K > ob AND prev_D > ob AND curr_K < curr_D AND prev_K >= prev_D; smooth_k=3 (equivalente ao talib slowk_period=3); stoch_ob = 100 - stoch_os (simétrico); filtro EMA opcional; sem BB mid exit; DEFAULT_PARAMS: stoch_k=9, stoch_d=3, stoch_os=40, tp_pct=0.5, sl_pct=0.8, ema_period=50; 3 instâncias: stoch_scalp_xau (XAU-USD, k=9, os=40, EMA50, TP0.5%/SL0.8%), stoch_scalp_wti (WTI-USD, k=5, os=30, EMA50, TP1.0%/SL1.0%), stoch_scalp_ton (TON-USD, k=5, os=30, EMA200, TP0.5%/SL1.0%)
    ema_cross.py        <- EMA Cross (5m) — REQUIRED_TIMEFRAMES=["5m"]; entrada no crossover EMA rápida/lenta; filtro de tendência EMA opcional (ema_trend); dois modos de SL: fixo (sl_pct) ou baseado em ATR (use_atr_sl=True → sl_pct = atr*atr_mult/close); quando use_atr_sl, retorna também `atr_sl_dist` (float) no signal; sem BB mid exit; DEFAULT_PARAMS: ema_fast=9, ema_slow=21, ema_trend=0, tp_pct=1.5, sl_pct=0.5, use_atr_sl=False, atr_period=14, atr_mult=1.0; 2 instâncias: ema_cross_hype (HYPE-USD, EMA9/21, trend=EMA200, ATR SL×1.0, TP1.5%), ema_cross_lit (LIT-USD, EMA9/21, trend=EMA50, sl_pct=0.5, TP0.5%)
    rsi_scalp.py        <- RSI Scalp (5m) — entrada no crossover do RSI saindo da zona extrema; Long: prev_rsi < rsi_os AND curr_rsi >= rsi_os; Short: prev_rsi > rsi_ob AND curr_rsi <= rsi_ob (rsi_ob = 100 - rsi_os); filtro EMA opcional (ema_period); sem BB mid exit; DEFAULT_PARAMS: rsi_period=14, rsi_os=30, tp_pct=0.8, sl_pct=0.8, ema_period=0; 4 instâncias: rsi_scalp_btc, rsi_scalp_eth, rsi_scalp_sol, rsi_scalp_ton
    bb_rsi.py           <- BB RSI (5m) — BBP na zona extrema E RSI na zona extrema simultaneamente; Long: bbp_curr < bbp_long_threshold AND rsi_curr < rsi_os; Short: bbp_curr > bbp_short_threshold AND rsi_curr > rsi_ob (rsi_ob = 100 - rsi_os); suporta bb_mid_exit; filtro EMA opcional; DEFAULT_PARAMS: bb_period=15, bb_std=1.5, bbp_long_threshold=0.10, bbp_short_threshold=0.90, rsi_period=14, rsi_os=30, tp_pct=0.8, sl_pct=0.8, bb_mid_exit=False, ema_period=0; 5 instâncias: bb_rsi_btc, bb_rsi_eth, bb_rsi_sol, bb_rsi_zec, bb_rsi_ton
    macd_cross.py       <- MACD Cross (5m) — entrada no crossover da linha MACD com a linha de sinal; Long: curr_macd > curr_sig AND prev_macd <= prev_sig; Short: curr_macd < curr_sig AND prev_macd >= prev_sig; filtro EMA de tendência opcional (ema_trend); colunas pandas-ta: MACD_ (linha), MACDs_ (signal), MACDh_ (histograma); DEFAULT_PARAMS: macd_fast=12, macd_slow=26, macd_signal=9, tp_pct=1.0, sl_pct=0.5, ema_trend=0; 3 instâncias: macd_cross_btc, macd_cross_eth, macd_cross_sol
    williams_r.py       <- Williams %R (5m) — entrada no crossover do %R saindo da zona extrema; escala -100 (oversold) a 0 (overbought); wr_ob = wr_os + 100 (e.g. os=-80 → ob=-20); Long: prev_wr < wr_os AND curr_wr >= wr_os; Short: prev_wr > wr_ob AND curr_wr <= wr_ob; filtro EMA opcional; DEFAULT_PARAMS: wr_period=14, wr_os=-80, tp_pct=0.8, sl_pct=0.8, ema_period=0; 3 instâncias: williams_r_xau, williams_r_wti, williams_r_ton
  backtest/
    __init__.py         <- Empty
    scanner_v2.py       <- **Scanner v2 — adiciona 4 dimensões em cima do scanner.py**: (a) **ADX regime filter** (`_adx_cache`, grids `ADX_PERIODS=[0,14]`, `ADX_MIN_TREND=[20,25]`) — semântica invertida por família: mean-reversion (BB_Stoch, BB_Reversion, BB_RSI, RSI_Scalp, Stoch_Scalp, Williams_R) bloqueia quando ADX >= min (só entra em ranging); trend (EMA_Cross, MACD_Cross) bloqueia quando ADX < min (só entra em trending); period=0 = sem filtro. (b) **Session filter UTC** (`_session_mask(ts_ms, start, end)` exclusivo no end; grid `SESSION_FILTERS=[(0,24), (7,21), (13,21)]`); (0,24) = sem filtro. (c) **ATR-based TP/SL** (`_atr_cache(period=14)`, grids `ATR_TP_MULTS=[1.0,1.5,2.0]`, `ATR_SL_MULTS=[0.5,1.0,1.5]`) — quando `atr_tp_mode=True`, `tp_dist = atr * atr_tp_mult`, `sl_dist = atr * atr_sl_mult` (distância absoluta por candle); quando False usa tp_pct/sl_pct legados. (d) **Walk-forward optimization** `run_scan_wfo(asset, total_days=180, n_windows=4, train_ratio=0.7, strategies=None, top_n=5, max_combos_per_family=5000)` — divide a janela em N pares sequenciais train/test, roda `run_scan_v2` no IS, replaya os `top_n` aprovados no OOS via `_replay_params_on_slice` (cobre **todas as 8 famílias**, parity garantida por test_scanner_v2_wfo_replay.py). Retorna `{windows: [{is, oos, params}], wfo_efficiency: sum(roi_oos)/sum(roi_is), sum_roi_is, sum_roi_oos}`. Efficiency >0.5 = generaliza; <0.5 = overfit. Core simulator: `_backtest_v2(sig_long, sig_short, close, high, low, tp_pct, sl_pct, bb_mid=None, tp_dist=None, sl_dist=None, session_mask=None, adx_mask=None)` — preserva prioridade SL > TP > BB-mid do scanner; quando arrays são None usa pct legado (para parity exata float). Sampling determinístico via `_iter_combos(iterables, max_combos=5000)` usando `Random(42)` quando o produto Cartesiano excede o cap — combos por família ficam reproduzíveis sessão a sessão. Reusa `_load_csv`, `_stats`, `_monthly_breakdown`, `APPROVAL`, `_bb_cache`, `_bb_mid_cache`, `_ema_cache`, `_rsi_cache`, `_stoch_cache`, `_scale_tp_sl`, `_tf_minutes` de `scanner.py` via import (não modifica scanner.py). Cada result dict carrega 7 novos campos: `adx_period`, `adx_min`, `session_start`, `session_end`, `atr_tp_mode`, `atr_tp_mult`, `atr_sl_mult`. Schema compatível com `apply_result` existente (basta passar o dict). Entry point público `run_scan_v2(asset, days=90, strategies=None, timeframe="5m", max_combos_per_family=5000)`. Sem integração com dashboard ainda — uso via Python/CLI.
    scanner.py          <- Scanner vetorizado de parâmetros **multi-TF**: `SUPPORTED_TIMEFRAMES=["5m","15m","30m","1h"]` e `_TF_MINUTES` para converter; `_load_csv(asset, days, timeframe="5m")` carrega `candles/{asset}_{tf}.csv`; `get_available_assets(timeframe="5m")` lista CSVs daquele TF; `_stats` recebe `mins_per_candle` para TPD correto por TF; cada `_scan_*(..., *, window_start_idx=0, mins=5)` propaga `mins` para `_stats`. **TP/SL escalado pelo TF**: helper `_scale_tp_sl(tps, sls, mins)` multiplica arrays base por `sqrt(mins/5)` (modelo random-walk de volatilidade vs tempo) — 5m=1.0×, 15m≈1.73×, 30m≈2.45×, 1h≈3.46×. Cada um dos 8 scanners chama `TPS, SLS = _scale_tp_sl(TPS, SLS, mins)` logo após definir os arrays base. Sem isso scans em TFs maiores usariam TP/SL apertados (dentro do range de 1 vela) e inflariam artificialmente a WR. **Breakdown mensal de performance**: `_backtest` retorna `list[tuple[float, int]]` — `(return_pct, entry_idx)`. `_stats` extrai os returns e, se `ts_ms` for passado E o período ≥31 dias, chama `_monthly_breakdown(trades_with_idx, ts_ms)` que agrupa por mês calendário (YYYY-MM via `pd.Timestamp(ts, unit='ms', tz='UTC').strftime('%Y-%m')`) e devolve `[{month, trades, wr, roi, pf}, ...]`. O campo `monthly` é adicionado ao dict de cada resultado aprovado. `run_scan` extrai `ts_ms = df["ts_ms"].values` e passa para cada `_scan_*` via kwarg `ts_ms`. UI scanner.html exibe coluna "Mensal" com chips compactos coloridos (verde/vermelho por sinal do ROI), label em PT (Jan/Fev/Mar...), tooltip com `trades/WR/PF/ROI` completo. Útil para identificar estratégias **consistentes** vs as que tiveram só 1 mês bom inflando o resultado global; `run_scan(asset, days, strategies, progress_cb, timeframe="5m")` valida TF, faz `_update_csv` SÓ se TF==5m (auto-update só 5m por enquanto), carrega CSV do TF, marca `r["tf"]=timeframe` em cada resultado e inclui `timeframe` no retorno; `start_scan_job(asset, days, strategies, timeframe)` propaga TF; **`apply_result(asset, strategy, params, tag=None, timeframe="5m")`**: passa `timeframe` para `manager.register_dynamic_instance(...)` (instância criada sempre com TF no nome — não usa mais `_INSTANCE_MAP` hardcoded para novos applies), inclui `translated["timeframe"]=tf` (estratégia live usa para escolher df) e `scanner_metrics["timeframe"]`. Endpoints: `GET /api/scanner/assets?timeframe=`, `POST /api/scanner/run` (body inclui `timeframe`), `POST /api/scanner/apply` (body inclui `timeframe`); UI scanner.html tem dropdown TF que recarrega assets ao mudar e nome da instância no modal mostra `{prefix}_{asset}_{tf}[_{slug}]`. **chama `csv_loader._update_csv(asset)` no início de cada scan** (baixa candles faltantes da Lighter REST antes de simular); carrega CSV com `days + _SCAN_WARMUP_DAYS` (=2) extras para que indicadores estejam quentes no primeiro candle da janela "real". `window_start_idx` calculado em `run_scan` marca onde a janela real começa; helper `_apply_window(sl_long, sl_short, window_start_idx)` zera sinais antes desse índice em todas as 8 famílias — trades só são contados dentro da janela 30d/90d verdadeira, mas indicadores no início da janela já têm valores válidos (sem efeito de warmup-na-borda). `n_real` (candles na janela real) usado pelo `_stats` para TPD; rebimba na realidade do bot live que sempre opera com buffer quente. Roda grid search para 8 estratégias usando numpy/pandas-ta vetorizado; indicadores pré-computados por grupo de params (sem recalcular por combo); lógica de sinal idêntica às estratégias live; simulação bar-a-bar apenas nos trades. **Prioridade de exit per-candle SL > TP > BB-mid** (mesma regra de engine.py — sem tie-break por close). `_backtest(...)` aceita `bb_mid` opcional (array da BB midline); quando passado, simula saída pela midline com mesma prioridade; outcome BB-mid retorna percent change real (close vs entry), não tp_pct/sl_pct. Helper `_bb_mid_cache(close_s, periods)` pré-computa SMA por período. **As 3 famílias com BB midline (BB_Stoch, BB_Reversion, BB_RSI) testam ambos `bb_mid_exit ∈ {False, True}` no grid** — dobra o número de combos dessas famílias, cada combo aprovado carrega o campo `bb_mid_exit` no dict de resultado. **BB_Reversion usa a mesma regra de entrada do bot live**: `BBP_prev < bbp_th AND close > BBL AND close < BBM` (long, simétrico p/ short) — espera reversão começar antes de entrar. Antes usava só `BBP_curr < th` (mais permissivo) e aprovava combos que o bot nunca executava em produção. Resultados agora batem com `engine.py` e `bot/strategies/bb_reversion.py` ao vivo. Critérios de aprovação: PF≥1.1, max_dd≤30%, min_trades=5 (dict APPROVAL). **wr_min/tpd_min/tpd_max desabilitados (valores permissivos: 0.0/0.0/999.0)** porque (a) wr_min assumia TP/SL simétricos — combos com TP>SL e WR baixo mas PF alto eram rejeitados injustamente; PF já captura "ganha mais do que perde". (b) tpd_min/tpd_max calibrados pra 5m bloqueavam combos legítimos no 15m/1h. Filtros equivalentes continuam disponíveis no painel de UI (Min Win Rate, Min TPD, Max TPD) — só não bloqueiam mais por default. Esses 3 gates são compartilhados entre scanner.py e scanner_v2.py via import; 8 estratégias: BB_Stoch (~864), Stoch_Scalp (~243), EMA_Cross (~135), BB_Reversion (~864), RSI_Scalp (~216), BB_RSI (~1728), MACD_Cross (~135), Williams_R (~162); Williams %R: os_levels negativos (-80,-70,-60), ob=os+100; MACD colunas: MACD_ (linha), MACDs_ (signal); job assíncrono via `start_scan_job(asset, days, strategies)` / `get_scan_job(job_id)`; **`apply_result(asset, strategy, params, tag=None, timeframe="5m")`**: traduz params → instância live, sempre via `manager.register_dynamic_instance(strategy, asset, tag=tag, timeframe=timeframe)` (cria ou atualiza instância dinâmica `{prefix}_{asset}_{tf}[_{tag}]`); o antigo `_INSTANCE_MAP` hardcoded foi removido após migration M6 — todas as instâncias hardcoded de manager.py agora têm `_5m` no sufixo, então o caminho dinâmico cobre todos os casos uniformemente; salva 3 keys no DB de uma vez via `set_configs`: `strategy.{name}.params` (params traduzidos), `strategy.{name}.scanner_metrics` (JSON com strategy, asset, **tag** (str|null), applied_at ISO, scanner_params raw, trades, wr, pf, roi, tpd, max_dd — usado pela aba Estratégias) e `strategy.{name}.enabled=true` (auto-ativa); `_METRIC_KEYS = {trades, wr, pf, roi, tpd, max_dd, approved}` filtra métricas dos params raw; `_translate_params` cobre todas as 8 estratégias mapeando campos do scanner → nomes dos DEFAULT_PARAMS das estratégias live + **overrides defensivos** para alinhar estratégia live ao comportamento do scanner: **BB_Stoch, BB_Reversion e BB_RSI propagam `bb_mid_exit` do scanner para os params do live** (não força mais False — backtest e bot live vão se comportar exatamente como o scanner aprovou), BB_Reversion força `rsi_long_max=100, rsi_short_min=0` (scanner não usa RSI guard), EMA_Cross força `use_atr_sl=False` (scanner usa sl_pct fixo, evita ATR mode da instância hardcoded ema_cross_hype); `get_available_assets()` lista CSVs em candles/; smooth_k=3 em todos os stoch
    csv_loader.py       <- Helpers compartilhados de I/O de candles: `_load_candles_csv(asset, interval, days=None, extra_days=0)` **prefere CSV nativo** do TF pedido (`candles/{asset_lower}_{interval}.csv` — ex: `pump_1h.csv`) e cai em resample do 5m só se o nativo não existir (suporta epoch ms ou datetime string YYYY-MM-DD HH:MM:SS, coluna `timestamp` ou `ts`); `_update_csv(asset, progress_cb=None, interval="5m")` busca candles faltantes na Lighter REST (`/api/v1/candles`) **no TF pedido** via `lighter_get` paginando em batches de 500 (`_fetch_lighter_candles_since`); engine/scanner agora lêem `params["timeframe"]` (default "5m") e propagam para `_load_candles_csv` + `_update_csv` — backtest de estratégias 15m/30m/1h consome diretamente os CSVs nativos baixados pela aba Ativos sem resample; `_get_lighter_market_id(asset)` resolve market_id via `/api/v1/orderBookDetails?filter=perp` (cache de 5 min); ao fazer append, normaliza CSV para epoch ms inteiro independente do formato original; `_CANDLES_DIR = Path(__file__).parents[3] / "candles"`; `_INTERVAL_MS` mapa de intervalos→ms. Consumido por `engine.py` e potencialmente outros consumidores que queiram ler candles do disco. **Aba Ativos**: expõe helpers públicos para a UI — `list_lighter_perp_markets()` retorna lista de dicts com `{symbol, market_id, last_price, volume_24h_usd, open_interest_base, open_interest_usd, price_change_24h_pct, daily_trades_count}` (extraído direto do payload `/api/v1/orderBookDetails?filter=perp`); também warma o `_market_id_cache`. `SUPPORTED_DOWNLOAD_INTERVALS = ["5m", "15m", "30m", "1h"]` define quais resoluções podem ser baixadas. `get_csv_status(asset, interval="5m")` lê `candles/{asset_lower}_{interval}.csv` (via helper `_read_native_csv` que NÃO faz resample — é distinto de `_load_candles_csv`) e retorna `{has_csv, rows, first_ts, last_ts}`. `download_full_history(asset, interval="5m", progress_cb=None)` baixa toda a história da Lighter REST no intervalo escolhido (chama `/api/v1/candles` com `resolution={interval}`, paginação backward em batches de 500 até esgotar histórico) e salva em `candles/{asset_lower}_{interval}.csv` no formato epoch-ms. **Dois passes quando o CSV já existe** (helper interno `_fetch_window`): (a) **forward update** de `last_ts` até `now_ms` (pega candles novos); (b) **backward backfill** de `0` até `first_ts - 1` (preenche gap antigo caso um download anterior tenha ficado parcial por interrupção/rate-limit/bug). Sem o backfill, um CSV truncado nunca seria reparado — clicar "Atualizar" só atualizava o delta forward. Retorna `{ok, interval, rows, added, first_ts, last_ts}` ou `{ok: False, error}`. Cria `_CANDLES_DIR` se não existir. **Importante**: `_load_candles_csv` (usado por engine/scanner) continua lendo `_5m.csv` e fazendo resample para outros TFs — os CSVs nativos baixados (15m/30m/1h) ficam disponíveis no disco para uso futuro mas não são consumidos automaticamente pelo backtest/scanner atuais. `_RESAMPLE_RULES` agora inclui `"30m": "30min"` e `_INTERVAL_MS` inclui `"30m": 1_800_000`.
    engine.py           <- Vectorized backtest engine (substituiu o walk-forward antigo após validação de fidelidade). Carrega candles via `csv_loader._load_candles_csv` e atualiza via `csv_loader._update_csv` no início de cada run. Indicadores pré-computados sobre série inteira com pandas_ta; sinais como máscaras booleanas numpy (uma `_signals_<family>` por família); outcome via numpy.argmax em slice booleano de SL/TP/BB-mid começando em i+1. Prioridade per-candle: **SL > TP > BB-mid** (pessimista, sem tie-break por close — alinhado ao scanner). 8 famílias registradas em `_FAMILY_FNS`: bb_stoch, bb_reversion, stoch_scalp, ema_cross (suporta ATR SL via `sl_dist` array), rsi_scalp, bb_rsi, macd_cross, williams_r. `_resolve_family` faz longest-prefix match; `_resolve_strategy_instance` mapeia nome genérico → instância específica (ex: "bb_reversion" + "BTC" → "bb_reversion_btc") via `STRATEGY_MAP` e `DEFAULT_PARAMS["assets"]`. Params lidos do DB via `bot_db.get_strategy_config(name)["params"]` mergeado com `strategy.DEFAULT_PARAMS`. Coerção `bb_mid_exit`, `use_atr_sl`, `atr_tp_mode` via `str(...).lower() not in ("false","0","no")` para sobreviver strings vindas do DB. `start_backtest_job()` → str (uuid), `get_job()` → dict; background threads via threading. Sem cache (recomputa do zero, fast o suficiente: ~100× mais rápido que a versão antiga em bb_stoch_btc 30d). Trades sem TP/SL hit descartados. Job dict inclui `elapsed_s`. PnL via `_add_pnl` (fee_rate * trade_size_usd, round trip). **Filtros do scanner v2 aplicados em `_run_backtest`** via `_apply_v2_filters(...)` (ADX + session) e `_resolve_atr_distances(...)` (ATR TP/SL): `adx_period` (0=off), `adx_min` — mask depende da família (`ema_cross`/`macd_cross` exigem `adx >= adx_min`, mean-reversion exige `adx < adx_min` via `_TREND_FAMILIES_FAST`); `session_start`/`session_end` (UTC, (0,24)=off) — mask por hora UTC do `ts` ms; `atr_tp_mode=True` força `tp_dist = ATR*atr_tp_mult` e `sl_dist = ATR*atr_sl_mult` (sobrescreve qualquer sl_dist da família — ex: EMA_Cross use_atr_sl), `atr_period` default 14. Instâncias v1 (sem esses campos) caem nos defaults seguros = sem filtro, comportamento idêntico ao engine pré-v2. `_simulate_fast` aceita `tp_dist` E `sl_dist` per-candle. Log `[backtest] v2 filters:` resume params + entries_long/short após filtros para auditoria.
    report.py           <- `compute_metrics(trades, initial_capital) → dict`; calcula win_rate, total_pnl, roi, max_drawdown, profit_factor, `trades_per_day` (média de trades/dia no span first→last entry; 0 para <2 trades ou span≤0), cumulative_pnl series; `bb_mid` conta como win se pnl > 0, como loss se pnl ≤ 0
dashboard/
  app.py                <- Flask server + SocketIO + API endpoints; inclui /backtest, /scanner, /strategies, /ativos, /analise pages; /api/backtest/run (POST), /api/backtest/status/<job_id> (GET); endpoints scanner /api/scanner/*; endpoints estratégias: GET /api/strategies (todas), POST /api/strategies/<name> (toggle/save params), **GET /api/strategies/applied** (estratégias visíveis na aba Estratégias — união de: (a) aplicadas via Scanner via `strategy.<name>.scanner_metrics` **excluindo as arquivadas (`archived=true` no JSON)**; (b) enabled=true sem métricas, para preservar configurações legadas. Retorna `{name, display_name, enabled, params, metrics}` com `metrics={}` para legadas. Sort: com métricas primeiro, depois por data de aplicação, depois por nome), **DELETE /api/strategies/applied/<name>** (soft-delete: seta `archived=true` dentro do JSON `scanner_metrics` e `enabled=false`; NÃO apaga a key — assim some da aba Estratégias mas continua aparecendo em `/api/analise` para preservar histórico scanner×live); **Aba Ativos**: rota `/ativos` (template `ativos.html`); `GET /api/ativos?interval={5m|15m|30m|1h}` (lista todos os perp da Lighter com status do CSV local naquele intervalo: symbol, market_id, interval, has_csv, rows, first_ts, last_ts + métricas de mercado; baixados primeiro, ordenados por rows DESC; depois disponíveis); `GET /api/ativos/intervals` retorna `SUPPORTED_DOWNLOAD_INTERVALS` (`["5m","15m","30m","1h"]`); `POST /api/ativos/download` `{asset, interval}` **enfileira** o download e retorna `{job_id, existing, queue_position}`; deduplica por chave `{asset}|{interval}`: se já há job `queued` ou `running` para o par, devolve o `job_id` existente com `existing: true`. **Fila FIFO com worker único** (`_ativos_queue` = `queue.Queue()`, thread daemon `ativos-worker` iniciada lazily via `_ensure_ativos_worker`) garante 1 download por vez — evita estourar rate-limit da Lighter REST quando o usuário clica em vários "Baixar" em sequência. Status do job: `queued` → `running` → `done`/`error`. `_ativos_queue_position(job_id)` calcula a posição na fila (0 = rodando agora, 1 = próximo, etc). `GET /api/ativos/download/<job_id>` retorna o job + `queue_position` (anexado dinamicamente para status `queued`). **`POST /api/ativos/update-all`** **enfileira N jobs individuais** (um por CSV presente em `candles/`, descoberto via regex `^([a-z0-9]+)_(\d+[mh])\.csv$` filtrado por `SUPPORTED_DOWNLOAD_INTERVALS`) — todos passam pela mesma fila do worker (não tem worker separado). Retorna `{batch_id, total}`; batch é guardado em `_ativos_updateall_batches[batch_id] = {job_ids, started_at}` e o batch ativo em `_ativos_updateall["batch_id"]`. Se já há batch ativo com jobs ainda `queued/running`, devolve o existente. `GET /api/ativos/update-all/<batch_id>` agrega o status dos N jobs e retorna `{status, total, current (done+error), queued, errors[], message}` — mensagem inclui o ativo que está rodando agora. Esse endpoint é disparado pelo botão "Atualizar lista" na UI — substitui o reload simples por bulk forward+backward em todos os baixados, reparando gaps de downloads parciais sem o usuário precisar clicar em cada card individualmente; "backtest_page", "scanner_page", "strategies_page", "ativos_page" excluídos do check_configured redirect
  templates/            <- HTML pages (Jinja2)
  static/css/           <- Dashboard CSS (dark theme)
  static/js/            <- Dashboard JS (SocketIO, Chart.js)
tests/                  <- pytest suite (pytest.ini na raiz de hyperliquid-bot/)
logs/                   <- Daily rotated log files
bot_data.db             <- SQLite database (created on first run)
guiahl.md               <- Referência JS/TS do SDK Hyperliquid (HIP-3, ordens, funding)
audit_phantom_trades.py <- Script utilitário: cruza trades fechados do DB contra `get_recent_fills` da Lighter; flaga (e opcionalmente deleta com `--delete`) trades sem fill real correspondente. Usa janela [entry_time-60s, exit_time+60s] e tolerância de ±5% no size. Útil para limpar trades fantasma gerados antes da correção da `market_open`.
cleanup_orphan_triggers.py <- Script utilitário: varre todos os mercados perp da Lighter, lista TP/SL trigger orders ativas em assets SEM posição (órfãs da OCO sintética) e cancela. Uso: `python cleanup_orphan_triggers.py` (dry-run, só lista) ou `python cleanup_orphan_triggers.py --apply` (cancela). Suporta `--asset <SYMBOL>` para limitar a um ativo. Usar quando o bot acumular `canceled-reduce-only` em algum asset (sintoma: trades em LIT/BRENTOIL/qualquer-asset retornando esse status sem motivo aparente).
```

O bot e o dashboard compartilham o mesmo banco SQLite (`bot_data.db`). O dashboard roda como servidor Flask na porta 8080, e o bot roda em uma thread separada controlada pelo dashboard.

---

## Instalacao e Setup

### 1. Requisitos
- Python 3.10 ou superior
- pip

### 2. Instalar dependencias

```bash
cd hyperliquid-bot
pip install -r requirements.txt
```

### 3. Rodar

```bash
python run.py
```

Isso inicia o dashboard em `http://localhost:8080`. Na primeira execucao, voce sera redirecionado para a tela de Configuracoes para inserir suas credenciais.

### 4. Configurar credenciais

Na tela de Configuracoes do dashboard:
1. Insira seu **Account Address** (endereco publico da carteira)
2. Insira sua **Secret Key** (chave privada ou chave de API wallet)
3. Escolha **Testnet** ou **Mainnet**
4. Defina os ativos monitorados (ex: BTC, ETH, SOL)
5. Clique em **Salvar Configuracoes**

### 5. Iniciar o bot

Na mesma tela de Configuracoes, use os botoes:
- **Iniciar** — comeca o loop do bot
- **Pausar** — pausa sem desconectar
- **Parar** — para completamente

---

## Testnet vs Mainnet

- **Testnet** (padrao): usa `https://api.hyperliquid-testnet.xyz`. Ideal para testes sem risco real.
- **Mainnet**: usa `https://api.hyperliquid.xyz`. Opera com dinheiro real.

A troca e feita pelo toggle na tela de Configuracoes. O bot precisa ser parado e reiniciado apos mudar de rede.

Para usar testnet:
1. Acesse `https://app.hyperliquid-testnet.xyz`
2. Crie uma conta e deposite USDC de teste
3. Copie as credenciais para o dashboard

---

## Modulos

### `bot/db.py` — Banco de Dados
Camada SQLite com thread safety (WAL mode). Cinco tabelas:
- **`get_strategy_config(name)`** — lê `enabled` e `params` do DB; na primeira vez que uma estratégia é vista (sem entrada no DB), persiste `enabled=false` imediatamente via `set_config` — default OFF: usuário precisa ativar explicitamente na tela de Config; garante que desabilitar → reiniciar sempre respeita o estado salvo; corrigido bug onde o return usava `enabled == "true"` em vez de `enabled` (bool double-comparison)
- **profiles** (multi-perfil): `id, name UNIQUE, exchange, lighter_wallet_address, lighter_public_key, lighter_private_key, hyperliquid_address, hyperliquid_secret, created_at, updated_at` — cada perfil tem suas próprias credenciais; trades/signals/logs/config são isolados por `profile_id`. Helpers em `bot/db.py`: `list_profiles()` (público, sem chaves privadas), `get_profile(id)` (full row), `create_profile(name, exchange, credentials)`, `update_profile(id, ...)`, `delete_profile(id)` (cascata em trades/signals/logs + `profile.<id>.%` config keys). Uniqueness guard em `lighter_wallet_address` (mesma wallet em 2 perfis = COI counter brigando). Helpers de config per-perfil: `get_profile_config(profile_id, key)`, `set_profile_config(profile_id, key, val)`, `set_profile_configs(profile_id, {...})` — prefixam tudo com `profile.<id>.`
- **trades**: todas as operacoes (abertas e fechadas) — coluna `profile_id INTEGER DEFAULT 1` (M8a)
- **config**: pares chave/valor — keys per-perfil ficam sob `profile.<id>.*`; keys globais (`selected_exchange`, `use_lighter_ws_candles`, `last_ts.*`, `_migration_*`, `flask.secret_key`) permanecem sem prefixo
- **logs**: logs persistidos — coluna `profile_id` (NULL = log global do candle manager)
- **signals**: todos os sinais detectados — coluna `profile_id INTEGER DEFAULT 1`
- **`get_strategy_stats()`** — retorna lista de dicts `{strategy, total, open_count, closed_total, wins, win_rate, pnl, avg_slippage_pct, enabled}` agrupados por estratégia (trades fechados). `enabled` vem do `config` table (`strategy.<name>.enabled`) e é usado pela aba Overview para separar estratégias ativas/inativas
- **Migration M6 (`_migrate_legacy_strategy_names_to_5m`)** — one-shot na `init_db`. Renomeia as 28 instâncias hardcoded legadas (bb_stoch_btc, bb_reversion_eth, etc.) para incluir `_5m` no sufixo. Move config keys (`strategy.<old>.params`, `.enabled`, `.scanner_metrics` → `<new>`), atualiza `trades.strategy` e `signals.strategy_name` para os novos nomes. Idempotente via marker `_migration_strategy_names_5m=done`. Não toca em entradas dinâmicas (criadas via scanner com tag/TF) nem em nomes que já existem com o sufixo
- **Migration M7 (`_migrate_legacy_dynamic_instances_to_5m`)** — one-shot na `init_db`, roda DEPOIS de M6. Renomeia instâncias **dinâmicas** legadas (criadas pelo scanner antes do multi-TF) adicionando `_5m` logo após o asset: `bb_rsi_sol_60_26_5` → `bb_rsi_sol_5m_60_26_5`; `bb_stoch_xau` → `bb_stoch_xau_5m`. Varre `strategy.<X>.params` no DB, identifica prefixo conhecido (longest-match em `_KNOWN_PREFIXES`), e ignora instâncias que já têm TF (token após asset em `_SUPPORTED_TFS_FOR_MIGRATION={"5m","15m","30m","1h"}`). Move config keys + atualiza trades e signals igual M6. Idempotente via marker `_migration_dynamic_strategy_5m=done`. Necessária para que os cards na aba Overview mostrem o TF nos nomes (não só nas instâncias hardcoded)
- **Migration M8 (`_migrate_to_multi_profile`)** — one-shot na `init_db`, roda DEPOIS de M6/M7. **M8a:** adiciona coluna `profile_id INTEGER DEFAULT 1` em `trades`, `signals`, `logs` + índices. **M8b:** (1) cria perfil `id=1, name="Default"` populado a partir das credenciais globais legadas (`lighter_wallet_address`, `lighter_public_key`, `lighter_private_key`, `account_address`, `secret_key`); (2) namespaceia keys per-perfil em `profile.1.<key>` — prefixos `strategy.`, `risk.`, `sizing.` + exact keys `bot_status`, `assets`, `lighter.client_order_counter`; (3) backfill `profile_id=1` em rows com `profile_id IS NULL`. **Não deleta** as credenciais globais (Fase 2 ainda lê de global; Fase 4 vai fazer essa migração final). Mantém intocadas: `selected_exchange`, `use_lighter_ws_candles`, `last_ts.*`, markers `_migration_*`, `flask.secret_key`. Idempotente via marker `_migration_multi_profile=done`. Helper `_is_m8_profile_key(key)` decide o que migrar; constants `_M8_GLOBAL_KEYS`, `_M8_PROFILE_PREFIXES`, `_M8_PROFILE_EXACT_KEYS` no topo do arquivo. **M8c (`_fix_profile_credential_columns`)** — corrige column names errados shipados em M8b (`lighter_account_index/api_key_*` → `lighter_wallet_address/public_key/private_key`) via `ALTER TABLE RENAME COLUMN` e re-popula a row do Default a partir das chaves legadas. Idempotente via `_migration_fix_profile_cred_cols=done`

### Multi-perfil — Refinamentos pós review (Important)

- **Cópias per-perfil dos `last_*_ts` no dispatch:** `_on_candle_close_dispatch` faz `dict(last_15m_ts)` (idem 30m/1h/4h/1d) ao chamar `process_asset` pra cada perfil. Sem isso, o primeiro perfil a processar um asset compartilhado (ex: CRCL monitorado tanto pelo Default quanto pelo "15 min") mutava o dict global em `_detect_new`; o segundo perfil então via `latest_ts <= store` e `new_15m/30m/1h` virava `False` — suas estratégias TF maior nunca rodavam no boundary. Persistência via `db.set_last_candle_ts(tf, asset, ts)` continua global e idempotente (ambos perfis escrevem o mesmo ts pra o mesmo boundary)
- **Staleness guard em `_detect_new`:** constante `_TF_STALENESS_SEC = 120` em `main.py` + map `_TF_INTERVAL_MS` (15m/30m/1h/4h/1d). Quando `_detect_new` encontra um candle "novo" cuja idade desde o close (`now - (latest_ts + interval_ms)`) excede 120s, atualiza o store/DB normalmente (pra não re-detectar nas próximas calls) mas retorna `False` — nenhuma estratégia é avaliada nesse boundary. Bloqueia o cenário onde um restart de pm2 deixa o bot offline durante um boundary 1h e o primeiro 5m close pós-restart dispararia uma trade 5+ min atrasada com preço diferente do que o backtest avaliou (caso real: WLD #2722, candle 1h 17:00 BRT detectado e executado 17:05 BRT). Loga WARNING com a idade em segundos
- **`_subscribed_profile_ids()` x `_running_profile_ids()`:** dois conjuntos distintos. Subscribed inclui `bot_status in ("running","starting")` e dirige `_union_assets` + `_required_intervals_union` (ou seja, o que o candle manager precisa subscrever). Running é só `"running"` e dirige o dispatch (`_on_candle_close_dispatch` só fan-outa pra perfis fully-connected). Sem essa separação, `start_bot` chamava `_refresh_candle_manager_assets` enquanto o perfil ainda estava "starting" → 0 perfis running → candle mgr criado com `intervals=["5m"]` mesmo com estratégias 15m/30m/1h habilitadas → df_15m/30m/1h vazios em `process_asset` → estratégias TF maior nunca rodavam
- **Rebuild do candle manager quando intervals mudam:** `_refresh_candle_manager_assets` agora compara `set(candle_mgr.intervals)` com a união requerida; se diferentes, faz teardown e rebuild com os intervals certos. `update_assets` no candle manager só refresca asset list, não intervals — sem essa verificação, uma vez criado com `["5m"]` ficaria preso assim mesmo quando uma estratégia 15m fosse habilitada depois
- **`_load_dynamic_strategies` lê do novo namespace:** regex `^(?:profile\.\d+\.)?strategy\.(.+)\.params$` aceita tanto o legado `strategy.<n>.params` quanto o multi-profile `profile.<pid>.strategy.<n>.params`. Sem isso, todas as instâncias dinâmicas criadas pelo Scanner ficavam invisíveis no `STRATEGY_MAP` pós-M8 e a aba Estratégias aparecia em branco. Set `seen` evita registrar a mesma instância 2x quando ela existe sob 2 perfis (registration é global — STRATEGY_MAP não rastreia profile_id)
- **PnL/stats/trades/signals per perfil:** `get_daily_pnl`, `get_total_pnl`, `get_trade_stats`, `get_trades`, `get_today_trades`, `get_signals`, `get_cumulative_pnl`, `get_pnl_distribution` aceitam `profile_id` (default `None` = agregado em todos os perfis para mantar uso de scripts antigos). `/api/overview`, `/api/trades`, `/api/trades/cumulative-pnl`, `/api/trades/pnl-distribution`, `/api/signals` e `background_pusher` propagam `g.profile_id`/`pid`. Cada perfil vê só os próprios trades, signals e gráficos
- **DELETE para bot antes de excluir:** `/api/profiles/<id>` DELETE chama `stop_bot(pid)` síncrono e espera até 16s pelo reaper limpar os dicts antes de `db.delete_profile`. Fecha a janela onde `_on_candle_close_dispatch` podia processar candle pra perfil prestes a ser deletado
- **Clear credential na UI:** botão "×" ao lado de cada input de credencial no modal arma um flag visual; submit envia `null` explícito naquele campo (vs. omitir o campo, que preserva o valor). Backend (`update_profile`) já tratava `None` corretamente — coluna vira NULL. Pequeno hint "Em branco = manter atual. × para apagar." abaixo do fieldset
- **Dispatch paralelo:** `_dispatch_pool = ThreadPoolExecutor(max_workers=8)` global em `main.py`. `_on_candle_close_dispatch` faz submit por perfil ao pool em vez de iterar serial; perfil lento não bloqueia o resto. Quando há só 1 perfil running, roda síncrono (sem overhead). Em shutdown (`RuntimeError`), fallback síncrono garante que nenhum close é dropado

### Multi-perfil — Hardening pós code-review

- **`LighterExchangeClient.connect()` agora lê do `profiles`**, não do config global. Cada cliente busca `lighter_wallet_address`/`lighter_public_key`/`lighter_private_key` do `db.get_profile(self._profile_id)`. Fallback pro global SÓ é permitido pro `profile_id=1` (instalações pré-multi-perfil que nunca passaram pelo Profiles UI). Qualquer outro perfil sem credenciais na row levanta `ValueError("Missing Lighter credentials on profile N")`. Fecha o buraco onde 2 perfis com wallets diferentes acabariam assinando contra a mesma conta na Lighter
- **`_candle_mgr_owner_pid`** rastreia qual perfil dono do `LighterCandleManager` está usando como cliente. Se o reaper desconectar esse perfil, `_refresh_candle_manager_assets` destrói o candle_mgr e reconstrói com o cliente de outro perfil running (escolhe o `min(running_pids)` deterministicamente)
- **Status "starting"**: `start_bot` seta `bot_status=starting` antes de spawnar a thread. `bot_loop` flipa pra `running` SÓ após `client.connect()` retornar com sucesso. `_on_candle_close_dispatch` filtra por `status=="running"`, fechando a janela onde `process_asset` rodava com cliente meio-autenticado
- **M8b atômico via `with conn:`**: o corpo da migration está dentro de um sqlite3 transaction context manager. Crash mid-loop = rollback + marker NÃO set, próximo boot retenta limpo

### Multi-perfil — Polimento (Fase 5)

- **SocketIO per-perfil**: backend já emite `overview_update.<pid>` para cada perfil + legacy `overview_update` para profile=1; cliente (`static/js/dashboard.js`) usa `socket.onAny` para captar todos os `overview_update.<pid>`. Atualiza a bolinha do dropdown para QUALQUER perfil; só dispara o `hlUpdate` (que move os KPIs/status indicator do sidebar) quando o `profile_id` do payload bate com `_activeProfileId`
- **Logs UI** (`dashboard/templates/logs.html`): checkbox "Mostrar todos os perfis" passa `?all=1` para `/api/logs`. Default = só perfil ativo (linhas com `profile_id=<active>` OR `profile_id IS NULL` — logs globais do candle manager sempre visíveis)
- **`_sweep_stray_profile_keys(conn)`** (em `bot/db.py`) — sweep idempotente que roda em TODO `init_db()` (sem marker), absorvendo keys per-profile que algum processo legado tenha escrito no namespace global após M8b. Move para `profile.1.<key>` se o namespaced não existir; deleta a stray. Necessário pra cenário onde o bot rodando na versão antiga ainda escreve `strategy.<name>.enabled` direto enquanto a nova versão é deployada

### Multi-perfil — Execução paralela (Fase 4)

- **`main.py` agora usa dicts por perfil**, não singletons. Globais: `_bot_threads: dict[int, Thread]`, `_bot_clients: dict[int, BaseExchangeClient]`, `_risk_mgrs: dict[int, RiskManager]`, `_stop_events: dict[int, Event]`, todos guardados por `_bot_lock`. `candle_mgr` continua singleton (compartilhado por todos os perfis) sob `_candle_mgr_lock`
- **`_refresh_candle_manager_assets()`** — cria/atualiza/destrói o candle manager compartilhado. Chamado em `start_bot`, `stop_bot` (via reaper) e quando uma asset list de perfil muda. Sem perfis running → destrói; primeiro perfil → instancia (LighterCandleManager se `selected_exchange==lighter` AND `use_lighter_ws_candles==true`, senão BinanceCandleManager) com union de `_union_assets()` + `_required_intervals_union()`
- **`_on_candle_close_dispatch(asset, interval)`** — callback singleton plugado no candle manager. Gated em `interval=="5m"`. Itera `_running_profile_ids()` (perfis com thread alive E `bot_status=="running"`); para cada um, filtra pela union de assets do perfil + estratégias, e chama `process_asset(asset, cfg, ts_caches..., profile_id=pid)`
- **`bot_loop(profile_id)`** — loop per-perfil; NÃO cria mais candle manager. Apenas conecta o client, instancia `RiskManager(profile_id)`, persiste em `_risk_mgrs[pid]`, e roda heartbeat (status poll, TP/SL recovery, refresh do candle mgr quando assets do perfil mudam). Usa `_stop_events[pid]` em vez de `_stop_event` global
- **`process_asset(asset, cfg, ..., profile_id)`** e **`check_bb_mid_exit(asset, df_5m, profile_id)`** — leem client + risk_mgr dos dicts via `.get(profile_id)`; retornam silenciosamente se o perfil foi parado entre o fire e o dispatch
- **`start_bot(profile_id)`** — idempotente: spawn thread + builda cliente via `_build_client_for_profile` + chama `_refresh_candle_manager_assets`. Retorna `None` se falhar a construção do cliente (e seta `bot_status=error`)
- **`stop_bot(profile_id)`** — seta o stop_event do perfil, persiste `bot_status=stopped`, dispara thread `_reap_bot_thread(pid)` em background. Reaper faz join(timeout=15), disconnect do client, remove entries dos dicts e chama `_refresh_candle_manager_assets()` (que destrói o singleton se foi o último perfil)
- **`run.py` auto-resume** — itera `db.list_profiles()` no boot; pra cada perfil com `bot_status in ("running","paused")`, chama `start_bot(pid)` (e `pause_bot(pid)` se aplicável)
- **Endpoints `/api/profiles/<id>/bot/{start,pause,stop}`** em `dashboard/app.py` — bot control per perfil; legacy `/api/bot/{start,pause,stop}` continuam existindo e operam sobre `g.profile_id` (perfil ativo da session)
- **Lock de execução por `(profile_id, asset)`** em `bot/executor.py` (já feito na Fase 2 Task 2.6) — dois perfis podem abrir o mesmo asset no mesmo candle close sem se bloquear; race intra-perfil ainda é serializada

### Multi-perfil — Dashboard (Fase 3)

- **Flask session** — `dashboard/app.py` define `app.secret_key` persistido em `config['flask.secret_key']` (gerado random na primeira boot). `before_request` lê `session['active_profile_id']`, faz fallback para o primeiro perfil quando ausente/inválido, e seta `g.profile_id` que todos os endpoints usam
- **Endpoints CRUD em `dashboard/app.py`**: `GET /api/profiles` (lista + bot_status + is_active; oculta chaves privadas), `POST /api/profiles` (cria; rejeita duplicata de `lighter_wallet_address` com 409), `PATCH /api/profiles/<id>` (renomeia/edita credenciais), `DELETE /api/profiles/<id>` (bloqueia se for o último perfil ou se houver trade aberto; cascata; limpa session se perfil ativo foi removido), `POST /api/profiles/<id>/activate` (set session)
- **Dropdown no sidebar** (`dashboard/templates/base.html`) — entre brand e nav-list. Lista perfis com bolinha de status colorida (running/paused/stopped/error), ações Renomear/Editar credenciais/Excluir/+Novo perfil
- **Modal** no fim do body — campos por exchange (Lighter: wallet/public/private; HL: address/secret); modo `rename` esconde fieldsets de credencial; submit em branco preserva valores existentes (omitido do payload)
- **CSS** — `.profile-selector`, `.profile-menu`, `.profile-list`, `.modal-backdrop`, `.modal`, `.profile-status-dot.{running,paused,stopped,error}` em `static/css/dashboard.css`
- **JS** — `_profilesCache`, `_activeProfileId`, `loadProfiles`/`renderProfileMenu`/`activateProfile`/`openProfileModal`/`deleteActiveProfile` em `static/js/dashboard.js`; bootstrap em `DOMContentLoaded`

### `bot/logger.py` — Sistema de Logging
Logging estruturado com 3 destinos simultaneos:
- Console (stdout)
- Arquivo diario rotacionado (`logs/bot_YYYY-MM-DD.log`)
- Tabela SQLite (para visualizacao no dashboard)

### `bot/ws_client.py` — Cliente Hyperliquid
Wrapper sobre o hyperliquid-python-sdk:
- REST para snapshots de candles (1m, 5m, 15m)
- REST para funding rate, posicoes, saldo
- WebSocket opcional para candles ao vivo
- Reconexao automatica em caso de falha
- `get_recent_fills(asset, since_ms)` — busca fills reais via `info.user_fills_by_time` (usado para exit price correto)

### `bot/indicators.py` — Indicadores Tecnicos
Calculados a cada candle fechado usando pandas-ta:
- **EMA 9 e EMA 21** no timeframe 5m
- **RSI 2** no timeframe 1m
- **ATR 14** no timeframe 1m
- **Volume medio 20 periodos** no timeframe 1m
- **VWAP** (opcional) — calculado apenas sobre candles do dia corrente UTC; `None` se não houver candles hoje, não bloqueia `compute_all()`
- **StochRSI** (opcional) — `stochrsi_k`, `stochrsi_d`, `stochrsi_k_prev`, `stochrsi_d_prev`; `None` se NaN, não bloqueia `compute_all()`
- **`compute_all(df_1m, df_5m, cfg) -> dict | None`** — retorna dict com 8 chaves obrigatórias + 5 chaves opcionais (`vwap`, `stochrsi_k`, `stochrsi_d`, `stochrsi_k_prev`, `stochrsi_d_prev`). Retorna `None` somente se indicadores obrigatórios forem NaN. Parâmetro `stochrsi_period` lido do cfg (padrão 14).
- **`is_fee_viable(atr, price, tp_multiplier, fee_rate=0.0009) -> bool`** — retorna `True` se `(atr/price) * tp_multiplier > fee_rate`. Guard contra `price <= 0`. Importar com `from bot.indicators import is_fee_viable` (sem circular import).

### `bot/strategies/` — Sistema de Estratégias

Arquitetura plugável via `BaseStrategy` ABC. O `manager.py` avalia todas as estratégias habilitadas e retorna um sinal por estratégia. `bot/signals.py` está deprecated e delega ao manager. `check_bb_mid_exit()` no `main.py` cobre ambas as estratégias com BB mid exit.

**BBReversionStrategy** — instâncias separadas por ativo, cada uma habilitada/configurável individualmente no dashboard
- `bb_reversion_btc` (BTC): BB(10,2.0) + EMA50 + RSI<65/RSI>35 + bbp<0.10/bbp>0.90 | TP 2.0% / SL 0.8%
- `bb_reversion_eth` (ETH): BB(10,2.0) + EMA50 + RSI<65/RSI>35 + bbp<0.15/bbp>0.85 | TP 1.0% / SL 1.0%
- `bb_reversion_sol` (SOL): BB(10,2.0) + EMA200 + sem filtro RSI + bbp<0.05/bbp>0.95 | TP 2.0% / SL 0.5%
- Gatilho: `new_5m=True`; mean reversion via retorno do preço para dentro das Bollinger Bands
- Entry LONG: BBP_prev < bbp_long_threshold AND close_curr > BBL_curr AND close_curr < BBM_curr
- Entry SHORT: BBP_prev > bbp_short_threshold AND close_curr < BBU_curr AND close_curr > BBM_curr
- Filtros: EMA trend (close > EMA → só LONG; close < EMA → só SHORT) + RSI(14) guard (LONG: RSI < rsi_long_max; SHORT: RSI > rsi_short_min)
- `_resolve_params(params)` = `{**self.DEFAULT_PARAMS, **params}` — sem presets; params do DB sobrescrevem os extra_defaults da instância
- Presets (aggressive/balanced/conservative) removidos — não existem mais
- Signal contém: `tp_pct`, `sl_pct`, `bb_mid` (valor da midline no entry), `bb_mid_exit`
- Dashboard: `renderStrategies` usa `s.name.startsWith('bb_reversion')` para roteamento; `renderBBReversionFields` usa `strategy.name` dinamicamente para todos os IDs de campos

Para adicionar uma nova estratégia: criar classe herdando `BaseStrategy`, registrar em `REGISTERED_STRATEGIES` no `manager.py`. O helper `_insert_fee_block_signal` está em `BaseStrategy` e é herdado automaticamente. A assinatura de `evaluate()` é `evaluate(self, asset, indicators, funding_rate, cfg, params, df_1m=None, df_5m=None, **kwargs)`.

**REGRA OBRIGATÓRIA — Slippage:** Todo signal dict retornado por `evaluate()` DEVE incluir `"signal_price": close_curr` (fechamento da vela 5m que disparou o sinal). Isso é necessário para calcular e exibir o slippage médio por estratégia no dashboard. O executor persiste `signal_price` na tabela `trades` e loga o slippage em bps após o fill. Sem esse campo, a estratégia não terá slippage no painel e não será possível validar se ela é lucrativa após custos reais.

### `bot/executor.py` — Execucao de Ordens
- Market orders via IOC limit com slippage configuravel
- Suporta três modos de TP/SL (detectado pelos campos presentes no signal):
  - **Percentage mode** (`tp_pct` presente): TP = entry × (1 ± tp_pct), SL = entry × (1 ∓ sl_pct) — usado por `bb_reversion`
  - **Risk/Reward mode** (`sl_price_hint` presente): SL = sl_price_hint, TP = entry ± |entry - sl| × `rr_ratio` (default 1.5)
  - **ATR mode** (padrão): TP = entry ± ATR × tp_mult, SL = entry ± ATR × sl_mult
- TP automatico em 1.5x ATR do entry price
- SL automatico em 1x ATR do entry price
- TP/SL como trigger orders na exchange
- **Fees + PnL**: capturados via fills API usando `oid` para matching preciso. Uma ordem pode gerar múltiplos fills parciais; `_get_fill_data` agrega (soma) fee e closedPnl de todos os fills com mesmo oid. O `closedPnl` do close fill = lucro bruto (sem fees). PnL final = `closedPnl - total_fees + net_funding`
- **Fallback de PnL (Lighter)**: `market_close` na Lighter retorna `txHash` como `oid`, mas `get_recent_fills` usa `tradeId` — o match por oid nunca casa. Se `close_data["closedPnl"] == 0.0`, `close_position` usa `_get_close_pnl_fallback(client, asset, pre_close_ms - 5000)` que soma todos os fills desde 5s antes do close (sem filtro de oid).
- **Funding**: busca `user_funding_history` no período do trade e soma pagamentos de funding recebidos/pagos
- Helper `_get_fill_data(client, asset, oid, since_ms)` retorna `{"fee": float, "closedPnl": float}` de um fill pelo order ID
- Helper `_get_close_pnl_fallback(client, asset, since_ms)` retorna soma de todos os fills desde `since_ms` (sem filtro de oid) — usado quando o exchange não mantém o mesmo identificador entre order response e fills API
- **Slippage logging**: após fill, se o signal contiver `signal_price`, loga `slippage` em bps = `(avg_px - signal_price) / signal_price * 10000` (invertido para shorts). Slippage também persiste na coluna `signal_price` da tabela `trades` para análise posterior

### `bot/risk.py` — Risk Manager
- **Sizing**: `calculate_position_size(asset)` aceita dois modos via `sizing_mode`: `risk_pct` (account_value × risk_pct_per_trade%) ou `fixed` (valor `fixed_trade_size_usd` constante por trade, ignora account_value). Em ambos os modos, faz **guard de margem por alavancagem**: chama `client.get_max_leverage(asset)` e bloqueia o trade retornando 0.0 se `account_value < size_usd / max_leverage` — permite sizes > account_value enquanto a margem necessária couber (ex: $100 size em conta de $30 só passa se max_lev do ativo ≥ 4x). max_leverage é lido de `meta.universe[].maxLeverage` (HL) ou `10000 / default_initial_margin_fraction` do `orderBookDetails` (Lighter — o campo vem em **basis points**: 500=5%=20x, 200=2%=50x; cacheado em `LighterClient._market_cache["initialMarginFractionBps"]`).
- Maximo 2 posicoes abertas simultaneas
- Pausa automatica se perda diaria >= 5% do capital
- Monitora posicoes para detectar fechamento por TP/SL
- **Recovery de TP/SL ausentes**: a cada heartbeat (30s), para cada posição ainda aberta na exchange, verifica se os triggers existem via `client.get_open_trigger_order_types(asset)`; se ausentes, re-coloca via `client.place_tp_sl(..., which=missing)` passando apenas o set de legs faltantes (`{'tp'}`, `{'sl'}` ou ambos) — evita duplicar trigger orders que já estão na exchange. `place_tp_sl` (base + hyperliquid + lighter) aceita `which: set | None = None` (default = ambos)
- **Cleanup de TP/SL órfãs (2 camadas — rede de segurança barata)**:
  - **Pre-flight** em `executor.open_position`: imediatamente antes de enviar `market_open`, chama `client.cleanup_orphan_triggers(asset)`.
  - **Pos-close** em `check_open_positions_tp_sl`: após detectar `Position no longer on exchange` (trigger executou), tenta cancelar a outra leg.
  - **Nota importante:** verificação empírica via `/accountInactiveOrders` mostrou que **a Lighter já cancela automaticamente a outra leg da OCO sintética** quando a posição vai a zero (61 ocorrências de `canceled-reduce-only` legítimas observadas no histórico). As 2 camadas são no-op na prática (não há órfãs reais), mas mantidas como rede de segurança barata — o check `if position > 0 → no-op` impede cancelar triggers legítimos. **O sweep periódico no heartbeat foi removido** (era a 3ª camada) por desperdiçar API calls sem caçar problema real. Default no ABC é no-op (retorna 0); apenas `LighterExchangeClient` implementa de fato.
- **Fees + PnL**: open fee do banco (gravada na abertura); close fee + `closedPnl` agregados de TODOS os close fills parciais via API (média ponderada para exit_price, soma para fee/closedPnl). PnL = `closedPnl - total_fees + net_funding`. Fallback para cálculo manual se não houver close fill

---

## Estrutura do Banco de Dados

### Tabela `trades`
| Campo | Tipo | Descricao |
|-------|------|-----------|
| id | INTEGER PK | Auto increment |
| asset | TEXT | Ativo (BTC, ETH, etc) |
| side | TEXT | long ou short |
| entry_price | REAL | Preco de entrada |
| exit_price | REAL | Preco de saida |
| size | REAL | Tamanho da posicao |
| pnl | REAL | Lucro/prejuizo em USD |
| pnl_pct | REAL | Lucro/prejuizo em % |
| status | TEXT | open ou closed |
| entry_time | TEXT | ISO timestamp de entrada |
| exit_time | TEXT | ISO timestamp de saida |
| ema9, ema21, rsi2, volume, atr, funding_rate | REAL | Indicadores no momento |
| tp_price, sl_price | REAL | Precos de TP e SL |
| order_id | TEXT | ID da ordem na HL |
| strategy | TEXT | Nome da estrategia que gerou o trade (bb_reversion) |
| signal_price | REAL | Preco de fechamento da vela 5m que gerou o sinal (para calculo de slippage) |

### Tabela `config`
| Campo | Tipo | Descricao |
|-------|------|-----------|
| key | TEXT PK | Nome do parametro |
| value | TEXT | Valor como string |

### Tabela `signals`
| Campo | Tipo | Descricao |
|-------|------|-----------|
| id | INTEGER PK | Auto increment |
| timestamp | TEXT | Quando o sinal foi detectado |
| asset | TEXT | Ativo |
| side | TEXT | long ou short |
| executed | INTEGER | 1 se executado, 0 se bloqueado |
| reason | TEXT | Motivo do bloqueio (se aplicavel) |
| ema9, ema21, rsi2, volume, volume_avg, atr, funding_rate | REAL | Indicadores no momento |
| strategy_name | TEXT | Nome da estrategia que gerou o sinal |

### Tabela `logs`
| Campo | Tipo | Descricao |
|-------|------|-----------|
| id | INTEGER PK | Auto increment |
| timestamp | TEXT | ISO timestamp |
| level | TEXT | INFO, WARNING, ERROR, DEBUG |
| module | TEXT | Modulo de origem |
| message | TEXT | Mensagem do log |

---

## Sistema de Logging

### Arquivos
- Localizacao: `logs/bot_YYYY-MM-DD.log`
- Rotacao diaria automatica (30 dias de retencao)

### Niveis padrão
- **INFO**: eventos gerais do bot (conexão, status, ordens enviadas/fechadas)
- **WARNING**: ordem bloqueada (risco, funding), reconexao do websocket
- **ERROR**: falha de execucao, erro de conexao, excecao capturada
- **DEBUG**: valores detalhados de indicadores (ativavel pelo dashboard)

### Niveis customizados (registrados em `bot/logger.py`)
- **CANDLE** (15, abaixo de INFO): leitura de candle por ativo — `log.candle(...)`. Visível apenas em debug mode. Usado em `main.py` (`process_asset`).
- **SIGNALS** (22, acima de INFO): detecção de sinal pelas estratégias — `log.signals(...)`. Sempre visível. Usado em todas as estratégias e `manager.py`. **Inclui também scan logs permanentes**: a cada 5m close, TODAS as 8 estratégias (bb_reversion, bb_stoch, stoch_scalp, ema_cross, rsi_scalp, bb_rsi, macd_cross, williams_r) emitem um log no formato `[ASSET] <FAMILY> SCAN [instance_name] — close=X <indicadores_e_thresholds> trig=long:bool short:bool` ANTES do trigger check — permite comparar candle a candle com backtest quando há divergência live↔backtest, sem precisar modificar código. Filtrar por "SCAN" + nome da estratégia na aba Logs para auditoria. Mudanças nesse formato (adicionar/remover campo) devem ser feitas consistentemente nas 8 estratégias para preservar comparabilidade.
- **BACKTEST** (24, acima de INFO): progresso do backtest — `log.backtest(...)`. Sempre visível. Usado em `bot/backtest/engine.py`.

Esses níveis aparecem como opções de filtro na página de Logs do dashboard (com cores distintas: CANDLE=cyan, SIGNALS=verde, BACKTEST=laranja).

### Formato
```
2024-01-15 14:30:00 | INFO    | bot.executor   | [ETH] LONG filled — size=0.1 @ 2510.50 TP=2525.00 SL=2500.00
```

---

## Dashboard

Acessar em `http://localhost:8080` apos rodar `python run.py`.

### Telas
1. **Overview**: Status do bot, KPIs (capital, P&L, win rate), posicoes abertas, **Estratégias** (cards por estratégia: trades, wins, win rate, P&L), **Ativos Monitorados** (fee viability por ativo: ATR%, Req%, verde/vermelho)
2. **Trades**: Tabela historica com filtros, grafico de P&L acumulado, histograma de resultados
3. **Sinais**: Todos os sinais detectados com indicadores no momento
4. **Logs**: Visualizador em tempo real com filtro por nivel
5. **Estratégias** (`/strategies`): Cards das estratégias aplicadas via Scanner — cada card mostra header (display_name + chip do asset + toggle Ativa/Inativa + botão excluir), grid 3×2 de métricas (Trades, Win Rate, ROI, PF, TPD, Max DD com colorização verde/vermelho por threshold), lista inline dos scanner_params raw (k=v · k=v) e timestamp "Aplicado em DD/MM HH:MM"; auto-refresh a cada 10s; excluir chama DELETE /api/strategies/applied/<name>; config.html não tem mais seção de estratégias (foi removida)
6. **Backtest**: Simulação histórica por estratégia; dropdown de estratégias populado dinamicamente via `GET /api/strategies/applied` no `DOMContentLoaded` (mesma fonte da aba Estratégias — instâncias aplicadas via Scanner + legadas enabled=true); `STRATEGY_ASSET` JS map e `<optgroup>` são construídos em runtime a partir de `params.assets[0]` (fallback: último segmento do nome); KPIs incluem Trades/Dia (`trades_per_day` calculado em `compute_metrics`); guard impede rodar com seleção vazia;
6. **Scanner**: Grid search de parâmetros por ativo; seleciona ativo + período + estratégias; job assíncrono com polling a cada 2s; cards de resumo por estratégia; tabs de filtro; tabela de aprovados ordenada por ROI; botão Aplicar por linha (salva params na instância live via /api/scanner/apply); endpoints: GET /api/scanner/assets, POST /api/scanner/run, GET /api/scanner/status/<job_id>, POST /api/scanner/apply formulário (estratégia, período, tamanho, fee — default fee=0 pois Lighter tem taxa zero); job assíncrono com polling a cada 2s; KPIs (total trades, win rate, P&L, drawdown, profit factor, média/trade, **ROI %**); gráfico de P&L acumulado (Chart.js); tabela de trades simulados com TP/SL/outcome/duração; estratégia selecionada determina o ativo automaticamente via `STRATEGY_ASSET` map no JS
8. **Análise** (`/analise`): Cruza scanner metrics com performance ao vivo para encontrar quais m&eacute;tricas do backtest predizem sucesso real. Endpoint `GET /api/analise` retorna lista de strategies aplicadas (**inclui as arquivadas via soft-delete da aba Estratégias** — flag `archived: bool` no response) com `{timeframe, archived, scanner: {trades, wr, pf, roi, tpd, max_dd}, live: {closed_total, wins, win_rate, pnl, pnl_per_trade, avg_slippage_pct, open_count}}` (timeframe lido do `scanner_metrics`, default "5m"). UI: select de alvo (PnL absoluto vs PnL por trade), filtro de m&iacute;nimo de trades fechados, **tabela de Performance por Timeframe** (agrega por TF, mostra count, trades total, WR m&eacute;dio, alvo m&eacute;dio, PnL total e barra de compara&ccedil;&atilde;o; ordena pelo melhor TF; troféu 🏆 no TF top), **tabela de correla&ccedil;&atilde;o de Pearson** entre cada m&eacute;trica do scanner e o alvo (com barra visual, interpreta&ccedil;&atilde;o textual e classifica&ccedil;&atilde;o de for&ccedil;a) + **scatter plots** (Chart.js, um por m&eacute;trica do scanner, com cor por sinal do PnL). Auto-refresh a cada 15s. `analise_page` exclu&iacute;da do `check_configured` redirect.

9. **Scanner v2** (`/scanner_v2`): Aba dedicada para o `bot/backtest/scanner_v2.py`. Dois sub-modos via tabs: **Grid Scan** (mesma UX do `/scanner` mas com 3 colunas novas — ADX (`14/<20` se ativo, `off` caso contrário), Sess (`13-21h` ou `24h`), ATR (`tp_mult/sl_mult` ou `%`) — e filtros adicionais no painel lateral para isolar combos com ADX/sess/ATR on vs off) e **Walk-Forward** (form com total_days/n_windows/train_ratio/top_n/max_combos, KPIs no topo incluindo `wfo_efficiency` colorizado por threshold ≥0.7=verde / ≥0.5=neutro / <0.3=vermelho com label textual "Excelente/Bom/Marginal/Overfit", tabela por janela com barra de eficiência por linha, e `<details>` colapsável por janela mostrando top params IS vs ROI/trades OOS). **Filtros de consistência mensal** no painel do Grid Scan (computados client-side a partir do array `monthly`): `% meses+ ≥` (% de meses com ROI > 0), `Pior mês ≥` (worst month ROI), `Mediana mês ≥` (mediana dos ROIs mensais). Nova coluna **Consist** sortável (chave `pct_pos`) mostrando `{%+} / med {valor}` colorizado por threshold. **Multi-select de combos**: checkbox em cada linha (mestre no thead), barra `X / 10 selecionados` + botões `Limpar` e `▶ Validar em janelas`. Limite hardcoded `MAX_SELECTION=10`. Botão "Validar em janelas" abre modal com seletor de período (90/180/270/365 dias) e nº janelas (3/4/6/12). Botão "Aplicar" em ambos os modos reutiliza `apply_result` do scanner antigo via `POST /api/scanner_v2/apply` — os 7 campos novos (`adx_period`, `adx_min`, `session_start`, `session_end`, `atr_tp_mode`, `atr_tp_mult`, `atr_sl_mult`) ficam preservados em `scanner_metrics.scanner_params` mas NÃO viram params live (a estratégia em produção ainda usa só os params base + TP/SL %); o modal de apply alerta explicitamente sobre isso. **Replay em janelas** (`replay_combos_in_windows` em scanner_v2.py): para cada combo (param dict completo, até 10), fatia o período em N janelas sequenciais e chama `_replay_params_on_slice` em cada — **SEM split IS/OOS**, apenas teste de estabilidade temporal dos params exatos. Diferente do WFO: WFO testa se a *otimização* generaliza (re-roda scanner por janela); Replay testa se *esses params específicos* sobrevivem ao tempo. Retorna `{combos: [{params, windows: [{idx, trades, wr, pf, roi, max_dd}], summary: {pct_positive_windows, worst_window_roi, best_window_roi, median_window_roi, total_roi, n_windows}}]}`. Async via `start_replay_job` no mesmo `_jobs` registry (`kind: "replay"`). Endpoints: `GET /api/scanner_v2/assets?timeframe=`, `POST /api/scanner_v2/run` (grid scan async via `start_scan_v2_job`), `POST /api/scanner_v2/wfo` (WFO async via `start_wfo_job`), `POST /api/scanner_v2/replay` (replay async via `start_replay_job` — recebe `{asset, timeframe, days, n_windows, combos: [param dict, ...]}`), `GET /api/scanner_v2/status/<job_id>` (mesmo formato dos jobs do scanner antigo: status running|done|error + progress + result/error), `POST /api/scanner_v2/apply`. Job registry compartilhado entre grid/wfo/replay em `scanner_v2._jobs` (key `kind: "scan"|"wfo"|"replay"`). `scanner_v2_page` excluída do `check_configured` redirect (mesmo padrão do scanner antigo).
7. **Ativos** (`/ativos`): Lista todos os mercados perp da Lighter com status do CSV local em `candles/`. **Tabs de intervalo** no topo (5m, 15m, 30m, 1h — populadas via `GET /api/ativos/intervals`) selecionam qual resolução está sendo visualizada/baixada; toda a página reflete a tab ativa (o mesmo ativo pode estar baixado em alguns intervalos e não em outros). **Duas seções**: "Baixados" no topo como cards (rows/first_ts/last_ts + chip mostrando o intervalo + botão Atualizar) e "Disponíveis para baixar" abaixo como **tabela ordenável** com colunas Símbolo, Preço, Vol 24h, Open Interest, Δ% 24h, Trades e botão "Baixar {interval}" — todos os campos de mercado vêm do `/api/v1/orderBookDetails?filter=perp` (sem chamadas adicionais). Sort default: `volume_24h_usd` desc; cabeçalho clicável alterna asc/desc (asc para symbol, desc para colunas numéricas). Atualização **manual** via botão "Atualizar lista" — **não é só refresh de metadata**: o botão dispara `POST /api/ativos/update-all` que roda forward+backward backfill em todos os CSVs presentes em `candles/` (sequencial, evita rate-limit), mostra progresso `{current}/{total}` no próprio label do botão, e só então recarrega a lista. Garante que ativos com download anterior parcial sejam reparados sem precisar clicar em cada card. Download dispara `POST /api/ativos/download {asset, interval}` (job background, polling a cada 1.5s do `GET /api/ativos/download/<job_id>`); ao concluir, recarrega a lista se a tab ativa ainda for a do download. Dedup de jobs é por `{asset}|{interval}` — múltiplos intervalos do mesmo ativo podem rodar em paralelo. Candles salvos em `candles/{asset_lower}_{interval}.csv`; o 5m é consumido por Scanner/Backtest, os outros intervalos ficam disponíveis no disco para uso manual ou futura integração.
8. **Configuracoes**: Credenciais, rede, ativos, parametros de risco/indicadores/sinais, controle do bot

Atualizacao automatica via SocketIO a cada 5 segundos.

---

## Parametros Configuraveis e Valores Padrao

### Parâmetros globais

| Parametro | Padrao | Descricao |
|-----------|--------|-----------|
| use_testnet | true | Usar testnet |
| selected_exchange | lighter | Exchange ativa ("lighter" ou "hyperliquid") — determina qual manager de candles é instanciado |
| use_lighter_ws_candles | true | Quando true e selected_exchange=="lighter", usa LighterCandleManager (nativo WS); caso contrário usa BinanceCandleManager |
| monitored_assets | ["BTC","ETH","SOL"] | Ativos monitorados |
| sizing_mode | risk_pct | "risk_pct" (size = account_value × risk_pct%) ou "fixed" (size = fixed_trade_size_usd) |
| risk_pct_per_trade | 1.0 | % do capital por trade (usado quando sizing_mode=risk_pct) |
| fixed_trade_size_usd | 0 | Valor fixo em USD por trade (usado quando sizing_mode=fixed). Em ambos os modos, RiskManager bloqueia se `account_value < size_usd / get_max_leverage(asset)` |
| max_positions | 2 | Max posicoes simultaneas |
| max_daily_loss_pct | 5.0 | Max perda diaria (% capital) |
| slippage | 0.005 | Slippage para market orders |
| debug_logging | false | Ativar logs DEBUG |
| fee_rate_round_trip | 0.0009 | Fee round trip (taker 0.045% × 2 lados) para filtro de viabilidade |

### Parâmetros por estratégia

Cada estratégia tem duas chaves no config table:
- `strategy.<name>.enabled` — `"true"` ou `"false"`
- `strategy.<name>.params` — JSON com os parâmetros da estratégia

**bb_reversion_btc / bb_reversion_eth / bb_reversion_sol**:

| Parametro | Descricao |
|-----------|-----------|
| bb_period | Periodo das Bollinger Bands |
| bb_std | Desvio padrao das BB |
| ema_period | Periodo da EMA de tendencia |
| rsi_long_max | RSI maximo para entrada LONG |
| rsi_short_min | RSI minimo para entrada SHORT |
| tp_pct | TP em % do entry |
| sl_pct | SL em % do entry |
| bb_mid_exit | Sair ao cruzar a BB midline antes do TP/SL |
| bbp_long_threshold | BBP abaixo deste valor dispara entrada LONG |
| bbp_short_threshold | BBP acima deste valor dispara entrada SHORT |
| assets | Lista de ativos desta instância (ex: ["BTC"]) |

---

## Erros e Aprendizados — Salvar na Memória

Ao final de qualquer sessão onde um erro ocorreu, uma decisão técnica foi tomada, ou um comportamento inesperado foi descoberto:

1. Criar (ou atualizar) um arquivo `.md` em `C:\Users\User\.claude\projects\C--Users-User-Documents-Vibe-Code-RazorHL\memory\` com a lição aprendida.
2. Adicionar (ou atualizar) o ponteiro correspondente em `MEMORY.md` (o índice do diretório acima).

Formato dos arquivos de memória:
```
---
name: feedback_<slug>
description: Uma linha descrevendo o problema/regra
metadata:
  type: feedback
---

<Descrição do problema e da solução>

**Why:** <Por que esse erro aconteceu>

**How to apply:** <Quando e como aplicar essa regra no futuro>
```

Arquivos de memória existentes relevantes para este projeto:
- `feedback_lighter_symbols.md` — símbolos Lighter usam nome bare (XAU, não XAU-USD)
- `feedback_binance_symbol_map.md` — base.py e binance_ws.py têm mapas separados; manter sincronizados
- `feedback_cotrigger_assets.md` — WTI e HYPE sem par Binance Spot; usar co-trigger
- `feedback_strategy_enabled_persist.md` — nunca usar `get_config(key) or "default"` para estados que o usuário altera
- `feedback_dashboard_render.md` — renderParamFields não trata arrays; criar função dedicada por tipo
- `feedback_bb_mid_exit.md` — sempre declarar bb_mid_exit explicitamente nos extra_defaults de BBStoch
- `feedback_js_syntax.md` — nunca usar sintaxe Python em blocos JavaScript de HTML

---

## Troubleshooting

### "Bot not configured"
Acesse `http://localhost:8080/config` e insira suas credenciais (Account Address + Secret Key).

### Erro de conexao com a Hyperliquid
- Verifique se as credenciais estao corretas
- No testnet, confirme que a conta tem USDC depositado
- Verifique sua conexao de internet
- O bot tenta reconectar automaticamente

### Ordens nao estao sendo executadas
- Verifique se o bot esta com status "Rodando" no dashboard
- Confira se os ativos monitorados estao corretos
- Veja os logs para sinais bloqueados (risco, funding, posicoes no maximo)
- Confirme que tem saldo USDC suficiente

### Dashboard nao carrega
- Verifique se `run.py` esta rodando
- Confirme que a porta 8080 esta livre
- Instale o eventlet: `pip install eventlet`

### Indicadores retornando None
- O bot precisa de pelo menos 22 candles de 5m e 21 candles de 1m para calcular os indicadores
- Isso e normal nos primeiros minutos apos iniciar

### Erros de permissao SQLite
- Verifique se o diretorio do projeto tem permissao de escrita
- Feche outras instancias do bot que possam estar usando o mesmo banco

### Logs do bot não aparecem no PM2
`pm2 logs razorhl` mostra apenas o Flask (HTTP requests e startup). Os logs de candles, estratégias, sinais e trades vão para o SQLite e aparecem em `http://localhost:8080/logs` e em `logs/bot_YYYY-MM-DD.log`. Para depurar o loop do bot, usar sempre o dashboard ou o arquivo de log diário.

### Onde editar o cliente da Hyperliquid
O cliente ativo é `bot/exchanges/hyperliquid.py`, instanciado via `bot/exchanges/factory.py`. Correções em fills, funding ou ordens devem ser feitas ali. O arquivo `bot/ws_client.py` foi removido em 2026-05-13.

### Fonte de candles (live)
- **Lighter (WS nativo — default)**: implementado em `bot/exchanges/lighter_ws.py::LighterCandleManager`. Conecta em `wss://mainnet.zklighter.elliot.ai/stream` e subscreve `candle/{market_id}/{resolution}` para cada par `(asset, tf)` ativo. O servidor empurra updates a cada ~500ms enquanto a vela está aberta; detecção de candle close vem da mudança do campo `t` entre updates (com dedup via `_last_emitted_t` por canal — check-and-set atômico protegido por `_emit_lock` em AMBOS os caminhos: `_on_message` (WS push) e `_check_boundary_fallback` (REST fallback). Sem o lock, as duas threads podiam ler o mesmo `last_emitted` e cada uma enfileirar o mesmo close, causando `process_asset` duplicado → trades duplicados no banco. O lock é tomado antes do REST roundtrip no boundary fallback para reservar o direito de emitir antes de qualquer trabalho). Threading: ws + worker (ThreadPoolExecutor 16) + watchdog (reconnect se >90s silente) + boundary (timer per-TF que dispara REST fallback 2s após cada boundary para canais silentes — cobre ativos de baixíssimo volume e WS instável). Buffer compartilhado com `LighterExchangeClient._candle_buffer` via `_candle_buffer_lock` (manager escreve, `get_candles` lê); o filtro `_drop_open_candle` continua sendo aplicado pelo `get_candles` como defesa em profundidade. Latência típica live: <2s do close real ao callback (antes era 15-30s no caminho REST-based). **Keepalive crítico**: o servidor exige que o cliente envie um frame a cada <2min ou fecha a conexão com code=1000. WebSocket control PING frames do `websocket-client` (`ping_interval=N`) NÃO contam — Lighter ignora. Solução: thread dedicada `_ping_thread` que envia `{"type":"ping"}` JSON a cada 60s via `self._ws.send(...)`. `_on_message` também trata pings server-initiated (formato `{"type":"ping"}`) respondendo `{"type":"pong"}` — defensivo, caso a Lighter mude o modelo. **Rate limit de subscribe**: a Lighter retorna erro 30010 "Too Many Inflight Messages" se mais de ~50 subscribes chegam de uma vez; `_on_open` envia com `time.sleep(0.02)` entre cada um. Bug detectado em 2026-05-24: WS reconectava a cada 2min, todos os triggers vinham do boundary fallback sequencial (~31s de spread para 22 ativos); fixes foram (a) handler de ping → pong, (b) rate-limit dos subscribes, (c) thread de ping JSON client-initiated.
- **Lighter (REST fallback)**: `LighterExchangeClient.get_candles()` chama `/api/v1/candles` direto. Usado em (a) cold start / seed do buffer no `start()` e em `update_assets()`, (b) boundary fallback: `_BOUNDARY_MARGIN_MS=5000` (5s) após o boundary, dispara REST + emit para QUALQUER `(asset, tf)` cujo close ainda não foi emitido via WS (`_last_emitted_t`). Não tenta gating por "WS healthy do canal" — testamos com `_channel_last_msg_ts` (skip se canal teve msg <10s) e ativos low-volume com updates esparsos do candle antigo passavam pelo skip mas só recebiam new-t do WS em 30-70s (caso PUMP @ +58s, WLD @ +67s — inaceitável para scalping). Custo: ~10-15 REST extras por boundary; benefício: latência ~5s flat para todos os ativos. (c) read direto quando manager está offline. **Fast path WS**: se `_candle_buffer_fresh_ts[(asset,tf)]` foi atualizado nos últimos `_CANDLE_WS_FRESH_S=60s` pelo manager, `get_candles` serve direto do buffer (após `_drop_open_candle`) sem REST — elimina N_tf × ~1.5s de REST sequencial por ativo no callback de candle close (era o gargalo dominante depois que o WS começou a funcionar). Em caso de erro ou market não encontrado, retorna `pd.DataFrame()` vazio — sem fallback Binance (removido na migração do WS Lighter). `main.py` tem retry stale de 360s que cobre falhas transitórias.
- **Hyperliquid**: candles via Binance Spot REST (`api.binance.com/api/v3/klines`) via `fetch_binance_candles()` em `bot/exchanges/base.py` (delegate do `HyperliquidExchangeClient.get_candles()`). `BinanceCandleManager` continua sendo o feed live quando `selected_exchange="hyperliquid"`. **TODO quando voltar a usar HL**: migrar para candles nativos do SDK Hyperliquid (`info.candles_snapshot`) para alinhar com o padrão de WS nativo da Lighter.
- **Rollback**: `use_lighter_ws_candles=false` força o `BinanceCandleManager` mesmo com `selected_exchange="lighter"` (modo legado para casos de instabilidade do WS Lighter).
- O backtest usa candles da **Lighter REST**: `bot/backtest/csv_loader.py::_fetch_lighter_candles_since` e `_update_csv` populam os CSVs em `candles/` via `/api/v1/candles` (`lighter_get`); engine/scanner leem esses CSVs. Não há mais dependência da Binance no caminho do backtest.
