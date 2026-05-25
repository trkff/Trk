"""
Hyperliquid exchange client — REST + WebSocket.
Implements BaseExchangeClient; wraps hyperliquid-python-sdk.
"""

import time
import threading
import functools
import pandas as pd
from datetime import datetime, timezone
from typing import Callable

import eth_account
from eth_account.signers.local import LocalAccount
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

from bot.logger import get_logger
from bot import db
from bot.exchanges.base import BaseExchangeClient

log = get_logger("exchanges.hyperliquid")


def _retry_api(max_retries: int = 3, base_delay: float = 2.0):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return fn(self, *args, **kwargs)
                except (ConnectionError, TimeoutError, OSError) as e:
                    last_exc = e
                    delay = base_delay * (2 ** attempt)
                    log.warning(f"{fn.__name__} failed (attempt {attempt + 1}/{max_retries}): {e} — retrying in {delay:.0f}s")
                    time.sleep(delay)
                except Exception as e:
                    err_str = str(e).lower()
                    if any(kw in err_str for kw in ("connection", "timeout", "reset", "eof", "broken pipe", "503", "502", "429")):
                        last_exc = e
                        delay = base_delay * (2 ** attempt)
                        log.warning(f"{fn.__name__} failed (attempt {attempt + 1}/{max_retries}): {e} — retrying in {delay:.0f}s")
                        time.sleep(delay)
                    else:
                        raise
            log.error(f"{fn.__name__} failed after {max_retries} attempts: {last_exc}")
            raise last_exc
        return wrapper
    return decorator


class HyperliquidClient(BaseExchangeClient):
    def __init__(self):
        self._info: Info | None = None
        self._exchange: Exchange | None = None
        self._wallet: LocalAccount | None = None
        self._address: str = ""
        self._ws_info: Info | None = None
        self._ws_running = False
        self._ws_thread: threading.Thread | None = None
        self._candle_callbacks: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()

    @property
    def info(self) -> Info:
        if self._info is None:
            raise RuntimeError("Client not initialized. Call connect() first.")
        return self._info

    @property
    def exchange(self) -> Exchange:
        if self._exchange is None:
            raise RuntimeError("Client not initialized. Call connect() first.")
        return self._exchange

    @property
    def address(self) -> str:
        return self._address

    _EMPTY_SPOT_META = {"tokens": [], "universe": []}

    def _resolve_spot_meta(self, api_url: str) -> dict | None:
        try:
            Info(api_url, skip_ws=True)
            return None
        except IndexError as e:
            log.warning(
                f"Spot metadata incompatível com testnet ({e}). "
                "Usando spot_meta vazio para Info e Exchange."
            )
            return self._EMPTY_SPOT_META

    def connect(self) -> None:
        cfg = db.get_all_config()
        secret_key = cfg.get("secret_key", "")
        address_cfg = cfg.get("account_address", "")
        use_testnet = cfg.get("use_testnet", "true").lower() == "true"

        if not secret_key or not address_cfg:
            raise ValueError("Missing credentials in config. Set account_address and secret_key.")

        data_url = constants.MAINNET_API_URL
        order_url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
        order_label = "TESTNET" if use_testnet else "MAINNET"

        self._wallet = eth_account.Account.from_key(secret_key)
        self._address = address_cfg
        self._info = Info(data_url, skip_ws=True, spot_meta=self._resolve_spot_meta(data_url))
        self._exchange = Exchange(
            self._wallet, order_url, account_address=address_cfg,
            spot_meta=self._resolve_spot_meta(order_url),
        )

        log.info(f"Data: MAINNET | Orders: {order_label} — wallet {address_cfg[:10]}...")

    def disconnect(self) -> None:
        self.stop_ws()
        self._info = None
        self._exchange = None
        self._wallet = None
        log.info("Disconnected from Hyperliquid")

    def reconnect(self) -> None:
        log.warning("Reconnecting to Hyperliquid...")
        self.disconnect()
        self.connect()

    # ── Candle data ─────────────────────────────────────────────────

    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
        from bot.exchanges.base import fetch_binance_candles
        return fetch_binance_candles(asset, interval, count)

    # ── Funding rate ─────────────────────────────────────────────────

    @_retry_api()
    def get_funding_rate(self, asset: str) -> float:
        try:
            meta_and_ctxs = self.info.meta_and_asset_ctxs()
            meta = meta_and_ctxs[0]
            contexts = meta_and_ctxs[1]
            for i, a in enumerate(meta["universe"]):
                if a["name"] == asset:
                    return float(contexts[i]["funding"])
            log.warning(f"Asset {asset} not found in meta — funding=0")
            return 0.0
        except Exception as e:
            log.error(f"Failed to fetch funding rate for {asset}: {e}")
            return 0.0

    # ── Account state ────────────────────────────────────────────────

    @_retry_api()
    def get_user_state(self) -> dict:
        return self.exchange.info.user_state(self._address)

    def get_account_value(self) -> float:
        state = self.get_user_state()
        return float(state["marginSummary"]["accountValue"])

    def get_open_positions(self) -> list[dict]:
        state = self.get_user_state()
        positions = []
        for ap in state.get("assetPositions", []):
            pos = ap["position"]
            szi = float(pos["szi"])
            if szi == 0:
                continue
            positions.append({
                "coin": pos["coin"],
                "size": abs(szi),
                "side": "long" if szi > 0 else "short",
                "entry_price": float(pos["entryPx"]),
                "unrealized_pnl": float(pos["unrealizedPnl"]),
                "notional": abs(float(pos.get("positionValue", 0))),
                "leverage": pos.get("leverage", {}),
                "liquidation_px": pos.get("liquidationPx"),
            })
        return positions

    @_retry_api()
    def get_mid_price(self, asset: str) -> float:
        mids = self.info.all_mids()
        return float(mids.get(asset, 0))

    @_retry_api()
    def get_recent_fills(self, asset: str, since_ms: int) -> list[dict]:
        try:
            fills = self.exchange.info.user_fills_by_time(self._address, since_ms)
            if not isinstance(fills, list):
                return []
            return [f for f in fills if f.get("coin") == asset]
        except Exception as e:
            log.error(f"Failed to fetch fills for {asset}: {e}")
            return []

    @_retry_api()
    def get_asset_sz_decimals(self, asset: str) -> int:
        meta = self.info.meta_and_asset_ctxs()[0]
        for a in meta["universe"]:
            if a["name"] == asset:
                return int(a["szDecimals"])
        return 2

    @_retry_api()
    def get_max_leverage(self, asset: str) -> float:
        meta = self.info.meta_and_asset_ctxs()[0]
        for a in meta["universe"]:
            if a["name"] == asset:
                return float(a.get("maxLeverage", 1) or 1)
        return 1.0

    def get_user_funding_history(self, since_ms: int) -> list[dict]:
        try:
            return self.exchange.info.user_funding_history(self._address, since_ms)
        except Exception as e:
            log.warning(f"Failed to fetch funding history: {e}")
            return []

    # ── Order execution ──────────────────────────────────────────────

    def market_open(self, asset: str, is_buy: bool, size: float,
                    slippage: float = 0.005) -> dict:
        return self.exchange.market_open(asset, is_buy, size, slippage=slippage)

    def market_close(self, asset: str) -> dict:
        return self.exchange.market_close(asset)

    def place_tp_sl(self, asset: str, is_buy_to_close: bool, size: float,
                    tp_price: float, sl_price: float, sz_decimals: int,
                    which: set | None = None) -> None:
        if which is None:
            which = {"tp", "sl"}
        if "tp" in which:
            try:
                tp_result = self.exchange.order(
                    asset, is_buy_to_close, size, tp_price,
                    {"trigger": {"isMarket": True, "triggerPx": tp_price, "tpsl": "tp"}},
                    reduce_only=True,
                )
                log.info(f"[{asset}] TP trigger placed @ {tp_price} — response: {tp_result}")
            except Exception as e:
                log.error(f"[{asset}] Failed to place TP: {e}", exc_info=True)

        if "sl" in which:
            try:
                sl_result = self.exchange.order(
                    asset, is_buy_to_close, size, sl_price,
                    {"trigger": {"isMarket": True, "triggerPx": sl_price, "tpsl": "sl"}},
                    reduce_only=True,
                )
                log.info(f"[{asset}] SL trigger placed @ {sl_price} — response: {sl_result}")
            except Exception as e:
                log.error(f"[{asset}] Failed to place SL: {e}", exc_info=True)

    def check_position_exists(self, asset: str) -> bool:
        return any(p["coin"] == asset for p in self.get_open_positions())

    # ── WebSocket for live candle updates ────────────────────────────

    def start_ws(self, assets: list[str], on_candle: Callable = None):
        if self._ws_running:
            return

        try:
            data_url = constants.MAINNET_API_URL
            self._ws_info = Info(data_url, skip_ws=False, spot_meta=self._resolve_spot_meta(data_url))
        except Exception as e:
            log.warning(f"WebSocket init failed, falling back to REST polling: {e}")
            return

        self._ws_running = True

        for asset in assets:
            for interval in ["1m", "5m"]:
                def make_cb(a, iv):
                    def cb(msg):
                        if on_candle:
                            try:
                                data = msg.get("data", msg)
                                on_candle(a, iv, data)
                            except Exception as ex:
                                log.error(f"WS candle callback error: {ex}", exc_info=True)
                    return cb

                try:
                    self._ws_info.subscribe(
                        {"type": "candle", "coin": asset, "interval": interval},
                        make_cb(asset, interval),
                    )
                    log.info(f"WS subscribed: {asset} {interval}")
                except Exception as e:
                    log.warning(f"WS subscribe failed for {asset} {interval}: {e}")

    def stop_ws(self):
        self._ws_running = False
        if self._ws_info:
            try:
                self._ws_info.disconnect()
            except Exception:
                pass
            self._ws_info = None
