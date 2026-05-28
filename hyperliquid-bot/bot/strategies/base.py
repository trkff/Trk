from abc import ABC, abstractmethod


SUPPORTED_TFS = ("5m", "15m", "30m", "1h")


def select_tf_df(p: dict, kwargs: dict, *,
                 name: str | None = None, asset: str | None = None,
                 df_5m=None, df_15m=None, df_30m=None, df_1h=None):
    """Helper para estratégias multi-TF.

    Lê `p['timeframe']` (default '5m'), confere o trigger `new_{tf}` em kwargs e
    retorna (tf, df) — o df correspondente ao TF. Se não for candle close do TF
    da estratégia (ou se o df estiver ausente), retorna (tf, None) e o evaluate
    deve sair cedo. `name` e `asset` são aceitos mas não-usados (placeholder
    para logging futuro).
    """
    tf = p.get("timeframe", "5m")
    if tf not in SUPPORTED_TFS:
        tf = "5m"
    if not kwargs.get(f"new_{tf}", False):
        return tf, None
    df_map = {"5m": df_5m, "15m": df_15m, "30m": df_30m, "1h": df_1h}
    return tf, df_map.get(tf)


class BaseStrategy(ABC):
    NAME: str = ""
    DISPLAY_NAME: str = ""
    DEFAULT_PARAMS: dict = {}
    REQUIRED_TIMEFRAMES: list[str] = ["5m"]

    @staticmethod
    def _make_indicators_snapshot(values: dict) -> str:
        """Serialize an indicator snapshot to JSON for the fidelity checker.

        Filters None/NaN/inf values; rounds floats to 6 decimals to keep the
        payload small and reproducible across re-runs of the backtest engine.
        """
        import json
        import math
        clean: dict = {}
        for k, v in values.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                clean[k] = v
                continue
            if math.isnan(fv) or math.isinf(fv):
                continue
            clean[k] = round(fv, 6)
        return json.dumps(clean)

    @abstractmethod
    def evaluate(
        self,
        asset: str,
        indicators: dict,
        funding_rate: float,
        cfg: dict,
        params: dict,
        df_1m=None,
        df_5m=None,
        df_2m=None,
        df_4h=None,
        df_1d=None,
        **kwargs,
    ) -> dict | None:
        """
        Returns a signal dict if a trade should be opened, or None.
        Blocked signals must be persisted to DB by the strategy itself.
        Signal dict must include: timestamp, asset, side, executed, reason,
        ema9, ema21, rsi2, volume, volume_avg, atr, funding_rate, strategy_name.
        df_1m and df_5m are optional raw DataFrames passed by manager.evaluate_all().
        """
        ...
