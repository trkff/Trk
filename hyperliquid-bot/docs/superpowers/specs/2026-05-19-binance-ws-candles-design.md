# Design: Binance WebSocket Candle Manager

**Data:** 2026-05-19  
**Contexto:** RazorHL — scalping bot na Hyperliquid  
**Problema:** REST polling a cada 10s causa até 10s de atraso na detecção de fechamento de candle, resultando em entry price divergindo do close da Binance (ex: entry 76,805 vs close 76,789).  
**Solução:** Substituir polling por WebSocket event-driven com `x: true` como trigger exato de fechamento.

---

## Motivação

Os backtests que validaram as estratégias (BB Reversion, BB Stoch) usam candles da Binance. O bot live usava candles da Hyperliquid — divergência de fonte. Após alinhar a fonte (Binance REST em ambos), o problema residual é o atraso de polling: o sinal é avaliado segundos após o fechamento real, com o preço já movido.

WebSocket resolve os dois problemas: fonte idêntica ao backtest + timing exato no fechamento.

---

## Arquitetura

### Novo módulo: `bot/exchanges/binance_ws.py`

Classe `BinanceCandleManager` com três threads internas:

```
WS Thread  →  queue.Queue  →  Worker Thread → process_asset()
    ↓
buffer.update(candle)  [RLock]
```

**WS Thread**
- Conecta ao Binance combined stream
- Ao receber evento com `x: true`: atualiza `buffer[asset][interval]` + faz `queue.put((asset, interval))`
- Nunca chama `process_asset` diretamente — não bloqueia o recv loop

**Worker Thread**
- Consome `queue.Queue` em loop
- Filtra: só processa eventos de `interval == "5m"` (único trigger de estratégias)
- Chama `process_asset(asset, ...)` serializado

**Watchdog Thread**
- Verifica `last_event_ts` a cada 30s
- Se nenhum evento em 90s: dispara reconexão
- Reconexão: re-seed REST com overlap → fecha WS → reabre WS

### Buffer

```python
_buffer: dict[str, dict[str, pd.DataFrame]]  # [asset][interval]
_lock: threading.RLock
```

- `get_candles(asset, interval, count)` lê do buffer (último `count` candles via `iloc[-count:]`)
- Fallback: se buffer vazio, busca via REST antes de retornar

### Timeframes subscritos via WS

| Timeframe | Subscreve WS | Trigger process_asset |
|-----------|-------------|----------------------|
| 1m        | Não         | Não (fetch REST pontual) |
| 5m        | Sim         | **Sim** |
| 15m       | Sim         | Não |
| 1h        | Sim         | Não |
| 4h        | Sim         | Não |
| 1d        | Sim         | Não |

1m continua sendo buscado via REST pontualmente quando `process_asset` é invocado (100 candles, ~50ms, não é trigger).

### Reconnect com overlap

```python
def _reseed_with_overlap(self, asset: str, interval: str):
    fresh = fetch_binance_candles(asset, interval, count=50)
    with self._lock:
        existing = self._buffer[asset][interval]
        merged = pd.concat([existing, fresh])
        merged = merged[~merged.index.duplicated(keep='last')]
        self._buffer[asset][interval] = merged.sort_index()
```

Re-seed roda para todos os assets × intervals antes de reabrir o stream.

### Seed inicial

Counts completos antes de abrir qualquer stream:

| Interval | count | Motivo |
|----------|-------|--------|
| 5m       | 500   | EMA(200) precisa de 200+ candles de aquecimento; 210 era insuficiente |
| 15m      | 300   | margem adequada para indicadores de médio prazo |
| 1h       | 300   | OK |
| 4h       | 300   | OK |
| 1d       | 300   | OK |

---

## Mudanças no `main.py`

### Inicialização

```python
from bot.exchanges.binance_ws import BinanceCandleManager

candle_mgr = BinanceCandleManager(assets, on_candle_close=process_asset_wrapper)
candle_mgr.start()  # seed REST + abre WS
```

### Loop principal

O loop de 10s vira watchdog/heartbeat de 30s. Responsabilidades que ficam no loop:
- Checar `bot_status` (paused/stopped)
- `risk_mgr.check_open_positions_tp_sl()`
- Heartbeat de log
- Toggle debug_logging

`process_asset` **não é mais chamado pelo loop** — só pelo worker thread do manager.

### Warm-up de timestamps

O warm-up atual (seed de `last_1m_ts`, `last_5m_ts`, etc.) é substituído pelo seed do buffer do manager. Os dicts `last_*_ts` são eliminados — a detecção de "candle novo" passa a ser o próprio evento WS `x: true`.

### Pausa/stop

`candle_mgr.stop()` chamado quando `bot_status == "stopped"`. Em pausa, o manager continua atualizando o buffer mas o worker thread não chama `process_asset`.

---

## Dependências

Adicionar ao `requirements.txt`:
```
websocket-client>=1.6.0
```

Biblioteca síncrona/threaded — compatível com a arquitetura atual sem introduzir asyncio.

---

## Interface pública de `BinanceCandleManager`

```python
class BinanceCandleManager:
    def __init__(self, assets: list[str], on_candle_close: Callable): ...
    def start(self) -> None: ...       # seed REST + abre WS
    def stop(self) -> None: ...        # fecha WS + threads
    def pause(self) -> None: ...       # seta _paused=True; worker para de consumir fila
    def resume(self) -> None: ...      # drena fila de eventos acumulados, depois seta _paused=False
    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame: ...
    def update_assets(self, assets: list[str]) -> None: ...  # reconecta com nova lista
```

**Drain no resume — obrigatório:**
```python
def resume(self) -> None:
    while not self._queue.empty():
        try: self._queue.get_nowait()
        except: break
    self._paused = False
```
Sem o drain, o worker consumiria eventos acumulados durante a pausa e dispararia `process_asset` para candles já fechados há minutos (stale signals).

**Queue com teto de tamanho:**
```python
self._queue = queue.Queue(maxsize=50)

# Na WS thread:
try:
    self._queue.put_nowait((asset, interval))
except queue.Full:
    pass  # descarta — próximo candle chegará em breve
```
Se `process_asset` ficar lento, a fila não cresce sem bound. Para estratégias de candle, descartar um evento é melhor que acumular centenas deles.

---

## Tratamento de erros

| Cenário | Comportamento |
|---------|---------------|
| WS cai (rede) | Watchdog detecta em ≤90s, reconecta com re-seed |
| Binance REST falha no seed | Log warning, retry 3× com backoff exponencial |
| Binance REST falha no re-seed | Usa buffer existente, tenta WS mesmo assim |
| `process_asset` lança exceção | Worker loga erro, continua consumindo fila |
| Asset adicionado no dashboard | `update_assets()` semeia novo asset + reconecta stream |

---

## O que NÃO muda

- Estratégias (`bb_reversion.py`, `bb_stoch.py`) — sem alterações
- Executor, risk manager — sem alterações  
- Dashboard, Flask, SocketIO — sem alterações
- Banco SQLite — sem alterações
- `fetch_binance_candles()` em `base.py` — continua sendo usado para seed/fallback
