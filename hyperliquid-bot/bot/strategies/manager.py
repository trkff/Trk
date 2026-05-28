"""
Strategy manager — orchestrates all registered strategies.
Replaces the direct bot.signals.evaluate() call in main.py.
"""

import json
from bot.logger import get_logger
from bot import db
from bot.strategies.bb_reversion import BBReversionStrategy
from bot.strategies.bb_stoch import BBStochStrategy
from bot.strategies.stoch_scalp import StochScalpStrategy
from bot.strategies.ema_cross import EMACrossStrategy
from bot.strategies.rsi_scalp import RSIScalpStrategy
from bot.strategies.bb_rsi import BBRSIStrategy
from bot.strategies.macd_cross import MACDCrossStrategy
from bot.strategies.williams_r import WilliamsRStrategy

log = get_logger("strategies.manager")

REGISTERED_STRATEGIES = [
    BBReversionStrategy(
        name="bb_reversion_btc_5m",
        display_name="BB Reversion BTC",
        extra_defaults={
            "assets": ["BTC"],
            "bb_period": 10, "bb_std": 2.0, "ema_period": 50,
            "rsi_long_max": 65, "rsi_short_min": 35,
            "tp_pct": 2.0, "sl_pct": 0.8,
            "bbp_long_threshold": 0.10, "bbp_short_threshold": 0.90,
            "bb_mid_exit": True,
        },
    ),
    BBReversionStrategy(
        name="bb_reversion_eth_5m",
        display_name="BB Reversion ETH",
        extra_defaults={
            "assets": ["ETH"],
            "bb_period": 10, "bb_std": 2.0, "ema_period": 50,
            "rsi_long_max": 65, "rsi_short_min": 35,
            "tp_pct": 1.0, "sl_pct": 1.0,
            "bbp_long_threshold": 0.15, "bbp_short_threshold": 0.85,
            "bb_mid_exit": True,
        },
    ),
    BBReversionStrategy(
        name="bb_reversion_sol_5m",
        display_name="BB Reversion SOL",
        extra_defaults={
            "assets": ["SOL"],
            "bb_period": 10, "bb_std": 2.0, "ema_period": 200,
            "rsi_long_max": 100, "rsi_short_min": 0,  # sem filtro RSI
            "tp_pct": 2.0, "sl_pct": 0.5,
            "bbp_long_threshold": 0.05, "bbp_short_threshold": 0.95,
            "bb_mid_exit": True,
        },
    ),
    BBStochStrategy(
        name="bb_stoch_btc_5m",
        display_name="BB Stoch BTC",
        extra_defaults={
            "assets": ["BTC"],
            "bb_period": 20, "bb_std": 2.0,
            "bbp_long_threshold": 0.10, "bbp_short_threshold": 0.90,
            "stoch_long": 30, "stoch_short": 70,
            "sl_pct": 0.5, "tp_pct": 2.0,
            "ema_period": 0,
        },
    ),
    BBStochStrategy(
        name="bb_stoch_eth_5m",
        display_name="BB Stoch ETH",
        extra_defaults={
            "assets": ["ETH"],
            "bb_period": 20, "bb_std": 1.5,
            "bbp_long_threshold": 0.15, "bbp_short_threshold": 0.85,
            "stoch_long": 25, "stoch_short": 75,
            "sl_pct": 0.5, "tp_pct": 2.0,
            "ema_period": 0,
        },
    ),
    BBStochStrategy(
        name="bb_stoch_sol_5m",
        display_name="BB Stoch SOL",
        extra_defaults={
            "assets": ["SOL"],
            "bb_period": 15, "bb_std": 1.5,
            "bbp_long_threshold": 0.10, "bbp_short_threshold": 0.90,
            "stoch_long": 30, "stoch_short": 70,
            "sl_pct": 0.5, "tp_pct": 2.0,
            "ema_period": 200,
        },
    ),
    BBStochStrategy(
        name="bb_stoch_zec_5m",
        display_name="BB Stoch ZEC (5m)",
        extra_defaults={
            "bb_period":   10,
            "bb_std":      2.0,
            "bbp_long_threshold":  0.05,
            "bbp_short_threshold": 0.95,
            "stoch_long":  30,
            "stoch_short": 70,
            "tp_pct":      0.8,
            "sl_pct":      0.8,
            "bb_mid_exit": False,
            "ema_period":  0,
            "assets":      ["ZEC"],
        },
    ),
    BBStochStrategy(
        name="bb_stoch_ton_5m",
        display_name="BB Stoch TON (5m)",
        extra_defaults={
            "bb_period":   15,
            "bb_std":      1.5,
            "bbp_long_threshold":  0.10,
            "bbp_short_threshold": 0.90,
            "stoch_long":  25,
            "stoch_short": 75,
            "tp_pct":      0.8,
            "sl_pct":      0.8,
            "bb_mid_exit": False,
            "ema_period":  0,
            "assets":      ["TON"],
        },
    ),
    StochScalpStrategy(
        name="stoch_scalp_xau_5m",
        display_name="Stoch Scalp XAU (5m)",
        extra_defaults={
            "stoch_k":    9,
            "stoch_os":   40,
            "tp_pct":     0.5,
            "sl_pct":     0.8,
            "ema_period": 50,
            "assets":     ["XAU"],
        },
    ),
    StochScalpStrategy(
        name="stoch_scalp_wti_5m",
        display_name="Stoch Scalp WTI (5m)",
        extra_defaults={
            "stoch_k":    5,
            "stoch_os":   30,
            "tp_pct":     1.0,
            "sl_pct":     1.0,
            "ema_period": 50,
            "assets":     ["WTI"],
        },
    ),
    StochScalpStrategy(
        name="stoch_scalp_ton_5m",
        display_name="Stoch Scalp TON (5m)",
        extra_defaults={
            "stoch_k":    5,
            "stoch_os":   30,
            "tp_pct":     0.5,
            "sl_pct":     1.0,
            "ema_period": 200,
            "assets":     ["TON"],
        },
    ),
    EMACrossStrategy(
        name="ema_cross_hype_5m",
        display_name="EMA Cross HYPE (5m)",
        extra_defaults={
            "ema_fast":   9,
            "ema_slow":   21,
            "ema_trend":  200,
            "tp_pct":     1.5,
            "use_atr_sl": True,
            "atr_period": 14,
            "atr_mult":   1.0,
            "assets":     ["HYPE"],
        },
    ),
    EMACrossStrategy(
        name="ema_cross_lit_5m",
        display_name="EMA Cross LIT (5m)",
        extra_defaults={
            "ema_fast":   9,
            "ema_slow":   21,
            "ema_trend":  50,
            "tp_pct":     0.5,
            "use_atr_sl": False,
            "sl_pct":     0.5,
            "assets":     ["LIT"],
        },
    ),
    # ── RSI Scalp ─────────────────────────────────────────────────────────
    RSIScalpStrategy(
        name="rsi_scalp_btc_5m",
        display_name="RSI Scalp BTC (5m)",
        extra_defaults={"assets": ["BTC"], "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8},
    ),
    RSIScalpStrategy(
        name="rsi_scalp_eth_5m",
        display_name="RSI Scalp ETH (5m)",
        extra_defaults={"assets": ["ETH"], "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8},
    ),
    RSIScalpStrategy(
        name="rsi_scalp_sol_5m",
        display_name="RSI Scalp SOL (5m)",
        extra_defaults={"assets": ["SOL"], "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8},
    ),
    RSIScalpStrategy(
        name="rsi_scalp_ton_5m",
        display_name="RSI Scalp TON (5m)",
        extra_defaults={"assets": ["TON"], "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8},
    ),
    # ── BB RSI ────────────────────────────────────────────────────────────
    BBRSIStrategy(
        name="bb_rsi_btc_5m",
        display_name="BB RSI BTC (5m)",
        extra_defaults={"assets": ["BTC"], "bb_period": 15, "bb_std": 1.5, "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8, "bb_mid_exit": False},
    ),
    BBRSIStrategy(
        name="bb_rsi_eth_5m",
        display_name="BB RSI ETH (5m)",
        extra_defaults={"assets": ["ETH"], "bb_period": 15, "bb_std": 1.5, "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8, "bb_mid_exit": False},
    ),
    BBRSIStrategy(
        name="bb_rsi_sol_5m",
        display_name="BB RSI SOL (5m)",
        extra_defaults={"assets": ["SOL"], "bb_period": 15, "bb_std": 1.5, "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8, "bb_mid_exit": False},
    ),
    BBRSIStrategy(
        name="bb_rsi_zec_5m",
        display_name="BB RSI ZEC (5m)",
        extra_defaults={"assets": ["ZEC"], "bb_period": 15, "bb_std": 1.5, "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8, "bb_mid_exit": False},
    ),
    BBRSIStrategy(
        name="bb_rsi_ton_5m",
        display_name="BB RSI TON (5m)",
        extra_defaults={"assets": ["TON"], "bb_period": 15, "bb_std": 1.5, "rsi_period": 14, "rsi_os": 30, "tp_pct": 0.8, "sl_pct": 0.8, "bb_mid_exit": False},
    ),
    # ── MACD Cross ────────────────────────────────────────────────────────
    MACDCrossStrategy(
        name="macd_cross_btc_5m",
        display_name="MACD Cross BTC (5m)",
        extra_defaults={"assets": ["BTC"], "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "tp_pct": 1.0, "sl_pct": 0.5},
    ),
    MACDCrossStrategy(
        name="macd_cross_eth_5m",
        display_name="MACD Cross ETH (5m)",
        extra_defaults={"assets": ["ETH"], "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "tp_pct": 1.0, "sl_pct": 0.5},
    ),
    MACDCrossStrategy(
        name="macd_cross_sol_5m",
        display_name="MACD Cross SOL (5m)",
        extra_defaults={"assets": ["SOL"], "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "tp_pct": 1.0, "sl_pct": 0.5},
    ),
    # ── Williams %R ───────────────────────────────────────────────────────
    WilliamsRStrategy(
        name="williams_r_xau_5m",
        display_name="Williams %R XAU (5m)",
        extra_defaults={"assets": ["XAU"], "wr_period": 14, "wr_os": -80, "tp_pct": 0.8, "sl_pct": 0.8},
    ),
    WilliamsRStrategy(
        name="williams_r_wti_5m",
        display_name="Williams %R WTI (5m)",
        extra_defaults={"assets": ["WTI"], "wr_period": 14, "wr_os": -80, "tp_pct": 0.8, "sl_pct": 0.8},
    ),
    WilliamsRStrategy(
        name="williams_r_ton_5m",
        display_name="Williams %R TON (5m)",
        extra_defaults={"assets": ["TON"], "wr_period": 14, "wr_os": -80, "tp_pct": 0.8, "sl_pct": 0.8},
    ),
]

STRATEGY_MAP = {s.NAME: s for s in REGISTERED_STRATEGIES}

# Map de prefixo da instância → (classe, nome scanner) para criação dinâmica.
# Ordenado por comprimento DESC para garantir match correto (bb_reversion antes de bb_).
_STRATEGY_CLASS_BY_PREFIX: list[tuple[str, type, str]] = sorted([
    ("bb_reversion", BBReversionStrategy, "BB_Reversion"),
    ("bb_stoch",     BBStochStrategy,     "BB_Stoch"),
    ("stoch_scalp",  StochScalpStrategy,  "Stoch_Scalp"),
    ("ema_cross",    EMACrossStrategy,    "EMA_Cross"),
    ("rsi_scalp",    RSIScalpStrategy,    "RSI_Scalp"),
    ("bb_rsi",       BBRSIStrategy,       "BB_RSI"),
    ("macd_cross",   MACDCrossStrategy,   "MACD_Cross"),
    ("williams_r",   WilliamsRStrategy,   "Williams_R"),
], key=lambda x: -len(x[0]))

_SCANNER_NAME_TO_PREFIX = {sname: prefix for prefix, _, sname in _STRATEGY_CLASS_BY_PREFIX}


def _slug(s: str) -> str:
    """Slugify para sufixo de instância: lowercase, [a-z0-9_], max 24 chars."""
    import re
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s[:24]


_SUPPORTED_TFS = {"5m", "15m", "30m", "1h"}


def register_dynamic_instance(scanner_strategy: str, asset: str,
                              tag: str | None = None,
                              timeframe: str = "5m",
                              _legacy_no_tf_in_name: bool = False) -> str | None:
    """Cria uma instância dinâmica de estratégia e a registra em REGISTERED_STRATEGIES + STRATEGY_MAP.
    Nome:
      - `{prefix}_{asset}_{tf}[_{tag}]` (padrão novo)
      - `{prefix}_{asset}[_{tag}]` (quando `_legacy_no_tf_in_name=True`, só usado pelo loader pra preservar entradas antigas no DB)
    Retorna o nome da instância, ou None se a estratégia/TF não for reconhecido.
    """
    if timeframe not in _SUPPORTED_TFS:
        log.warning(f"[manager] register_dynamic_instance: TF inválido {timeframe!r}")
        return None
    prefix = _SCANNER_NAME_TO_PREFIX.get(scanner_strategy)
    if not prefix:
        log.warning(
            f"[manager] register_dynamic_instance: prefixo não encontrado para "
            f"scanner_strategy={scanner_strategy!r}. Chaves conhecidas: "
            f"{list(_SCANNER_NAME_TO_PREFIX.keys())}"
        )
        return None
    cls = next((c for p, c, _ in _STRATEGY_CLASS_BY_PREFIX if p == prefix), None)
    if cls is None:
        log.warning(f"[manager] register_dynamic_instance: classe None para prefix={prefix}")
        return None

    tag_slug = _slug(tag) if tag else ""
    base_name = f"{prefix}_{asset.lower()}" if _legacy_no_tf_in_name else f"{prefix}_{asset.lower()}_{timeframe}"
    name = f"{base_name}_{tag_slug}" if tag_slug else base_name

    if name in STRATEGY_MAP:
        log.info(f"[manager] register_dynamic_instance: já existia {name}")
        return name

    display_tag = f" [{tag_slug}]" if tag_slug else ""
    try:
        instance = cls(
            name=name,
            display_name=f"{scanner_strategy.replace('_', ' ')} {asset.upper()}{display_tag} ({timeframe})",
            extra_defaults={"assets": [asset.upper()], "timeframe": timeframe},
        )
    except Exception as e:
        log.error(f"[manager] register_dynamic_instance: falha ao instanciar {name}: {e}", exc_info=True)
        return None

    REGISTERED_STRATEGIES.append(instance)
    STRATEGY_MAP[name] = instance
    log.info(f"[manager] Instância dinâmica registrada: {name}")
    return name


def _load_dynamic_strategies():
    """No startup: varre o DB por `*.strategy.<name>.params` cujo <name> não
    esteja em REGISTERED_STRATEGIES mas case com algum prefixo conhecido —
    recria as instâncias. Pós-multi-profile, as keys vivem em
    `profile.<pid>.strategy.<name>.params`; também aceita o formato legado
    `strategy.<name>.params` (caso uma DB pré-M8 boote pela primeira vez).

    Reconhece dois formatos de nome:
      - {prefix}_{asset}           → legado 5m sem TF no nome (ex: bb_stoch_btc)
      - {prefix}_{asset}_{tf}[_{tag_slug}]   → novo formato com TF
    Registration is global (STRATEGY_MAP doesn't track profile) — each
    unique strategy name is registered once even if it appears under
    multiple profiles.
    """
    import re
    # Aceita "profile.<pid>.strategy.<name>.params" OU "strategy.<name>.params"
    pat = re.compile(r"^(?:profile\.\d+\.)?strategy\.(.+)\.params$")
    seen: set[str] = set()
    all_cfg = db.get_all_config()
    for key in all_cfg:
        m = pat.match(key)
        if not m:
            continue
        inst_name = m.group(1)
        if inst_name in STRATEGY_MAP or inst_name in seen:
            continue
        seen.add(inst_name)
        for prefix, cls, scanner_name in _STRATEGY_CLASS_BY_PREFIX:
            if not inst_name.startswith(prefix + "_"):
                continue
            rest = inst_name[len(prefix) + 1:]   # ex: "btc_15m_57_36" / "btc" / "btc_agressivo"
            parts = rest.split("_")
            asset = parts[0]
            tf = "5m"
            tag = None
            legacy = False
            if len(parts) == 1:
                # `prefix_asset` puro — legado pré-TF
                legacy = True
            elif parts[1] in _SUPPORTED_TFS:
                # Novo formato: prefix_asset_tf[_tag]
                tf = parts[1]
                if len(parts) > 2:
                    tag = "_".join(parts[2:])
            else:
                # Legado: prefix_asset_tag (sem TF no nome)
                legacy = True
                tag = "_".join(parts[1:])
            register_dynamic_instance(scanner_name, asset, tag=tag, timeframe=tf,
                                       _legacy_no_tf_in_name=legacy)
            break


_load_dynamic_strategies()


def get_active_assets(profile_id: int = 1) -> list[str]:
    """Union of assets across strategies enabled on this profile.

    Asset universe is driven EXCLUSIVELY by enabled strategies — habilitar
    uma estratégia pra CRCL faz o bot monitorar CRCL, nada mais. Não existe
    mais a lista global `monitored_assets` (que tinha o footgun de monitorar
    BTC/ETH/SOL por padrão mesmo sem estratégia usando — e pior: NÃO
    monitorar assets de estratégias enabled quando o admin esquecia de
    incluir na lista).
    """
    result: set[str] = set()
    for s in REGISTERED_STRATEGIES:
        scfg = db.get_strategy_config(s.NAME, profile_id=profile_id)
        if not scfg["enabled"]:
            continue
        params = {**s.DEFAULT_PARAMS, **scfg["params"]}
        for a in (params.get("assets") or []):
            result.add(a)
    return sorted(result)


def get_required_timeframes(profile_id: int = 1) -> list[str]:
    """Union dos TFs requeridos pelas estratégias enabled deste perfil.
    5m sempre incluído (é o trigger do WS). Para cada estratégia, lê params['timeframe']
    (com fallback para REQUIRED_TIMEFRAMES da classe)."""
    tfs: set[str] = {"5m"}
    for s in REGISTERED_STRATEGIES:
        scfg = db.get_strategy_config(s.NAME, profile_id=profile_id)
        if not scfg["enabled"]:
            continue
        params = {**s.DEFAULT_PARAMS, **scfg["params"]}
        tf = params.get("timeframe")
        if tf in _SUPPORTED_TFS:
            tfs.add(tf)
        else:
            tfs.update(s.REQUIRED_TIMEFRAMES)
    return sorted(tfs)


def get_all_strategy_metadata(profile_id: int = 1) -> list[dict]:
    """Return display metadata for all registered strategies on a profile."""
    result = []
    for s in REGISTERED_STRATEGIES:
        scfg = db.get_strategy_config(s.NAME, profile_id=profile_id)
        result.append({
            "name": s.NAME,
            "display_name": s.DISPLAY_NAME,
            "enabled": scfg["enabled"],
            "params": {**s.DEFAULT_PARAMS, **scfg["params"]},
            "default_params": s.DEFAULT_PARAMS,
        })
    return result


def evaluate_all(
    asset: str,
    indicators: dict,
    funding_rate: float,
    cfg: dict,
    df_1m=None,
    df_5m=None,
    df_2m=None,
    df_15m=None,
    df_30m=None,
    df_4h=None,
    df_1d=None,
    df_1h=None,
    new_4h: bool = False,
    new_1d: bool = False,
    new_1h: bool = False,
    new_5m: bool = False,
    new_15m: bool = False,
    new_30m: bool = False,
    profile_id: int = 1,
) -> list[dict]:
    """
    Evaluate strategies enabled on this profile and return their signals
    (at most one per strategy). Strategies are independent — each can fire
    its own signal. main.py / risk manager decides whether to execute each one.
    """
    global_assets = json.loads(cfg.get("monitored_assets", '["BTC","ETH","SOL"]'))
    signals = []
    for strategy in REGISTERED_STRATEGIES:
        scfg = db.get_strategy_config(strategy.NAME, profile_id=profile_id)
        if not scfg["enabled"]:
            continue

        params = {**strategy.DEFAULT_PARAMS, **scfg["params"]}

        strategy_assets = params.get("assets") or global_assets
        if asset not in strategy_assets:
            continue

        # Merge per-asset overrides on top of base params
        asset_overrides = params.get("asset_overrides", {})
        if isinstance(asset_overrides, dict) and asset in asset_overrides:
            params = {**params, **asset_overrides[asset]}

        try:
            signal = strategy.evaluate(
                asset, indicators, funding_rate, cfg, params,
                df_1m=df_1m, df_5m=df_5m, df_15m=df_15m, df_30m=df_30m, df_1h=df_1h,
                new_5m=new_5m, new_15m=new_15m, new_30m=new_30m, new_1h=new_1h,
                profile_id=profile_id,
            )
        except Exception as e:
            log.error(f"[{asset}] Strategy {strategy.NAME} error: {e}", exc_info=True)
            continue

        if signal is not None:
            log.signals(f"[{asset}] Signal from {strategy.NAME}: {signal.get('side')}")
            signals.append(signal)

    return signals
