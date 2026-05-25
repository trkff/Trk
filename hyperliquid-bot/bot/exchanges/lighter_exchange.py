"""
LighterExchangeClient — implements BaseExchangeClient for the Lighter DEX.
Uses lighter_client.py for all HTTP calls.
Candles are sourced from Lighter REST (/api/v1/candles) with fallback to Binance REST on error.

Key production gotchas (ported from YieldShield TypeScript implementation):
- Auth token expires in 1h; renew proactively at 50 minutes
- position.sign determines direction ('1'=Long, '-1'=Short); size is always positive
- Nonce desync on restart/timeout: catch 'invalid nonce', re-fetch, retry once
- market_close polls for 2.5s to confirm position zeroed before returning
- place_tp_sl uses ORDER_TYPE_TAKE_PROFIT (4) and ORDER_TYPE_STOP_LOSS (2)
- get_recent_fills uses aggregate=False for accurate PnL (aggregate=True diverges ~0.6%)
- Lighter timestamps from /api/v1/trades are in MILLISECONDS — compare ts directly against since_ms (do NOT divide by 1000)
"""

import asyncio
import threading
import time
import pandas as pd
from datetime import datetime, timezone

import requests

from bot.logger import get_logger
from bot import db
from bot.exchanges.base import BaseExchangeClient
from bot.exchanges.lighter_client import LighterClient

log = get_logger("exchanges.lighter")

# Order types
ORDER_TYPE_LIMIT = 0
ORDER_TYPE_MARKET = 1
ORDER_TYPE_STOP_LOSS = 2
ORDER_TYPE_STOP_LOSS_LIMIT = 3
ORDER_TYPE_TAKE_PROFIT = 4
ORDER_TYPE_TAKE_PROFIT_LIMIT = 5

# Time in force
TIF_IOC = 0
TIF_GOOD_TILL_TIME = 1

SLIPPAGE = 0.005
FILL_WAIT_SEC = 2.5
FILL_POLL_INTERVAL_SEC = 1.0
FILL_POLL_TIMEOUT_SEC = 6.0  # tempo adicional de polling após FILL_WAIT_SEC inicial
# Multiplicadores de slippage para retry escalonado em market_open.
# A 1ª tentativa usa o slippage configurado; se cancelar (race condition na batch
# da Lighter), aumentamos o limit IOC para 2.5× e depois 5× o slippage base.
# Em momentos calmos a 1ª tentativa preenche; em rompimentos de banda onde múltiplos
# bots competem pelo mesmo lado do book, o retry sobrevive ao mesmo preço de fill
# original mais um buffer maior. Aborta após esgotar os multiplicadores.
SLIP_RETRY_MULTIPLIERS = (1.0, 2.5, 5.0)

# Status de cancel da Lighter que NÃO melhoram com mais slippage — aborta retry imediato.
# margin/position/invalid-balance são restrições de risco da conta ou do market que slippage
# maior não resolve. self-trade idem. too-much-slippage e not-enough-liquidity continuam o retry.
LIGHTER_HARD_CANCEL_STATUSES = frozenset({
    "canceled-margin-not-allowed",
    "canceled-position-not-allowed",
    "canceled-invalid-balance",
    "canceled-self-trade",
    "canceled-post-only",
    "canceled-reduce-only",
})
AUTH_REFRESH_SEC = 50 * 60  # renew 10 minutes before 1h expiry
LIGHTER_TAKER_FEE_RATE = 0.0  # Lighter has zero trading fees

# Candle buffer settings
_CANDLE_BUFFER_MAX = 600   # max rows kept per (asset, interval) in the rolling buffer
_CANDLE_WARM_FETCH = 3     # candles fetched on incremental (warm) updates
_CANDLE_WS_FRESH_S = 60    # serve get_candles direct from buffer (no REST) when
                            # the WS manager wrote to this (asset, tf) in the last N seconds

# Interval → ms (used to drop the currently-open candle from Lighter REST responses).
# A Lighter REST `/api/v1/candles` devolve a vela ainda em formação como última linha,
# com `c` (close) = mark atual. Se a strategy ler `iloc[-1]`, ela acaba avaliando uma
# vela parcial em vez da que fechou — divergência clássica live↔backtest.
_INTERVAL_MS: dict[str, int] = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "12h": 43_200_000, "1d": 86_400_000,
}


def _drop_open_candle(df: pd.DataFrame, interval: str, now_ms: int | None = None) -> pd.DataFrame:
    """Descarta a última vela se o seu open_ts ≥ boundary atual (= vela ainda aberta).

    Pré-condição: `df` ordenado por timestamp ASC, com coluna `timestamp` em epoch ms.
    """
    if df.empty:
        return df
    ivms = _INTERVAL_MS.get(interval)
    if ivms is None:
        return df
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    current_open_ms = (now_ms // ivms) * ivms
    # vela cujo open_ts == current_open_ms ainda está em formação; descarta.
    return df[df["timestamp"] < current_open_ms]

# Binance symbol mapping: Lighter asset name → Binance spot symbol
_BINANCE_SYMBOL_MAP: dict[str, str] = {}  # populated lazily; fallback: append USDT


def _binance_symbol(asset: str) -> str:
    return _BINANCE_SYMBOL_MAP.get(asset.upper(), f"{asset.upper()}USDT")


def price_to_int(price: float, decimals: int) -> int:
    return round(price * 10 ** decimals)


def size_to_int(size: float, decimals: int) -> int:
    return round(size * 10 ** decimals)


class LighterExchangeClient(BaseExchangeClient):

    def __init__(self):
        self._wallet_address: str = ""
        self._public_key: str = ""
        self._private_key: str = ""
        self._client: LighterClient | None = None
        self._signer = None  # lighter.SignerClient — imported lazily
        self._account_index: int = -1
        self._api_key_index: int = -1
        self._auth_token: str = ""
        self._auth_token_expiry: float = 0.0
        self._initialized = False
        self._client_order_counter = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._candle_buffer: dict[tuple[str, str], pd.DataFrame] = {}
        self._candle_buffer_lock = threading.Lock()
        # Wall-clock timestamp (float, time.time()) when LighterCandleManager last
        # wrote to a (asset, tf) buffer via WS. get_candles uses this to decide
        # whether to serve directly from buffer (WS-fresh) or fall through to REST.
        self._candle_buffer_fresh_ts: dict[tuple[str, str], float] = {}

    @property
    def address(self) -> str:
        return self._wallet_address

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Return a running asyncio event loop for the lighter SDK's aiohttp connector."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(
                target=self._loop.run_forever,
                daemon=True,
                name="lighter-asyncio",
            )
            self._loop_thread.start()
        return self._loop

    def _init_signer(self, SignerClient, api_key_index: int):
        """Instantiate SignerClient inside the running event loop (aiohttp requirement)."""
        async def _mk():
            return SignerClient(
                url="https://mainnet.zklighter.elliot.ai",
                api_private_keys={api_key_index: self._private_key},
                account_index=self._account_index,
            )
        return asyncio.run_coroutine_threadsafe(_mk(), self._get_loop()).result(30)

    def _run_async(self, coro):
        """Run an async lighter SDK coroutine synchronously via the background event loop."""
        return asyncio.run_coroutine_threadsafe(coro, self._get_loop()).result(30)

    def _next_client_order_index(self) -> int:
        self._client_order_counter += 1
        return self._client_order_counter

    def _ensure_auth_token(self) -> str:
        if time.time() > self._auth_token_expiry:
            auth, err = self._signer.create_auth_token_with_expiry(
                deadline=3600, api_key_index=self._api_key_index
            )
            if err:
                raise RuntimeError(f"Failed to create Lighter auth token: {err}")
            self._auth_token = auth
            self._auth_token_expiry = time.time() + AUTH_REFRESH_SEC
        return self._auth_token

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        try:
            from lighter import SignerClient  # type: ignore[import]
        except ImportError:
            raise RuntimeError(
                "lighter-sdk not installed. Run: pip install lighter-sdk"
            )

        self._account_index = self._client.discover_account_index(self._wallet_address)
        api_key_index, nonce = self._client.discover_api_key_index(
            self._account_index, self._public_key
        )
        self._api_key_index = api_key_index
        self._signer = self._init_signer(SignerClient, api_key_index)
        self._client.load_markets()
        auth, err = self._signer.create_auth_token_with_expiry(
            deadline=3600, api_key_index=api_key_index
        )
        if err:
            raise RuntimeError(f"Failed to create Lighter auth token: {err}")
        self._auth_token = auth
        self._auth_token_expiry = time.time() + AUTH_REFRESH_SEC
        self._initialized = True
        log.info(f"Lighter initialized — account_index={self._account_index} api_key_index={api_key_index}")

    def connect(self) -> None:
        cfg = db.get_all_config()
        self._wallet_address = cfg.get("lighter_wallet_address", "")
        self._public_key = cfg.get("lighter_public_key", "")
        self._private_key = cfg.get("lighter_private_key", "")

        if not self._wallet_address or not self._public_key or not self._private_key:
            raise ValueError("Missing Lighter credentials. Set lighter_wallet_address, lighter_public_key, lighter_private_key.")

        self._client = LighterClient(user_label=self._wallet_address[:10])
        self._initialized = False
        self._ensure_init()
        log.info(f"Connected to Lighter — wallet {self._wallet_address[:10]}...")

    def disconnect(self) -> None:
        self._initialized = False
        self._signer = None
        self._client = None
        with self._candle_buffer_lock:
            self._candle_buffer.clear()
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
        self._loop_thread = None
        log.info("Disconnected from Lighter")

    def reconnect(self) -> None:
        log.warning("Reconnecting to Lighter...")
        self.disconnect()
        self.connect()

    # ── Candle data (Binance REST — Lighter has no native candle endpoint) ────

    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
        self._ensure_init()
        key = (asset.upper(), interval)

        with self._candle_buffer_lock:
            cached = self._candle_buffer.get(key, pd.DataFrame())

        # Fast path: WS manager wrote to this buffer recently. Skip REST entirely,
        # serve from buffer (still apply _drop_open_candle since WS includes the
        # still-forming candle). This eliminates 1.5s × N_tf REST calls per asset
        # on every candle close, which dominated the per-asset latency.
        fresh_ts = self._candle_buffer_fresh_ts.get(key, 0.0)
        if (
            not cached.empty
            and len(cached) >= count
            and time.time() - fresh_ts < _CANDLE_WS_FRESH_S
        ):
            served = _drop_open_candle(cached, interval)
            return served.iloc[-count:].copy()

        is_warm = not cached.empty and len(cached) >= count
        fetch_count = _CANDLE_WARM_FETCH if is_warm else count

        market = self._client.get_market(asset)
        if not market:
            log.warning(f"[{asset}] Market not found on Lighter, returning empty df")
            return pd.DataFrame()
        try:
            raw = self._client.get_candles(market["marketId"], interval, fetch_count)
            if not raw:
                raise ValueError("empty candles response")
            rows = [
                {
                    "timestamp": int(c["t"]),
                    "open":      float(c["o"]),
                    "high":      float(c["h"]),
                    "low":       float(c["l"]),
                    "close":     float(c["c"]),
                    "volume":    float(c["v"]),
                }
                for c in raw
            ]
            fresh = pd.DataFrame(rows)
            fresh["datetime"] = pd.to_datetime(fresh["timestamp"], unit="ms", utc=True)
            fresh.set_index("datetime", inplace=True)
            fresh.sort_index(inplace=True)

            # Filtra a vela ainda em formação: Lighter REST inclui o candle aberto
            # como última linha (close = mark atual). Strategy faz iloc[-1] e acabava
            # avaliando vela parcial — causa divergência live↔backtest.
            before = len(fresh)
            fresh = _drop_open_candle(fresh, interval)
            if before and len(fresh) < before:
                log.debug(f"[{asset}] dropped open {interval} candle ({before}→{len(fresh)})")

            if is_warm:
                merged = pd.concat([cached, fresh])
                merged = merged[~merged.index.duplicated(keep="last")]
                merged = merged.sort_index().iloc[-_CANDLE_BUFFER_MAX:]
            else:
                merged = fresh

            with self._candle_buffer_lock:
                self._candle_buffer[key] = merged

            return merged.iloc[-count:].copy()
        except Exception as e:
            log.warning(f"[{asset}] Lighter candles ({interval}) failed: {e}")
            return pd.DataFrame()

    # ── Funding rate ─────────────────────────────────────────────────

    def get_funding_rate(self, asset: str) -> float:
        self._ensure_init()
        market = self._client.get_market(asset)
        if not market:
            return 0.0
        return self._client.get_funding_rate(market["marketId"])

    # ── Account state ────────────────────────────────────────────────

    def get_account_value(self) -> float:
        self._ensure_init()
        auth = self._ensure_auth_token()
        acc = self._client.get_account(self._account_index, auth)
        return float(acc.get("collateral", 0))

    def get_open_positions(self) -> list[dict]:
        self._ensure_init()
        auth = self._ensure_auth_token()
        acc = self._client.get_account(self._account_index, auth)
        positions = []
        for pos in acc.get("positions", []):
            size = abs(float(pos["position"]))
            if size == 0:
                continue
            # Direction from sign field: '1' = Long, '-1' = Short
            sign = int(pos.get("sign", "-1"))
            side = "long" if sign == 1 else "short"
            market = self._client._market_by_id.get(pos["marketId"], {})
            positions.append({
                "coin": market.get("symbol", str(pos["marketId"])),
                "size": size,
                "side": side,
                "entry_price": float(pos["avgEntryPrice"]),
                "unrealized_pnl": float(pos["unrealizedPnl"]),
                "notional": abs(float(pos["positionValue"])),
            })
        return positions

    def get_mid_price(self, asset: str) -> float:
        self._ensure_init()
        auth = self._ensure_auth_token()
        market = self._client.get_market(asset)
        if not market:
            log.warning(f"[{asset}] Market not found on Lighter")
            return 0.0
        return self._client.get_mark_price(market["marketId"], auth)

    def get_recent_fills(self, asset: str, since_ms: int) -> list[dict]:
        self._ensure_init()
        auth = self._ensure_auth_token()
        market = self._client.get_market(asset)
        if not market:
            return []
        result = []
        cursor = None
        while True:
            # aggregate=False for accurate PnL (aggregate=True diverges ~0.6%)
            trades, cursor = self._client.get_trades_page(
                self._account_index, market["marketId"], auth,
                limit=50, cursor=cursor, aggregate=False,
            )
            for t in trades:
                # Lighter timestamps are in milliseconds — compare ms to ms
                ts = float(t["timestamp"])
                if ts < since_ms:
                    cursor = None
                    break
                is_buyer = str(t["bidAccountId"]) == str(self._account_index)
                pnl = float(t["bidAccountPnl"] if is_buyer else t["askAccountPnl"])
                result.append({
                    "oid": t["tradeId"],
                    "fee": 0.0,
                    "closedPnl": pnl,
                    "px": t["price"],
                    "sz": t["size"],
                    "side": "B" if is_buyer else "A",
                })
            if cursor is None:
                break
        return result

    def get_asset_sz_decimals(self, asset: str) -> int:
        self._ensure_init()
        market = self._client.get_market(asset)
        if not market:
            return 2
        return market["sizeDecimals"]

    def get_max_leverage(self, asset: str) -> float:
        self._ensure_init()
        market = self._client.get_market(asset)
        if not market:
            return 1.0
        # Lighter retorna default_initial_margin_fraction em basis points:
        # 500 = 5% = 20x leverage; 200 = 2% = 50x.
        imf_bps = market.get("initialMarginFractionBps")
        if not imf_bps or imf_bps <= 0:
            return 1.0
        return 10000.0 / imf_bps

    def get_user_funding_history(self, since_ms: int) -> list[dict]:
        # Lighter funding history per account is not currently exposed via REST.
        # Funding payments are reflected in the realized PnL of each position.
        return []

    # ── Order execution ──────────────────────────────────────────────

    def _place_order(self, asset: str, is_ask: bool, size: float, price: float,
                     order_type: int, reduce_only: bool,
                     trigger_price: float | None = None) -> dict:
        """Place a single order with nonce desync recovery (retry once)."""
        self._ensure_init()
        market = self._client.get_market(asset)
        if not market:
            raise ValueError(f"Market {asset} not found on Lighter")

        base_amount = size_to_int(size, market["sizeDecimals"])
        price_int = price_to_int(price, market["priceDecimals"])
        trigger_int = price_to_int(trigger_price, market["priceDecimals"]) if trigger_price is not None else 0

        # SDK sentinels: -1 = DEFAULT_28_DAY (trigger orders), 0 = DEFAULT_IOC (market orders).
        # The underlying signer converts -1 to a real 28-day timestamp internally.
        # Trigger orders must use TIF_GOOD_TILL_TIME — TIF_IOC would cancel them immediately
        # because the trigger price isn't reached at submission time.
        _TRIGGER_TYPES = {ORDER_TYPE_TAKE_PROFIT, ORDER_TYPE_TAKE_PROFIT_LIMIT,
                          ORDER_TYPE_STOP_LOSS, ORDER_TYPE_STOP_LOSS_LIMIT}
        is_trigger = order_type in _TRIGGER_TYPES
        order_expiry = -1 if is_trigger else 0
        time_in_force = TIF_GOOD_TILL_TIME if is_trigger else TIF_IOC

        for attempt in range(2):
            try:
                client_order_index = self._next_client_order_index()
                _tx_obj, resp, error = self._run_async(self._signer.create_order(
                    market_index=market["marketId"],
                    client_order_index=client_order_index,
                    base_amount=base_amount,
                    price=price_int,
                    is_ask=is_ask,
                    order_type=order_type,
                    time_in_force=time_in_force,
                    reduce_only=reduce_only,
                    trigger_price=trigger_int,
                    order_expiry=order_expiry,
                ))
                if error:
                    raise Exception(f"Order rejected by Lighter: {error}")
                return {
                    "txHash": resp.tx_hash if resp else "",
                    "client_order_index": client_order_index,
                }
            except Exception as e:
                if attempt == 0 and "invalid nonce" in str(e).lower():
                    log.warning(f"[{asset}] Nonce desync detected, resyncing...")
                    self._client.get_next_nonce(self._account_index, self._api_key_index)
                    try:
                        from lighter import SignerClient  # type: ignore[import]
                        self._signer = self._init_signer(SignerClient, self._api_key_index)
                    except Exception as init_err:
                        raise Exception(f"Nonce resync failed: {init_err}") from e
                    continue
                raise
        raise Exception(f"[{asset}] Order failed after nonce resync")

    def market_open(self, asset: str, is_buy: bool, size: float,
                    slippage: float = 0.005) -> dict:
        self._ensure_init()
        auth = self._ensure_auth_token()
        market = self._client.get_market(asset)
        if not market:
            raise ValueError(f"Market {asset} not found on Lighter")

        best_bid, best_ask = self._client.get_best_prices(market["marketId"], auth)
        if best_bid <= 0 or best_ask <= 0:
            raise ValueError(f"[{asset}] Cannot get book prices for market order (bid={best_bid} ask={best_ask})")

        # Snapshot position before sending so we can detect a real fill.
        self._client.invalidate_account_cache()
        before = self.get_open_positions()
        before_pos = next((p for p in before if p["coin"] == asset), None)
        before_size = before_pos["size"] if before_pos else 0.0
        before_side = before_pos["side"] if before_pos else None

        # IOC limit precisa cruzar o lado oposto do book.
        # BUY: limit >= best_ask; SELL: limit <= best_bid.
        # Slippage é tolerância acima do best ask (ou abaixo do best bid), NÃO acima do mid.
        is_ask = not is_buy
        expected_side = "long" if is_buy else "short"
        if before_pos and before_side != expected_side:
            # Opening opposite an existing position would net it; not a path we use here.
            log.warning(f"[{asset}] market_open against existing {before_side} position — aborting")
            return {"statuses": [{"error": "opposite-position"}]}

        spread_pct = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 100
        pre_send_ms = int(time.time() * 1000)
        tx = ""
        after_pos = None
        after_size = 0.0
        after_side = None
        filled = False

        # Retry escalonado: cada tentativa aumenta o slippage. Sobrevive a race
        # conditions na batch da Lighter onde múltiplos bots agressivos no mesmo lado
        # varrem o book entre snapshot e inclusão da tx. Aborta após esgotar os mults.
        for attempt_idx, slip_mult in enumerate(SLIP_RETRY_MULTIPLIERS):
            effective_slip = slippage * slip_mult
            # Releitura do book a cada tentativa (preço pode ter andado).
            if attempt_idx > 0:
                best_bid, best_ask = self._client.get_best_prices(market["marketId"], auth)
                if best_bid <= 0 or best_ask <= 0:
                    log.warning(f"[{asset}] Lost book prices mid-retry — aborting (bid={best_bid} ask={best_ask})")
                    break
            ref_price = best_ask if is_buy else best_bid
            limit_price = ref_price * (1 + effective_slip) if is_buy else ref_price * (1 - effective_slip)

            # Diagnóstico de profundidade.
            try:
                top = self._client.get_top_of_book(market["marketId"], auth, levels=5)
                target_levels = top["asks"] if is_buy else top["bids"]
                reachable_sz = sum(
                    sz for px, sz in target_levels
                    if (is_buy and px <= limit_price) or (not is_buy and px >= limit_price)
                )
                depth_str = ", ".join(f"{px}@{sz}" for px, sz in target_levels)
                depth_warn = " ⚠ THIN" if reachable_sz < size else ""
            except Exception as e:
                depth_str = f"depth_err={e}"
                reachable_sz = -1.0
                depth_warn = ""

            tag = f"try{attempt_idx + 1}/{len(SLIP_RETRY_MULTIPLIERS)}"
            log.info(
                f"[{asset}] market_open {'BUY' if is_buy else 'SELL'} {tag} bid={best_bid} ask={best_ask} "
                f"spread={spread_pct:.3f}% limit={limit_price} slip={effective_slip:.4f} (×{slip_mult}) "
                f"need={size} reachable={reachable_sz} [{depth_str}]{depth_warn}"
            )

            try:
                result = self._place_order(
                    asset=asset,
                    is_ask=is_ask,
                    size=size,
                    price=limit_price,
                    order_type=ORDER_TYPE_MARKET,
                    reduce_only=False,
                )
            except Exception as e:
                log.error(f"[{asset}] market_open send failed on {tag}: {e}")
                break  # erro de assinatura/SDK não vai melhorar com slippage maior
            tx = result.get("txHash", "")
            client_order_index = result.get("client_order_index")
            log.info(
                f"[{asset}] market_open {'BUY' if is_buy else 'SELL'} {tag} size={size} "
                f"tx={tx[:12]} coi={client_order_index}"
            )

            # Polling até confirmar fill ou estourar timeout.
            time.sleep(FILL_WAIT_SEC)
            deadline = time.time() + FILL_POLL_TIMEOUT_SEC
            poll_count = 0
            while True:
                self._client.invalidate_account_cache()
                after = self.get_open_positions()
                after_pos = next((p for p in after if p["coin"] == asset), None)
                after_size = after_pos["size"] if after_pos else 0.0
                after_side = after_pos["side"] if after_pos else None
                poll_count += 1
                if after_pos is not None and after_side == expected_side and after_size > before_size:
                    filled = True
                    break
                if time.time() >= deadline:
                    break
                time.sleep(FILL_POLL_INTERVAL_SEC)

            if filled:
                if attempt_idx > 0 or poll_count > 1:
                    log.info(
                        f"[{asset}] market_open fill confirmed on {tag} after {poll_count} polls "
                        f"(~{FILL_WAIT_SEC + (poll_count - 1) * FILL_POLL_INTERVAL_SEC:.1f}s)"
                    )
                break

            # Buscar motivo real do cancel no /accountInactiveOrders — campo `status`
            # tem o cancel reason verbatim do matching engine (canceled-margin-not-allowed,
            # canceled-too-much-slippage, etc). Hard cancels abortam retry imediato.
            # IMPORTANTE: o schema Order da Lighter NÃO tem campo tx_hash.
            # O identificador estável que conseguimos correlacionar é o `client_order_index`
            # (definido pelo bot em _place_order via _next_client_order_index()).
            # O endpoint /accountInactiveOrders pode indexar com lag de alguns segundos —
            # então fazemos polling curto (até ~6s) antes de declarar 'unknown'.
            cancel_status = ""
            coi_target = str(client_order_index) if client_order_index is not None else ""
            if coi_target:
                lookup_deadline = time.time() + 6.0
                while True:
                    try:
                        inactive = self._client.get_inactive_orders(
                            self._account_index, auth, market_id=market["marketId"], limit=50
                        )
                        for o in inactive:
                            coi_field = o.get("client_order_index", o.get("client_order_id"))
                            if coi_field is not None and str(coi_field) == coi_target:
                                cancel_status = str(o.get("status", ""))
                                break
                    except Exception as e:
                        log.debug(f"[{asset}] inactive-order lookup failed: {e}")
                    if cancel_status or time.time() >= lookup_deadline:
                        break
                    time.sleep(1.5)

            reason_str = cancel_status or "unknown (tx not in inactive orders yet)"
            log.warning(
                f"[{asset}] market_open {tag} did NOT fill — no {expected_side} position after "
                f"{poll_count} poll(s) over ~{FILL_POLL_TIMEOUT_SEC + FILL_WAIT_SEC:.1f}s "
                f"— Lighter reason: {reason_str}"
            )

            if cancel_status in LIGHTER_HARD_CANCEL_STATUSES:
                log.error(
                    f"[{asset}] hard cancel '{cancel_status}' — slippage retry won't help, aborting"
                )
                return {"statuses": [{"error": cancel_status}]}

        if not filled:
            log.error(
                f"[{asset}] market_open exhausted {len(SLIP_RETRY_MULTIPLIERS)} slippage retries "
                f"(up to {slippage * SLIP_RETRY_MULTIPLIERS[-1]:.4f}) — giving up"
            )
            return {"statuses": [{"error": "unfilled"}]}

        filled_sz = after_size - before_size
        if filled_sz <= 0:
            log.warning(f"[{asset}] market_open did NOT increase position (before={before_size} after={after_size})")
            return {"statuses": [{"error": "unfilled"}]}

        # Real fill price: weighted average of fills since send.
        avg_px = self._weighted_avg_fill_px(asset, pre_send_ms - 2000, is_buy=is_buy, expected_sz=filled_sz)
        if avg_px <= 0:
            # Fall back to position avgEntryPrice if no fill rows yet (API indexing lag).
            avg_px = after_pos["entry_price"]
            log.warning(f"[{asset}] No fills indexed yet for open; using position avgEntryPrice={avg_px}")

        log.info(f"[{asset}] market_open CONFIRMED size={filled_sz} @ {avg_px} (requested {size})")
        return {"statuses": [{"filled": {"avgPx": avg_px, "totalSz": filled_sz, "oid": tx}}]}

    def _weighted_avg_fill_px(self, asset: str, since_ms: int,
                              is_buy: bool, expected_sz: float | None = None) -> float:
        """Average fill price weighted by size, filtering by side.
        Returns 0.0 if no matching fills found."""
        try:
            fills = self.get_recent_fills(asset, since_ms)
        except Exception as e:
            log.warning(f"[{asset}] Failed to fetch fills for avg px: {e}")
            return 0.0
        want_side = "B" if is_buy else "A"
        matched = [f for f in fills if f.get("side") == want_side]
        if not matched:
            return 0.0
        total_sz = sum(float(f["sz"]) for f in matched)
        if total_sz <= 0:
            return 0.0
        notional = sum(float(f["px"]) * float(f["sz"]) for f in matched)
        return notional / total_sz

    def market_close(self, asset: str) -> dict:
        self._ensure_init()
        auth = self._ensure_auth_token()
        positions = self.get_open_positions()
        pos = next((p for p in positions if p["coin"] == asset), None)
        if not pos:
            log.warning(f"[{asset}] market_close called but no open position found")
            return {"statuses": []}

        market = self._client.get_market(asset)
        if not market:
            raise ValueError(f"Market {asset} not found on Lighter")

        best_bid, best_ask = self._client.get_best_prices(market["marketId"], auth)
        if best_bid <= 0 or best_ask <= 0:
            raise ValueError(f"[{asset}] Cannot get book prices for market close (bid={best_bid} ask={best_ask})")
        size = pos["size"]
        is_long = pos["side"] == "long"
        is_ask = is_long  # closing long = sell (ask), closing short = buy (bid)
        # IOC precisa cruzar o lado oposto do book: SELL → best_bid, BUY → best_ask.
        ref_price = best_bid if is_ask else best_ask
        limit_price = ref_price * (1 - SLIPPAGE) if is_ask else ref_price * (1 + SLIPPAGE)

        pre_close_ms = int(time.time() * 1000)
        result = self._place_order(
            asset=asset,
            is_ask=is_ask,
            size=size,
            price=limit_price,
            order_type=ORDER_TYPE_MARKET,
            reduce_only=True,
        )
        log.info(f"[{asset}] market_close size={size} tx={result.get('txHash', '')[:12]}")

        # Wait and confirm position zeroed
        time.sleep(FILL_WAIT_SEC)
        self._client.invalidate_account_cache()
        after = self.get_open_positions()
        remaining = next((p for p in after if p["coin"] == asset), None)
        for attempt in range(2):
            if not remaining or remaining["size"] == 0:
                break
            log.warning(f"[{asset}] Close unconfirmed, retry {attempt + 1}")
            time.sleep(1.5)
            self._client.invalidate_account_cache()
            after = self.get_open_positions()
            remaining = next((p for p in after if p["coin"] == asset), None)
        if remaining and remaining["size"] > 0:
            raise Exception(f"Lighter close did not fill for {asset}: {remaining['size']} remaining")

        # Real exit price: weighted average of close fills (close side is opposite of open).
        # is_buy for the close = NOT is_long (long close sells, short close buys).
        avg_px = self._weighted_avg_fill_px(asset, pre_close_ms - 2000, is_buy=(not is_long), expected_sz=size)
        if avg_px <= 0:
            fallback_mid = (best_bid + best_ask) / 2
            log.warning(f"[{asset}] No fills indexed yet for close; using mid={fallback_mid} (PnL fallback will recover real value)")
            avg_px = fallback_mid

        return {"statuses": [{"filled": {"avgPx": avg_px, "totalSz": size, "oid": result.get("txHash", "")}}]}

    def place_tp_sl(self, asset: str, is_buy_to_close: bool, size: float,
                    tp_price: float, sl_price: float, sz_decimals: int,
                    which: set | None = None) -> None:
        """Place TP and/or SL trigger orders. `which` filters which to place
        (default: both). Used by recovery to avoid duplicating existing triggers."""
        if which is None:
            which = {"tp", "sl"}
        self._ensure_init()
        market = self._client.get_market(asset)
        if not market:
            log.error(f"[{asset}] Cannot place TP/SL: market not found on Lighter")
            return

        is_ask = not is_buy_to_close  # closing a long = sell (ask)

        # TP order: ORDER_TYPE_TAKE_PROFIT_LIMIT — price = slippage limit on trigger
        # NOTE: market variants (TAKE_PROFIT/STOP_LOSS) only accept TIF_IOC which cancels
        # them at submission. The LIMIT variants accept TIF_GOOD_TILL_TIME and persist.
        if "tp" in which:
            tp_limit = tp_price * (1 - SLIPPAGE) if is_ask else tp_price * (1 + SLIPPAGE)
            try:
                self._place_order(
                    asset=asset,
                    is_ask=is_ask,
                    size=size,
                    price=tp_limit,
                    order_type=ORDER_TYPE_TAKE_PROFIT_LIMIT,
                    reduce_only=True,
                    trigger_price=tp_price,
                )
                log.info(f"[{asset}] Lighter TP placed @ trigger={tp_price:.4f}")
            except Exception as e:
                log.error(f"[{asset}] Failed to place Lighter TP: {e}", exc_info=True)

        # SL order: ORDER_TYPE_STOP_LOSS_LIMIT
        if "sl" in which:
            sl_limit = sl_price * (1 - SLIPPAGE) if is_ask else sl_price * (1 + SLIPPAGE)
            try:
                self._place_order(
                    asset=asset,
                    is_ask=is_ask,
                    size=size,
                    price=sl_limit,
                    order_type=ORDER_TYPE_STOP_LOSS_LIMIT,
                    reduce_only=True,
                    trigger_price=sl_price,
                )
                log.info(f"[{asset}] Lighter SL placed @ trigger={sl_price:.4f}")
            except Exception as e:
                log.error(f"[{asset}] Failed to place Lighter SL: {e}", exc_info=True)

    def get_open_trigger_order_types(self, asset: str) -> set:
        """Return set of active trigger types for asset: 'tp' and/or 'sl'.
        Queries /api/v1/accountActiveOrders and maps order type strings."""
        self._ensure_init()
        auth = self._ensure_auth_token()
        market = self._client.get_market(asset)
        if not market:
            return set()
        try:
            orders = self._client.get_active_orders(
                self._account_index, auth, market["marketId"]
            )
        except Exception as e:
            log.warning(f"[{asset}] Failed to fetch active orders: {e}")
            return {"tp", "sl"}  # assume present on error to avoid spurious re-placement
        result = set()
        for o in orders:
            t = o.get("type", "")
            if t in ("take-profit", "take-profit-limit"):
                result.add("tp")
            elif t in ("stop-loss", "stop-loss-limit"):
                result.add("sl")
        return result

    def list_active_trigger_orders(self, asset: str) -> list[dict]:
        """Lista ordens trigger ativas para o asset, com `order_index` (necessário p/ cancel).
        Retorna lista de dicts: {order_index, type, trigger_price, is_ask, base_amount}.
        """
        self._ensure_init()
        auth = self._ensure_auth_token()
        market = self._client.get_market(asset)
        if not market:
            return []
        try:
            orders = self._client.get_active_orders(
                self._account_index, auth, market["marketId"]
            )
        except Exception as e:
            log.warning(f"[{asset}] Failed to fetch active orders for trigger listing: {e}")
            return []
        triggers: list[dict] = []
        for o in orders:
            t = o.get("type", "")
            if t in ("take-profit", "take-profit-limit", "stop-loss", "stop-loss-limit"):
                triggers.append({
                    "order_index": o.get("order_index"),
                    "type": t,
                    "trigger_price": o.get("trigger_price"),
                    "is_ask": o.get("is_ask"),
                    "base_amount": o.get("base_amount") or o.get("initial_base_amount"),
                })
        return triggers

    def cancel_order(self, asset: str, order_index: int) -> bool:
        """Cancela uma ordem específica via signer (nonce desync recovery, igual _place_order).
        Retorna True se a tx foi assinada com sucesso (não garante que matching engine processou).
        """
        self._ensure_init()
        market = self._client.get_market(asset)
        if not market:
            log.warning(f"[{asset}] cancel_order: market not found")
            return False

        for attempt in range(2):
            try:
                _tx, _resp, error = self._run_async(self._signer.cancel_order(
                    market_index=market["marketId"],
                    order_index=int(order_index),
                ))
                if error:
                    raise Exception(f"Cancel rejected by Lighter: {error}")
                return True
            except Exception as e:
                if attempt == 0 and "invalid nonce" in str(e).lower():
                    log.warning(f"[{asset}] cancel_order nonce desync, resyncing...")
                    self._client.get_next_nonce(self._account_index, self._api_key_index)
                    try:
                        from lighter import SignerClient  # type: ignore[import]
                        self._signer = self._init_signer(SignerClient, self._api_key_index)
                    except Exception as init_err:
                        log.error(f"[{asset}] cancel_order nonce resync failed: {init_err}")
                        return False
                    continue
                log.error(f"[{asset}] cancel_order failed (order_index={order_index}): {e}")
                return False
        return False

    def cleanup_orphan_triggers(self, asset: str) -> int:
        """Cancela TP/SL trigger orders órfãs no asset (triggers ativas sem posição correspondente).
        Acontece quando uma trigger executa e a outra fica pendurada (não há OCO link na Lighter).
        Acúmulo dessas órfãs faz market_open novos retornarem `canceled-reduce-only`.
        Retorna o número de ordens canceladas.
        """
        self._ensure_init()
        # Se há posição ativa neste asset, NÃO limpamos — o recovery do risk.py cuida.
        self._client.invalidate_account_cache()
        positions = self.get_open_positions()
        if any(p["coin"] == asset and p["size"] > 0 for p in positions):
            return 0

        triggers = self.list_active_trigger_orders(asset)
        if not triggers:
            return 0

        canceled = 0
        for t in triggers:
            oid = t.get("order_index")
            if oid is None:
                continue
            log.info(
                f"[{asset}] Orphan trigger detected — type={t['type']} order_index={oid} "
                f"trigger={t.get('trigger_price')} — canceling"
            )
            if self.cancel_order(asset, oid):
                canceled += 1
        if canceled:
            log.warning(f"[{asset}] cleanup_orphan_triggers canceled {canceled}/{len(triggers)} orphan triggers")
        return canceled

    def check_position_exists(self, asset: str) -> bool:
        return any(p["coin"] == asset for p in self.get_open_positions())
