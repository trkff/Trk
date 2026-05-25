from abc import ABC, abstractmethod
import pandas as pd
import requests
from bot.logger import get_logger

_log = get_logger(__name__)

_BINANCE_SYMBOL_MAP: dict[str, str] = {
    "XAU": "XAUTUSDT",  # Tether Gold on Binance Spot
}


def fetch_binance_candles(asset: str, interval: str, count: int = 100) -> pd.DataFrame:
    """Fetch closed candles from Binance Spot REST API (public, no auth required)."""
    symbol = _BINANCE_SYMBOL_MAP.get(asset.upper(), f"{asset.upper()}USDT")
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={count}"
    try:
        resp = requests.get(url, timeout=10)
        if not resp.ok:
            _log.warning(f"[{asset}] Binance candles {resp.status_code}: {resp.text[:200]}")
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        rows = [
            {
                "timestamp": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in resp.json()
        ]
        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        df.sort_index(inplace=True)
        return df
    except Exception as e:
        _log.error(f"[{asset}] Failed to fetch Binance candles: {e}")
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


class BaseExchangeClient(ABC):

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def reconnect(self) -> None: ...

    @abstractmethod
    def get_candles(self, asset: str, interval: str, count: int = 100) -> pd.DataFrame:
        # Returns DataFrame with columns: timestamp, open, high, low, close, volume
        # Index: datetime (UTC)
        ...

    @abstractmethod
    def get_funding_rate(self, asset: str) -> float: ...

    @abstractmethod
    def get_account_value(self) -> float: ...

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        # Each dict: {coin, size, side, entry_price, unrealized_pnl, notional}
        ...

    @abstractmethod
    def get_mid_price(self, asset: str) -> float: ...

    @abstractmethod
    def get_recent_fills(self, asset: str, since_ms: int) -> list[dict]: ...

    @abstractmethod
    def get_asset_sz_decimals(self, asset: str) -> int: ...

    @abstractmethod
    def get_user_funding_history(self, since_ms: int) -> list[dict]:
        # Returns funding payment records since since_ms (epoch ms)
        # Each dict must have: {delta: {coin, szi, usdc}, time}
        ...

    @abstractmethod
    def market_open(self, asset: str, is_buy: bool, size: float,
                    slippage: float = 0.005) -> dict:
        # Returns raw exchange response (statuses list or equivalent)
        ...

    @abstractmethod
    def market_close(self, asset: str) -> dict:
        # Returns raw exchange response (statuses list or equivalent)
        ...

    @abstractmethod
    def place_tp_sl(self, asset: str, is_buy_to_close: bool, size: float,
                    tp_price: float, sl_price: float, sz_decimals: int,
                    which: set | None = None) -> None:
        # `which` filters which legs to place ({'tp','sl'}); None = both. Used by recovery.
        ...

    @abstractmethod
    def check_position_exists(self, asset: str) -> bool: ...

    def get_max_leverage(self, asset: str) -> float:
        """Return the maximum leverage allowed for `asset`. Default: 1.0 (no leverage).
        Override in exchanges that expose per-market leverage limits."""
        return 1.0

    def get_open_trigger_order_types(self, asset: str) -> set:
        """Return set of active trigger order types for asset: 'tp' and/or 'sl'.
        Default assumes both exist (no recovery). Override in exchanges that support it."""
        return {"tp", "sl"}

    def cleanup_orphan_triggers(self, asset: str) -> int:
        """Cancela trigger orders órfãs (sem posição associada) no asset.
        Default: no-op (exchanges com OCO nativo não precisam — ex: Hyperliquid).
        Override em exchanges que não fazem OCO (Lighter).
        Retorna o número de orders canceladas.
        """
        return 0

    @property
    @abstractmethod
    def address(self) -> str: ...
