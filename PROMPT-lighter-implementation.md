# Prompt: Implementação da Lighter Exchange no RazorHL

---

## Objetivo

Adicionar a **Lighter Exchange** (taxa zero) como exchange selecionável no RazorHL, criando antes uma **camada de abstração modular** para que HyperLiquid e Lighter possam coexistir e ser selecionadas via config no dashboard. A implementação da Lighter deve ser uma **tradução guiada** de uma implementação TypeScript já battle-tested em produção (YieldShield), portando todos os learnings e gotchas descobertos.

---

## Contexto do Projeto

**RazorHL** é um bot Python de trading em perpétuos na HyperLiquid. Stack: Python 3.10+, Flask+SocketIO, SQLite, pandas-ta, hyperliquid-python-sdk.

Arquitetura atual relevante:
```
hyperliquid-bot/
  bot/
    ws_client.py      ← HLClient: conexão, candles, funding, posições, fills
    executor.py       ← open_position(), close_position(), _place_tp_sl() — usa HLClient direto
    risk.py           ← RiskManager — usa HLClient para saldo e posições
    strategies/       ← estratégias — NÃO tocam exchange diretamente
  main.py             ← loop principal — instancia HLClient, passa para executor/risk
  dashboard/app.py    ← Flask dashboard
  bot/db.py           ← SQLite config (chave/valor)
```

O problema atual: `executor.py` e `risk.py` importam `HLClient` diretamente via tipo hard-coded. Não há abstração — trocar exchange exige mexer em múltiplos arquivos.

---

## Arquitetura Alvo

```
bot/
  exchanges/
    __init__.py
    base.py           ← BaseExchangeClient ABC — contrato que todos implementam
    hyperliquid.py    ← HLClient atual movido aqui (renomeado HyperliquidClient)
    lighter.py        ← Novo cliente Lighter implementando o mesmo ABC
    factory.py        ← ExchangeFactory: lê config do SQLite e retorna cliente correto
  ws_client.py        ← MANTIDO por compatibilidade, apenas re-exporta HyperliquidClient
  executor.py         ← usa BaseExchangeClient (não mais HLClient diretamente)
  risk.py             ← usa BaseExchangeClient
main.py               ← usa ExchangeFactory para instanciar o cliente correto
```

---

## Fase 1 — Abstraction Layer (NÃO quebra nada existente)

### 1.1 Criar `bot/exchanges/base.py`

Definir o ABC com exatamente os métodos que `executor.py` e `risk.py` usam hoje:

```python
from abc import ABC, abstractmethod
import pandas as pd

class BaseExchangeClient(ABC):

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame: ...
    # Retorna DataFrame com colunas: timestamp, open, high, low, close, volume
    # Index: datetime (UTC)

    @abstractmethod
    def get_funding_rate(self, asset: str) -> float: ...

    @abstractmethod
    def get_account_value(self) -> float: ...

    @abstractmethod
    def get_open_positions(self) -> list[dict]: ...
    # Cada dict: {coin, size, side, entry_price, unrealized_pnl, notional}

    @abstractmethod
    def get_mid_price(self, asset: str) -> float: ...

    @abstractmethod
    def get_recent_fills(self, asset: str, since_ms: int) -> list[dict]: ...

    @abstractmethod
    def get_asset_sz_decimals(self, asset: str) -> int: ...

    @abstractmethod
    def market_open(self, asset: str, is_buy: bool, size: float, slippage: float = 0.005) -> dict: ...
    # Retorna: {filled: bool, avg_px: float, total_sz: float, oid: str}

    @abstractmethod
    def market_close(self, asset: str) -> dict: ...
    # Retorna: {filled: bool, avg_px: float, total_sz: float, oid: str}

    @abstractmethod
    def place_tp_sl(self, asset: str, is_buy_to_close: bool, size: float,
                    tp_price: float, sl_price: float, sz_decimals: int) -> None: ...

    @abstractmethod
    def check_position_exists(self, asset: str) -> bool: ...

    @property
    @abstractmethod
    def address(self) -> str: ...
```

### 1.2 Criar `bot/exchanges/hyperliquid.py`

Mover o conteúdo de `bot/ws_client.py` para cá. Renomear a classe de `HLClient` para `HyperliquidClient`. Fazer `HyperliquidClient` herdar de `BaseExchangeClient` e implementar todos os métodos abstratos.

Os métodos `market_open`, `market_close` e `place_tp_sl` precisam ser extraídos de `executor.py` e movidos para cá como métodos da classe (não funções soltas). Isso torna o executor stateless em relação à exchange.

### 1.3 Criar `bot/exchanges/factory.py`

```python
from bot import db
from bot.exchanges.base import BaseExchangeClient

def create_exchange_client() -> BaseExchangeClient:
    cfg = db.get_all_config()
    selected = cfg.get("selected_exchange", "hyperliquid")

    if selected == "lighter":
        from bot.exchanges.lighter import LighterClient
        return LighterClient()
    else:
        from bot.exchanges.hyperliquid import HyperliquidClient
        return HyperliquidClient()
```

### 1.4 Criar `bot/exchanges/__init__.py`

Vazio ou com re-export de `BaseExchangeClient`.

### 1.5 Atualizar `bot/ws_client.py`

Manter o arquivo mas torná-lo apenas um re-export para compatibilidade:
```python
# Backward compat — use bot.exchanges.hyperliquid.HyperliquidClient directly
from bot.exchanges.hyperliquid import HyperliquidClient as HLClient
__all__ = ["HLClient"]
```

### 1.6 Atualizar `bot/executor.py`

Trocar o tipo `HLClient` por `BaseExchangeClient` em todas as assinaturas. As funções `open_position`, `close_position`, `check_position_exists` recebem `client: BaseExchangeClient`. A lógica de ordens (market_open, place_tp_sl, market_close) agora chama `client.market_open(...)` etc. em vez de `client.exchange.market_open(...)`.

### 1.7 Atualizar `bot/risk.py`

Trocar o tipo `HLClient` por `BaseExchangeClient` no `RiskManager.__init__` e em todos os métodos.

### 1.8 Atualizar `main.py`

Substituir a instanciação direta de `HLClient()` por `ExchangeFactory.create_exchange_client()`.

### Critério de sucesso da Fase 1
Bot funciona identicamente ao atual. Nenhuma estratégia ou lógica de risco foi alterada. Apenas a plumbing de instanciação mudou. Rodar o bot no testnet e confirmar que sinais, ordens e risk checks funcionam normalmente.

---

## Fase 2 — Implementação do `bot/exchanges/lighter.py`

Esta é uma **tradução Python** de uma implementação TypeScript battle-tested. A lógica deve ser fiel ao original — não reimplementar do zero.

### 2.1 Dependências

```bash
pip install lighter-sdk
```

O `lighter-sdk` embute o binário Go compilado (`lighter-signer`) para assinatura de transações. É o mesmo binário que o YieldShield usa via FFI em Node.js.

### 2.2 Constantes e tipos

```python
# Order types (da spec oficial da Lighter)
ORDER_TYPE_LIMIT           = 0
ORDER_TYPE_MARKET          = 1
ORDER_TYPE_STOP_LOSS       = 2
ORDER_TYPE_STOP_LOSS_LIMIT = 3
ORDER_TYPE_TAKE_PROFIT     = 4
ORDER_TYPE_TAKE_PROFIT_LIMIT = 5

# Time in force
TIF_IOC           = 0
TIF_GOOD_TILL_TIME = 1

LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai"
SLIPPAGE = 0.005
FILL_WAIT_SEC = 2.5
MIN_GET_GAP_SEC = 0.3   # rate limit: 300ms mínimo entre GETs
```

### 2.3 HTTP Layer — CRÍTICO: rate limiting + WAF detection

**Este é o gotcha mais importante.** A Lighter tem AWS WAF que bloqueia requisições em rajada. O YieldShield descobriu isso em produção.

```python
import threading
import time
import requests
from bot.logger import get_logger

log = get_logger("lighter.http")

# Serializa todos os GETs com gap mínimo de 300ms
_get_lock = threading.Lock()
_last_get_ts = 0.0

WAF_BODY_MARKERS = ["awsWafCookieDomainList", "captcha.awswaf.com", "challenge.js"]
RATE_LIMIT_BACKOFFS = [0.5, 1.0, 2.0]  # segundos

def _is_waf_blocked(text: str, content_type: str) -> bool:
    if "text/html" in content_type:
        return True
    lower = text.lower()
    if lower.startswith("<!doctype html") or lower.startswith("<html"):
        return True
    return any(m in text for m in WAF_BODY_MARKERS)

def lighter_get(user_label: str, path: str) -> dict:
    global _last_get_ts
    url = f"{LIGHTER_BASE_URL}{path}"

    # Serializa com gap mínimo de 300ms entre chamadas
    with _get_lock:
        now = time.monotonic()
        wait = MIN_GET_GAP_SEC - (now - _last_get_ts)
        if wait > 0:
            time.sleep(wait)
        _last_get_ts = time.monotonic()

    waf_retried = False
    while True:
        for attempt, backoff in enumerate(RATE_LIMIT_BACKOFFS):
            resp = requests.get(url, timeout=15)
            if resp.status_code == 429:
                if attempt < len(RATE_LIMIT_BACKOFFS) - 1:
                    log.warning(f"[{user_label}] 429 rate limited, retry in {backoff}s")
                    time.sleep(backoff)
                    continue
                raise Exception(f"Lighter rate limited (429) after retries: {path}")
            if resp.status_code == 405:
                if not waf_retried:
                    waf_retried = True
                    log.warning(f"[{user_label}] 405 WAF detected, waiting 60s: {path}")
                    time.sleep(60)
                    break  # retry the outer while
                raise Exception(f"Lighter WAF blocked (405) after retry: {path}")
            if not resp.ok:
                body = resp.text[:500]
                if _is_waf_blocked(body, resp.headers.get("content-type", "")):
                    raise Exception(f"Lighter WAF blocked ({resp.status_code}): {path}")
                raise Exception(f"Lighter API {resp.status_code}: {body}")
            ct = resp.headers.get("content-type", "")
            if _is_waf_blocked(resp.text[:200], ct):
                raise Exception(f"Lighter WAF blocked (HTML body): {path}")
            return resp.json()
        else:
            raise Exception(f"Lighter rate limited (429): {path}")

def lighter_post(user_label: str, path: str, data: dict) -> dict:
    # POST NÃO retenta — risco de double-submit se a tx já foi aceita
    url = f"{LIGHTER_BASE_URL}{path}"
    resp = requests.post(url, data=data, timeout=15)
    if resp.status_code == 429:
        raise Exception(f"Lighter POST rate limited (429): {path}")
    if not resp.ok:
        body = resp.text[:500]
        ct = resp.headers.get("content-type", "")
        if _is_waf_blocked(body, ct):
            raise Exception(f"Lighter WAF blocked POST ({resp.status_code}): {path}")
        raise Exception(f"Lighter POST {resp.status_code}: {body}")
    ct = resp.headers.get("content-type", "")
    if _is_waf_blocked(resp.text[:200], ct):
        raise Exception(f"Lighter WAF blocked POST (HTML): {path}")
    return resp.json()
```

### 2.4 Classe `LighterClient` — REST API wrapper

Implementar os métodos abaixo. Todos usam `lighter_get` / `lighter_post` do item 2.3.

```python
class LighterClient:
    MARKET_CACHE_TTL = 3600   # 1h
    FUNDING_CACHE_TTL = 300   # 5min
    ACCOUNT_CACHE_TTL = 5     # 5s

    def __init__(self, user_label: str):
        self.user_label = user_label
        self._market_cache: dict[str, dict] = {}       # symbol -> MarketInfo
        self._market_by_id: dict[int, dict] = {}       # marketId -> MarketInfo
        self._market_cache_ts = 0.0
        self._funding_cache: dict[int, float] = {}
        self._funding_cache_ts = 0.0
        self._account_cache: dict | None = None
        self._account_cache_ts = 0.0

    def discover_account_index(self, wallet_address: str) -> int:
        resp = lighter_get(self.user_label, f"/api/v1/accountsByL1Address?l1_address={wallet_address}")
        subs = resp.get("sub_accounts") or resp.get("accounts") or []
        if not subs:
            raise ValueError(f"Lighter account not found for {wallet_address}. Deposit at lighter.xyz first.")
        return subs[0]["index"]

    def discover_api_key_index(self, account_index: int, public_key: str) -> tuple[int, int]:
        # Retorna (api_key_index, nonce)
        resp = lighter_get(self.user_label, f"/api/v1/apikeys?account_index={account_index}")
        keys = resp.get("api_keys", [])
        pk = public_key.lower().lstrip("0x")
        for k in keys:
            if k["public_key"].lower() == pk:
                return k["api_key_index"], k["nonce"]
        raise ValueError(f"API key not registered on Lighter for account {account_index}.")

    def get_next_nonce(self, account_index: int, api_key_index: int) -> int:
        resp = lighter_get(self.user_label, f"/api/v1/nextNonce?account_index={account_index}&api_key_index={api_key_index}")
        return resp["nonce"]

    def load_markets(self) -> None:
        if time.time() - self._market_cache_ts < self.MARKET_CACHE_TTL and self._market_cache:
            return
        resp = lighter_get(self.user_label, "/api/v1/orderBookDetails?filter=perp")
        self._market_cache.clear()
        self._market_by_id.clear()
        for d in resp.get("order_book_details", []):
            if d.get("status") != "active":
                continue
            info = {
                "marketId": d["market_id"], "symbol": d["symbol"].upper(),
                "sizeDecimals": d["supported_size_decimals"],
                "priceDecimals": d["supported_price_decimals"],
                "minBaseAmount": float(d["min_base_amount"]),
                "minQuoteAmount": float(d["min_quote_amount"]),
                "lastTradePrice": float(d.get("last_trade_price") or 0),
            }
            self._market_cache[info["symbol"]] = info
            self._market_by_id[info["marketId"]] = info
        self._market_cache_ts = time.time()

    def get_market(self, symbol: str) -> dict | None:
        return self._market_cache.get(symbol.upper())

    def get_account(self, account_index: int, auth_token: str) -> dict:
        now = time.time()
        if self._account_cache and now - self._account_cache_ts < self.ACCOUNT_CACHE_TTL:
            return self._account_cache
        resp = lighter_get(self.user_label, f"/api/v1/account?by=index&value={account_index}&auth={auth_token}")
        acc = (resp.get("accounts") or [])[0]
        if not acc:
            raise ValueError(f"Lighter account {account_index} not found")
        result = {
            "index": acc["index"],
            "collateral": acc["collateral"],
            "availableBalance": acc["available_balance"],
            "positions": [
                {
                    "marketId": p["market_id"], "symbol": p["symbol"],
                    "position": p["position"], "avgEntryPrice": p["avg_entry_price"],
                    "positionValue": p["position_value"], "unrealizedPnl": p["unrealized_pnl"],
                    # CRÍTICO: position e positionValue são sempre POSITIVOS.
                    # Direção vem do campo 'sign': '-1' = Short, '1' = Long.
                    "sign": str(p.get("sign", "-1")),
                }
                for p in acc.get("positions", [])
            ],
        }
        self._account_cache = result
        self._account_cache_ts = now
        return result

    def invalidate_account_cache(self) -> None:
        self._account_cache = None
        self._account_cache_ts = 0.0

    def get_best_prices(self, market_id: int, auth_token: str) -> tuple[float, float]:
        resp = lighter_get(self.user_label, f"/api/v1/orderBookOrders?market_id={market_id}&limit=1&auth={auth_token}")
        best_bid = float(resp["bids"][0]["price"]) if resp.get("bids") else 0.0
        best_ask = float(resp["asks"][0]["price"]) if resp.get("asks") else 0.0
        return best_bid, best_ask

    def get_mark_price(self, market_id: int, auth_token: str) -> float:
        best_bid, best_ask = self.get_best_prices(market_id, auth_token)
        if best_ask > 0 and best_bid > 0:
            return (best_ask + best_bid) / 2
        market = self._market_by_id.get(market_id)
        return market["lastTradePrice"] if market else 0.0

    def get_funding_rate(self, market_id: int) -> float:
        if time.time() - self._funding_cache_ts < self.FUNDING_CACHE_TTL and self._funding_cache:
            return self._funding_cache.get(market_id, 0.0)
        resp = lighter_get(self.user_label, "/api/v1/funding-rates")
        self._funding_cache = {fr["market_id"]: fr["rate"] for fr in resp.get("funding_rates", [])}
        self._funding_cache_ts = time.time()
        return self._funding_cache.get(market_id, 0.0)

    def send_tx(self, tx_type: int, tx_info: str) -> dict:
        resp = lighter_post(self.user_label, "/api/v1/sendTx", {"tx_type": tx_type, "tx_info": tx_info})
        if resp.get("code") != 200:
            raise Exception(f"Lighter sendTx failed: {resp.get('message', resp)}")
        self.invalidate_account_cache()
        return {"txHash": resp["tx_hash"], "predictedMs": resp.get("predicted_execution_time_ms", 0)}

    def get_trades_page(self, account_index: int, market_id: int, auth_token: str,
                        limit: int = 50, cursor: str | None = None,
                        aggregate: bool = False, sort_dir: str = "desc") -> tuple[list[dict], str | None]:
        # CRÍTICO: NÃO usar offset — a Lighter ignora silenciosamente e sempre retorna página 0.
        # Usar cursor-based pagination.
        qs = [
            f"account_index={account_index}", f"market_id={market_id}",
            f"sort_by=timestamp", f"sort_dir={sort_dir}",
            f"limit={min(limit, 100)}", f"aggregate={'true' if aggregate else 'false'}",
            f"auth={auth_token}",
        ]
        if cursor:
            qs.append(f"cursor={cursor}")
        resp = lighter_get(self.user_label, f"/api/v1/trades?{'&'.join(qs)}")
        trades = [
            {
                "tradeId": t["trade_id"], "marketId": t["market_id"],
                "size": t["size"], "price": t["price"], "usdAmount": t["usd_amount"],
                "isMakerAsk": t["is_maker_ask"], "askAccountId": t["ask_account_id"],
                "bidAccountId": t["bid_account_id"],
                "askAccountPnl": t.get("ask_account_pnl", "0"),
                "bidAccountPnl": t.get("bid_account_pnl", "0"),
                "timestamp": t["timestamp"],
            }
            for t in resp.get("trades", [])
        ]
        next_cursor = resp.get("next_cursor") or None
        return trades, next_cursor
```

### 2.5 Signer — usando `lighter-sdk`

O `lighter-sdk` do PyPI encapsula o binário Go. Usar a classe `SignerClient` dele diretamente. Exemplo de inicialização:

```python
from lighter import SignerClient

signer = SignerClient(
    url="https://mainnet.zklighter.elliot.ai",
    api_private_keys={api_key_index: private_key},
    account_index=account_index,
)
```

Verificar na documentação do `lighter-sdk` os métodos equivalentes a:
- `create_order(market_index, client_order_index, base_amount, price, is_ask, order_type, time_in_force, reduce_only, trigger_price, nonce, api_key_index)` → para MARKET, STOP_LOSS, TAKE_PROFIT
- `cancel_order(market_index, order_index, nonce, api_key_index)`
- `create_auth_token(deadline_seconds)` → retorna string do token de autenticação

Se a API do SDK divergir dos parâmetros acima, seguir o que o SDK expõe — não tentar chamar o binário diretamente.

### 2.6 Classe `LighterExchangeClient(BaseExchangeClient)`

Implementar o ABC completo. Pontos críticos:

**Auth token:** Expira em 1h. Renovar proativamente aos 50 minutos.
```python
def _ensure_auth_token(self) -> str:
    if time.time() > self._auth_token_expiry:
        self._auth_token = self._signer.create_auth_token(3600)
        self._auth_token_expiry = time.time() + 50 * 60
    return self._auth_token
```

**`get_open_positions()`:** Usar o campo `sign` para determinar lado — NÃO o sinal do tamanho.
```python
sign = int(pos.get("sign", "-1"))
side = "long" if sign == 1 else "short"
# position e positionValue são sempre positivos — usar abs() defensivamente
size = abs(float(pos["position"]))
```

**`market_open()` e `market_close()` — Nonce desync recovery:**
```python
try:
    tx = self._signer.create_order(...)
    self._client.send_tx(tx.tx_type, tx.tx_info)
except Exception as e:
    if "invalid nonce" not in str(e).lower():
        raise
    # Resync nonce e retenta UMA vez
    log.warning(f"[{asset}] Nonce desync, resyncing...")
    fresh_nonce = self._client.get_next_nonce(self._account_index, self._api_key_index)
    self._signer.reset_nonce(fresh_nonce)  # ou equivalente no SDK
    tx = self._signer.create_order(...)  # novo client_order_index
    self._client.send_tx(tx.tx_type, tx.tx_info)
```

**`market_close()` — Loop de confirmação:**
```python
# Após enviar close, aguardar e confirmar que posição zerou
time.sleep(FILL_WAIT_SEC)
after = self.get_open_positions()
remaining = next((p for p in after if p["coin"] == asset), None)
for attempt in range(2):
    if not remaining or remaining["size"] == 0:
        break
    log.warning(f"[{asset}] Close unconfirmed, retry {attempt+1}")
    time.sleep(1.5)
    after = self.get_open_positions()
    remaining = next((p for p in after if p["coin"] == asset), None)
if remaining and remaining["size"] > 0:
    raise Exception(f"Lighter close did not fill for {asset}: {remaining['size']} remaining")
```

**`place_tp_sl()` — TP/SL nativos da Lighter:**
```python
# TP order — ORDER_TYPE_TAKE_PROFIT (4)
# Além do trigger_price, passar price como slippage máximo aceitável (igual a taker)
tp_tx = self._signer.create_order(
    market_index=market.marketId,
    client_order_index=next_client_order_index(),
    base_amount=size_to_int(size, market["sizeDecimals"]),
    price=price_to_int(tp_price * (1 - SLIPPAGE) if is_buy_to_close else tp_price * (1 + SLIPPAGE), market["priceDecimals"]),
    is_ask=is_buy_to_close,
    order_type=ORDER_TYPE_TAKE_PROFIT,
    time_in_force=TIF_IOC,
    reduce_only=True,
    trigger_price=price_to_int(tp_price, market["priceDecimals"]),
)
self._client.send_tx(tp_tx.tx_type, tp_tx.tx_info)

# SL order — ORDER_TYPE_STOP_LOSS (2) — mesma lógica
sl_tx = self._signer.create_order(
    ...
    order_type=ORDER_TYPE_STOP_LOSS,
    trigger_price=price_to_int(sl_price, market["priceDecimals"]),
)
self._client.send_tx(sl_tx.tx_type, sl_tx.tx_info)
```

**`get_candles()`:** A Lighter não tem endpoint nativo de candles históricos (é um orderbook DEX). Usar uma fonte externa de preço para candles — **CoinGecko API gratuita** ou **Binance REST API** (que tem candles públicos sem autenticação para pares spot como BTCUSDT). O método deve buscar candles pelo símbolo mapeando para o par correto. Se nenhuma fonte estiver disponível, levantar `NotImplementedError` com mensagem clara — não silenciar.

**`get_recent_fills()`:** Usar `get_trades_page` com `aggregate=False`. Mapear fills para o formato que o `executor.py` espera: `{oid, fee, closedPnl}`. Fee na Lighter é sempre 0 (taxa zero). PnL: `bidAccountPnl` se o account é buyer, `askAccountPnl` se é seller.

**`get_asset_sz_decimals()`:** Retornar `market["sizeDecimals"]` do cache de mercados.

**`get_funding_rate()`:** Usar `LighterClient.get_funding_rate(market_id)`.

**Helpers de conversão (idênticos ao YieldShield):**
```python
def price_to_int(price: float, decimals: int) -> int:
    return round(price * 10 ** decimals)

def size_to_int(size: float, decimals: int) -> int:
    return round(size * 10 ** decimals)
```

### 2.7 Inicialização lazy (`_ensure_init`)

A conexão com a Lighter exige chamadas à API para descobrir `account_index` e `api_key_index`. Isso deve acontecer de forma lazy na primeira chamada — não no `__init__`. Padrão:

```python
def _ensure_init(self) -> None:
    if self._initialized:
        return
    self._account_index = self._client.discover_account_index(self._wallet_address)
    api_key_index, nonce = self._client.discover_api_key_index(self._account_index, self._public_key)
    self._api_key_index = api_key_index
    self._signer = SignerClient(...)  # inicializar com nonce
    self._client.load_markets()
    self._auth_token = self._signer.create_auth_token(3600)
    self._auth_token_expiry = time.time() + 50 * 60
    self._initialized = True
```

### Critério de sucesso da Fase 2

- `LighterExchangeClient` instancia sem erros com credenciais válidas
- `get_account_value()` retorna saldo real da conta Lighter
- `get_open_positions()` retorna posições com `side` correto (long/short via `sign`)
- `market_open()` + `place_tp_sl()` abre posição e coloca TP/SL no testnet da Lighter
- `market_close()` fecha posição e confirma que zerou
- WAF blocking gera log de warning claro em vez de exception opaca
- Rate limiting não causa 429 em cascata com múltiplas chamadas seguidas

---

## Fase 3 — Dashboard e Config

### 3.1 Adicionar `selected_exchange` ao SQLite

No `bot/db.py`, garantir que `selected_exchange` tem default `"hyperliquid"` na inicialização do banco.

### 3.2 Adicionar credenciais da Lighter ao config

Novas chaves no SQLite para a Lighter:
- `lighter_wallet_address` — endereço Ethereum (l1_address)
- `lighter_public_key` — chave pública da API key registrada na Lighter
- `lighter_private_key` — chave privada da API key (para assinar transações)

**NUNCA** armazenar em plaintext se o projeto já tem criptografia de credenciais. Se o projeto não tem, pelo menos não logar essas chaves.

### 3.3 Atualizar a tela de Configurações do dashboard

Na aba de Configurações (`dashboard/templates/config.html`):
- Adicionar dropdown **"Exchange"** com opções: `HyperLiquid` e `Lighter`
- Quando `Lighter` for selecionado, exibir (via JS) os campos: Wallet Address, Public Key, Private Key
- Quando `HyperLiquid` for selecionado, manter os campos atuais

### 3.4 Endpoint de salvamento

O endpoint que salva o config deve persistir `selected_exchange` e as credenciais da Lighter no SQLite. Ao salvar, recriar o cliente de exchange via `factory.py`.

---

## Gotchas Críticos de Produção (NÃO ignorar)

Estes foram descobertos em produção no YieldShield — ignorá-los causará bugs silenciosos:

1. **WAF da AWS** — A Lighter tem bot protection. Respostas 405 e corpo HTML são bloqueios WAF, não erros reais. Tratar como descrito na Fase 2.3.

2. **Rate limit com serializer global** — O rate limit da Lighter é por IP, não por conta. Com múltiplas operações concorrentes (ex: strategy loop + riskmanager polling), sem o gap de 300ms entre GETs, você vai receber 429 em cascata que bloqueiam detecção de fills e cancelamento de ordens.

3. **Offset de paginação está quebrado** — O parâmetro `offset` da API `/api/v1/trades` é **silenciosamente ignorado** pela Lighter — sempre retorna a página 0. A única paginação que funciona é cursor-based (`cursor` param). Sem isso, fills ficam truncados em 50 registros.

4. **Campo `sign` para direção** — `position` e `positionValue` na API são sempre positivos (valor absoluto). A direção (Long/Short) está no campo `sign`: `"1"` = Long, `"-1"` = Short. Não usar o sinal do número.

5. **Nonce desync** — Se o processo reinicia ou ocorre timeout de rede durante uma tx, o nonce local fica dessincronizado. Implementar o recovery: catch "invalid nonce" + re-fetch + retry.

6. **Loop de confirmação no close** — O close pode aceitar a transação (tx_hash) mas ainda demorar 2-3s para aparecer zerado via get_account. Aguardar 2.5s e verificar em loop antes de declarar sucesso.

7. **`aggregate=false` para PnL** — `aggregate=true` diverge ~0.6% do PnL real por arredondamento server-side. Usar `aggregate=false` (raw fills) para cálculos de PnL precisos.

---

## Estrutura de Arquivos Final

```
hyperliquid-bot/
  bot/
    exchanges/
      __init__.py
      base.py              ← NOVO: BaseExchangeClient ABC
      hyperliquid.py       ← MOVIDO de ws_client.py + HyperliquidClient
      lighter.py           ← NOVO: LighterExchangeClient
      factory.py           ← NOVO: ExchangeFactory
    ws_client.py           ← MANTIDO: re-export de HyperliquidClient
    executor.py            ← ATUALIZADO: usa BaseExchangeClient
    risk.py                ← ATUALIZADO: usa BaseExchangeClient
    db.py                  ← ATUALIZADO: novas config keys
    strategies/            ← NÃO TOCAR
    indicators.py          ← NÃO TOCAR
    signals.py             ← NÃO TOCAR
  main.py                  ← ATUALIZADO: usa factory
  dashboard/
    templates/config.html  ← ATUALIZADO: dropdown de exchange + campos Lighter
    app.py                 ← ATUALIZADO: novo endpoint save config
```

---

## Ordem de Implementação Recomendada

1. `bot/exchanges/base.py` — define o contrato
2. `bot/exchanges/hyperliquid.py` — move HLClient, faz herdar de BaseExchangeClient
3. `bot/ws_client.py` — torna re-export
4. `bot/exchanges/factory.py` — factory simples
5. `bot/executor.py` e `bot/risk.py` — trocar tipos, verificar que bot ainda funciona
6. `main.py` — usar factory
7. **Testar Fase 1 no testnet antes de continuar**
8. `bot/exchanges/lighter.py` — implementar LighterExchangeClient completo
9. Dashboard — dropdown + credenciais
10. **Testar Fase 2 no testnet da Lighter**

---

## Regras de Qualidade

- **Arquivo ≤ 300 linhas, função ≤ 40 linhas.** Se `lighter.py` passar de 300 linhas, separar em `lighter_client.py` (REST layer) e `lighter_exchange.py` (implementação do ABC).
- **Sem `console.log` ou `print`** — usar `logger.*` do `bot/logger.py`.
- **Sem credenciais em logs** — private key e auth token nunca aparecem em log.
- **Tratamento de erro explícito** em toda chamada de rede — nunca engolir silenciosamente.
- **Diff mínimo** — não refatorar código não relacionado à task. Se identificar débito técnico no caminho, anotar como TODO e não executar.
- **Testes**: ao adicionar a lighter, criar ao menos testes unitários para `price_to_int`, `size_to_int`, e para o parsing de `sign` em `get_open_positions()`.
