from bot import db
from bot.exchanges.base import BaseExchangeClient


def create_exchange_client() -> BaseExchangeClient:
    cfg = db.get_all_config()
    selected = cfg.get("selected_exchange", "hyperliquid")

    if selected == "lighter":
        from bot.exchanges.lighter import LighterExchangeClient
        return LighterExchangeClient()
    else:
        from bot.exchanges.hyperliquid import HyperliquidClient
        return HyperliquidClient()
