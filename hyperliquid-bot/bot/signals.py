"""
DEPRECATED: Use bot.strategies.manager.evaluate_all() instead.
Kept for backward compatibility only.
"""


def evaluate(asset: str, indicators: dict, funding_rate: float, cfg: dict) -> dict | None:
    from bot.strategies.manager import evaluate_all
    return evaluate_all(asset, indicators, funding_rate, cfg)
