"""
Lighter REST API client — HTTP layer + data wrapper.
Handles rate limiting, WAF detection, and cursor-based pagination.

Critical gotchas discovered in production (YieldShield):
- AWS WAF blocks burst requests: serialize GETs with 300ms minimum gap
- 405 response = WAF block (not HTTP method error), wait 60s and retry once
- offset param in /api/v1/trades is silently ignored — use cursor pagination only
- position.sign field determines direction; position value is always positive
"""

import time
import threading
import requests

from bot.logger import get_logger

log = get_logger("exchanges.lighter_client")

LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai"
MIN_GET_GAP_SEC = 0.3
RATE_LIMIT_BACKOFFS = [0.5, 1.0, 2.0]
WAF_BODY_MARKERS = ["awsWafCookieDomainList", "captcha.awswaf.com", "challenge.js"]

_get_lock = threading.Lock()
_last_get_ts = 0.0


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
                    break  # retry outer while
                raise Exception(f"Lighter WAF blocked (405) after retry: {path}")
            if not resp.ok:
                body = resp.text[:500]
                ct = resp.headers.get("content-type", "")
                if _is_waf_blocked(body, ct):
                    raise Exception(f"Lighter WAF blocked ({resp.status_code}): {path}")
                raise Exception(f"Lighter API {resp.status_code}: {body}")
            ct = resp.headers.get("content-type", "")
            if _is_waf_blocked(resp.text[:200], ct):
                raise Exception(f"Lighter WAF blocked (HTML body): {path}")
            return resp.json()
        else:
            raise Exception(f"Lighter rate limited (429): {path}")


def lighter_post(user_label: str, path: str, data: dict) -> dict:
    # POST does not retry — risk of double-submit if tx was already accepted
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


class LighterClient:
    MARKET_CACHE_TTL = 3600
    FUNDING_CACHE_TTL = 300
    ACCOUNT_CACHE_TTL = 5

    def __init__(self, user_label: str):
        self.user_label = user_label
        self._market_cache: dict[str, dict] = {}
        self._market_by_id: dict[int, dict] = {}
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
        """Returns (api_key_index, nonce)."""
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
                "marketId": d["market_id"],
                "symbol": d["symbol"].upper(),
                "sizeDecimals": d["supported_size_decimals"],
                "priceDecimals": d["supported_price_decimals"],
                "minBaseAmount": float(d["min_base_amount"]),
                "minQuoteAmount": float(d["min_quote_amount"]),
                "lastTradePrice": float(d.get("last_trade_price") or 0),
                # Lighter retorna em basis points (500 = 5% = 20x leverage).
                # Usamos default_initial_margin_fraction (alavancagem inicial padrão do market);
                # min_initial_margin_fraction seria o limite máximo permitido (geralmente maior leverage).
                "initialMarginFractionBps": float(d.get("default_initial_margin_fraction") or 0) or None,
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
        accounts = resp.get("accounts") or []
        if not accounts:
            raise ValueError(f"Lighter account {account_index} not found")
        acc = accounts[0]
        result = {
            "index": acc["index"],
            "collateral": acc["collateral"],
            "availableBalance": acc["available_balance"],
            "positions": [
                {
                    "marketId": p["market_id"],
                    "symbol": p["symbol"],
                    "position": p["position"],
                    "avgEntryPrice": p["avg_entry_price"],
                    "positionValue": p["position_value"],
                    "unrealizedPnl": p["unrealized_pnl"],
                    # CRITICAL: position and positionValue are always POSITIVE.
                    # Direction comes from sign field: '-1' = Short, '1' = Long.
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

    def get_top_of_book(self, market_id: int, auth_token: str, levels: int = 5) -> dict:
        """Top N levels of bid/ask with sizes, para diagnóstico de profundidade.
        Retorna {'bids': [(price, size), ...], 'asks': [...], 'bid_sum', 'ask_sum'}.
        Tenta campos comuns (`remaining_base_amount`, `base_amount`, `size`, `amount`).
        """
        resp = lighter_get(
            self.user_label,
            f"/api/v1/orderBookOrders?market_id={market_id}&limit={levels}&auth={auth_token}",
        )
        size_keys = ("remaining_base_amount", "base_amount", "size", "amount", "remaining_amount")

        def _extract(side: list[dict]) -> list[tuple[float, float]]:
            out: list[tuple[float, float]] = []
            for lvl in side[:levels]:
                price = float(lvl.get("price", 0) or 0)
                sz = 0.0
                for k in size_keys:
                    if k in lvl:
                        try:
                            sz = float(lvl[k])
                            break
                        except (TypeError, ValueError):
                            continue
                out.append((price, sz))
            return out

        bids = _extract(resp.get("bids", []))
        asks = _extract(resp.get("asks", []))
        return {
            "bids": bids,
            "asks": asks,
            "bid_sum": sum(sz for _, sz in bids),
            "ask_sum": sum(sz for _, sz in asks),
        }

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
        self._funding_cache = {fr["market_id"]: float(fr["rate"]) for fr in resp.get("funding_rates", [])}
        self._funding_cache_ts = time.time()
        return self._funding_cache.get(market_id, 0.0)

    def send_tx(self, tx_type: int, tx_info: str) -> dict:
        resp = lighter_post(self.user_label, "/api/v1/sendTx", {"tx_type": tx_type, "tx_info": tx_info})
        if resp.get("code") != 200:
            raise Exception(f"Lighter sendTx failed: {resp.get('message', resp)}")
        self.invalidate_account_cache()
        return {"txHash": resp["tx_hash"], "predictedMs": resp.get("predicted_execution_time_ms", 0)}

    def get_candles(self, market_id: int, resolution: str, count: int) -> list[dict]:
        """Fetch OHLCV candles from Lighter REST. Returns at most 500 candles per call."""
        now_ms = int(time.time() * 1000)
        qs = [
            f"market_id={market_id}",
            f"resolution={resolution}",
            "start_timestamp=0",
            f"end_timestamp={now_ms}",
            f"count_back={min(count, 500)}",
        ]
        resp = lighter_get(self.user_label, f"/api/v1/candles?{'&'.join(qs)}")
        return resp.get("c", [])

    def get_active_orders(self, account_index: int, auth_token: str,
                          market_id: int | None = None) -> list[dict]:
        """Fetch open (active) orders for account. Includes trigger orders (TP/SL)."""
        qs = [f"account_index={account_index}", f"auth={auth_token}"]
        if market_id is not None:
            qs.append(f"market_id={market_id}")
        resp = lighter_get(self.user_label, f"/api/v1/accountActiveOrders?{'&'.join(qs)}")
        return resp.get("orders", [])

    def get_inactive_orders(self, account_index: int, auth_token: str,
                            market_id: int | None = None, limit: int = 10) -> list[dict]:
        """Fetch recent inactive orders (canceled/filled) com motivo do cancel no campo `status`.
        Possíveis status: filled, canceled, canceled-post-only, canceled-reduce-only,
        canceled-position-not-allowed, canceled-margin-not-allowed,
        canceled-too-much-slippage, canceled-not-enough-liquidity, canceled-self-trade,
        canceled-expired, canceled-oco, canceled-child, canceled-liquidation,
        canceled-invalid-balance.
        """
        qs = [f"account_index={account_index}", f"limit={min(max(limit, 1), 100)}", f"auth={auth_token}"]
        if market_id is not None:
            qs.append(f"market_id={market_id}")
        resp = lighter_get(self.user_label, f"/api/v1/accountInactiveOrders?{'&'.join(qs)}")
        return resp.get("orders", [])

    def get_trades_page(self, account_index: int, market_id: int, auth_token: str,
                        limit: int = 50, cursor: str | None = None,
                        aggregate: bool = False, sort_dir: str = "desc") -> tuple[list[dict], str | None]:
        # CRITICAL: offset param is silently ignored by Lighter — always returns page 0.
        # Only cursor-based pagination works correctly.
        qs = [
            f"account_index={account_index}",
            f"market_id={market_id}",
            "sort_by=timestamp",
            f"sort_dir={sort_dir}",
            f"limit={min(limit, 100)}",
            f"aggregate={'true' if aggregate else 'false'}",
            f"auth={auth_token}",
        ]
        if cursor:
            qs.append(f"cursor={cursor}")
        resp = lighter_get(self.user_label, f"/api/v1/trades?{'&'.join(qs)}")
        trades = [
            {
                "tradeId": t["trade_id"],
                "marketId": t["market_id"],
                "size": t["size"],
                "price": t["price"],
                "usdAmount": t["usd_amount"],
                "isMakerAsk": t["is_maker_ask"],
                "askAccountId": t["ask_account_id"],
                "bidAccountId": t["bid_account_id"],
                "askAccountPnl": t.get("ask_account_pnl", "0"),
                "bidAccountPnl": t.get("bid_account_pnl", "0"),
                "timestamp": t["timestamp"],
            }
            for t in resp.get("trades", [])
        ]
        next_cursor = resp.get("next_cursor") or None
        return trades, next_cursor
