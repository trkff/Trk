"""
Local CSV loader + Lighter REST candle updater.

Shared by backtest engine, scanner, and any other consumer that needs to
load historical OHLCV from `candles/{asset_lower}_5m.csv` or update it from
the Lighter REST API.

Other timeframes (15m, 1h, 4h, 1d) are resampled from the 5m CSV.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from bot.exchanges.lighter_client import lighter_get
from bot.logger import get_logger

log = get_logger("backtest.csv_loader")

# CSV files live at <project_root>/candles/{asset_lower}_5m.csv
# This file is at hyperliquid-bot/bot/backtest/csv_loader.py → 4 levels up = project root
_CANDLES_DIR = Path(__file__).parents[3] / "candles"

# Resample rules: 5m CSV → other intervals
_RESAMPLE_RULES = {
    "15m": "15min",
    "30m": "30min",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1D",
}

_INTERVAL_MS: dict[str, int] = {
    "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

# Intervals downloadable via the "Ativos" tab (saved as {asset}_{interval}.csv)
SUPPORTED_DOWNLOAD_INTERVALS: list[str] = ["5m", "15m", "30m", "1h"]

# Module-level market-id cache (refreshed every 5 min)
_market_id_cache: dict[str, int] = {}
_market_id_cache_ts: float = 0.0


# ── Candle loading from local CSV ──────────────────────────────────────────

def _load_candles_csv(asset: str, interval: str, days: int | None = None, extra_days: int = 0) -> pd.DataFrame:
    """
    Load historical OHLCV from local CSV at candles/{asset_lower}_5m.csv.
    Other intervals (15m, 1h, 4h, 1d) are resampled from the 5m data.

    Supports two CSV formats:
      - Numeric timestamp column (epoch ms): BTC, ETH, SOL, LIT
      - Datetime string column (YYYY-MM-DD HH:MM:SS): ZEC, TON, WTI, XAU, HYPE
    Column name may be 'timestamp' or 'ts'.

    Returns DataFrame with columns: timestamp (epoch ms int), open, high, low, close, volume.
    """
    _EMPTY = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    csv_path = _CANDLES_DIR / f"{asset.lower()}_5m.csv"
    if not csv_path.exists():
        log.warning(f"[backtest] CSV not found: {csv_path}")
        return _EMPTY

    df = pd.read_csv(csv_path)

    if "ts" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"ts": "timestamp"})

    if "timestamp" not in df.columns:
        log.warning(f"[backtest] No timestamp column in {csv_path.name}")
        return _EMPTY

    sample = df["timestamp"].iloc[0]
    if isinstance(sample, str):
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).astype("int64") // 1_000_000
    else:
        df["timestamp"] = df["timestamp"].astype("int64")

    df = df.sort_values("timestamp").reset_index(drop=True)
    df.index = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

    if days is not None:
        total_days = days + extra_days
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - total_days * 86_400_000
        df = df[df["timestamp"] >= start_ms].copy()
        if df.empty:
            log.warning(f"[backtest] No data in range for {asset} ({csv_path.name})")
            return _EMPTY

    if interval == "5m":
        log.backtest(f"[backtest] Loaded {len(df)} 5m candles for {asset} from CSV")
        return df

    rule = _RESAMPLE_RULES.get(interval)
    if rule is None:
        log.warning(f"[backtest] Unsupported interval {interval}, returning 5m data")
        return df

    resampled = df[["open", "high", "low", "close", "volume"]].resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["open"])

    resampled["timestamp"] = [int(ts.timestamp() * 1000) for ts in resampled.index]
    resampled = resampled.reset_index(drop=True)
    resampled.index = pd.to_datetime(resampled["timestamp"], unit="ms", utc=True)

    log.backtest(f"[backtest] Loaded {len(resampled)} {interval} candles for {asset} (resampled from 5m CSV)")
    return resampled


# ── CSV update from Lighter REST ───────────────────────────────────────────

def _get_lighter_market_id(asset: str) -> int | None:
    global _market_id_cache, _market_id_cache_ts
    if time.time() - _market_id_cache_ts > 300:
        try:
            resp = lighter_get("backtest", "/api/v1/orderBookDetails?filter=perp")
            _market_id_cache = {
                d["symbol"].upper(): d["market_id"]
                for d in resp.get("order_book_details", [])
                if d.get("status") == "active"
            }
            _market_id_cache_ts = time.time()
        except Exception as e:
            log.warning(f"[backtest] Failed to load Lighter markets: {e}")
    return _market_id_cache.get(asset.upper())


def _fetch_lighter_candles_since(asset: str, interval: str, since_ms: int) -> pd.DataFrame:
    """
    Fetch all 5m candles from Lighter REST with timestamp > since_ms.
    Paginates in batches of 500 walking backwards from now until since_ms is covered.
    """
    EMPTY = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    market_id = _get_lighter_market_id(asset)
    if market_id is None:
        log.warning(f"[backtest] {asset} not found on Lighter — cannot update CSV")
        return EMPTY

    interval_ms = _INTERVAL_MS.get(interval, 300_000)
    now_ms = int(time.time() * 1000)
    end_ms = now_ms
    all_rows: list[dict] = []

    while end_ms > since_ms:
        try:
            qs = (
                f"market_id={market_id}&resolution={interval}"
                f"&start_timestamp={since_ms}&end_timestamp={end_ms}&count_back=500"
            )
            resp = lighter_get("backtest", f"/api/v1/candles?{qs}")
            batch = resp.get("c", [])
        except Exception as e:
            log.warning(f"[backtest] Lighter candles fetch failed for {asset}: {e}")
            break

        if not batch:
            break

        rows = [
            {
                "timestamp": int(c["t"]),
                "open": float(c["o"]),
                "high": float(c["h"]),
                "low":  float(c["l"]),
                "close": float(c["c"]),
                "volume": float(c["v"]),
            }
            for c in batch
        ]
        all_rows.extend(rows)

        if len(batch) < 500:
            break

        earliest = min(r["timestamp"] for r in rows)
        if earliest <= since_ms:
            break
        end_ms = earliest - interval_ms

    if not all_rows:
        return EMPTY

    df = pd.DataFrame(all_rows)
    df = df[df["timestamp"] > since_ms]
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    # Descarta vela ainda em formação (Lighter inclui a vela aberta como última linha;
    # gravar isso no CSV congela um close errado e produz indicadores divergentes).
    if not df.empty:
        current_open_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        df = df[df["timestamp"] < current_open_ms].reset_index(drop=True)
    return df


# ── Public helpers for the "Ativos" tab ────────────────────────────────────

def list_lighter_perp_markets() -> list[dict]:
    """
    Return all active perp markets from Lighter with market stats:
    {symbol, market_id, last_price, volume_24h_usd, open_interest_base,
     open_interest_usd, price_change_24h_pct, daily_trades_count}.
    Refreshes the module market-id cache as a side effect.
    """
    try:
        resp = lighter_get("ativos", "/api/v1/orderBookDetails?filter=perp")
        raw = [d for d in resp.get("order_book_details", []) if d.get("status") == "active"]
        markets = []
        for d in raw:
            try:
                last_price = float(d.get("last_trade_price") or 0)
                oi_base = float(d.get("open_interest") or 0)
                vol_usd = float(d.get("daily_quote_token_volume") or 0)
                change = float(d.get("daily_price_change") or 0)
                trades = int(d.get("daily_trades_count") or 0)
            except (TypeError, ValueError):
                last_price = oi_base = vol_usd = change = 0.0
                trades = 0
            markets.append({
                "symbol": d["symbol"].upper(),
                "market_id": int(d["market_id"]),
                "last_price": last_price,
                "volume_24h_usd": vol_usd,
                "open_interest_base": oi_base,
                "open_interest_usd": oi_base * last_price,
                "price_change_24h_pct": change,
                "daily_trades_count": trades,
            })
        markets.sort(key=lambda m: m["symbol"])
        global _market_id_cache, _market_id_cache_ts
        _market_id_cache = {m["symbol"]: m["market_id"] for m in markets}
        _market_id_cache_ts = time.time()
        return markets
    except Exception as e:
        log.warning(f"[ativos] Failed to load Lighter markets: {e}")
        return []


def _read_native_csv(asset: str, interval: str) -> pd.DataFrame:
    """
    Read native CSV `candles/{asset_lower}_{interval}.csv` (epoch ms format).
    Used by the Ativos tab — does NOT resample (unlike `_load_candles_csv`).
    """
    _EMPTY = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    csv_path = _CANDLES_DIR / f"{asset.lower()}_{interval}.csv"
    if not csv_path.exists():
        return _EMPTY
    df = pd.read_csv(csv_path)
    if "ts" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"ts": "timestamp"})
    if "timestamp" not in df.columns:
        return _EMPTY
    sample = df["timestamp"].iloc[0]
    if isinstance(sample, str):
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).astype("int64") // 1_000_000
    else:
        df["timestamp"] = df["timestamp"].astype("int64")
    return df.sort_values("timestamp").reset_index(drop=True)


def get_csv_status(asset: str, interval: str = "5m") -> dict:
    """
    Return {"has_csv": bool, "rows": int, "first_ts": int|None, "last_ts": int|None}
    for the local `{asset_lower}_{interval}.csv`.
    """
    if interval not in SUPPORTED_DOWNLOAD_INTERVALS:
        return {"has_csv": False, "rows": 0, "first_ts": None, "last_ts": None}
    csv_path = _CANDLES_DIR / f"{asset.lower()}_{interval}.csv"
    if not csv_path.exists():
        return {"has_csv": False, "rows": 0, "first_ts": None, "last_ts": None}
    df = _read_native_csv(asset, interval)
    if df.empty:
        return {"has_csv": True, "rows": 0, "first_ts": None, "last_ts": None}
    return {
        "has_csv": True,
        "rows": int(len(df)),
        "first_ts": int(df["timestamp"].iloc[0]),
        "last_ts":  int(df["timestamp"].iloc[-1]),
    }


def download_full_history(asset: str, interval: str = "5m", progress_cb=None) -> dict:
    """
    Download all available `interval` candles for `asset` from Lighter REST and
    save to candles/{asset_lower}_{interval}.csv. If a CSV already exists,
    fetches only the missing candles since the last row. Returns a summary dict.

    Supported intervals: see SUPPORTED_DOWNLOAD_INTERVALS (5m, 15m, 30m, 1h).
    progress_cb(msg: str) is called periodically with human-readable progress.
    """
    if interval not in SUPPORTED_DOWNLOAD_INTERVALS:
        return {"ok": False, "error": f"intervalo inválido: {interval}"}

    _CANDLES_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = _CANDLES_DIR / f"{asset.lower()}_{interval}.csv"

    market_id = _get_lighter_market_id(asset)
    if market_id is None:
        return {"ok": False, "error": f"{asset} not found on Lighter"}

    if csv_path.exists():
        existing = _read_native_csv(asset, interval)
        since_ms = int(existing["timestamp"].iloc[-1]) if not existing.empty else 0
    else:
        existing = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        since_ms = 0

    interval_ms = _INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    end_ms = now_ms
    all_rows: list[dict] = []
    batches = 0

    while end_ms > since_ms:
        try:
            qs = (
                f"market_id={market_id}&resolution={interval}"
                f"&start_timestamp={max(since_ms, 0)}&end_timestamp={end_ms}&count_back=500"
            )
            resp = lighter_get("ativos", f"/api/v1/candles?{qs}")
            batch = resp.get("c", [])
        except Exception as e:
            log.warning(f"[ativos] {asset} {interval}: fetch failed: {e}")
            break

        if not batch:
            break

        rows = [
            {
                "timestamp": int(c["t"]),
                "open":  float(c["o"]),
                "high":  float(c["h"]),
                "low":   float(c["l"]),
                "close": float(c["c"]),
                "volume": float(c["v"]),
            }
            for c in batch
        ]
        all_rows.extend(rows)
        batches += 1

        if progress_cb and batches % 5 == 0:
            progress_cb(f"{asset} {interval}: {len(all_rows)} candles baixados...")

        if len(batch) < 500:
            break

        earliest = min(r["timestamp"] for r in rows)
        if earliest <= since_ms:
            break
        end_ms = earliest - interval_ms

    if not all_rows and existing.empty:
        return {"ok": False, "error": "Lighter retornou 0 candles"}

    new_df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame(columns=existing.columns)
    if not existing.empty:
        combined = pd.concat(
            [existing[["timestamp", "open", "high", "low", "close", "volume"]], new_df],
            ignore_index=True,
        )
    else:
        combined = new_df

    combined = (
        combined.drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    # Descarta vela ainda em formação (mesma razão do _fetch_lighter_candles_since).
    if not combined.empty:
        current_open_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        combined = combined[combined["timestamp"] < current_open_ms].reset_index(drop=True)
    combined.to_csv(csv_path, index=False)

    log.backtest(
        f"[ativos] {asset} {interval}: salvo {len(combined)} candles em "
        f"{csv_path.name} (+{len(new_df)} novos)"
    )
    return {
        "ok": True,
        "interval": interval,
        "rows": int(len(combined)),
        "added": int(len(new_df)),
        "first_ts": int(combined["timestamp"].iloc[0]),
        "last_ts": int(combined["timestamp"].iloc[-1]),
    }


def _update_csv(asset: str, progress_cb=None) -> None:
    """
    Check the local 5m CSV for asset and append any candles missing since the last row.
    Writes back in epoch-ms integer format (normalizes datetime-string CSVs on first update).
    """
    csv_path = _CANDLES_DIR / f"{asset.lower()}_5m.csv"
    if not csv_path.exists():
        log.backtest(f"[backtest] No CSV for {asset}, skipping update")
        return

    existing = _load_candles_csv(asset, "5m", days=None)
    if existing.empty:
        return

    last_ts = int(existing["timestamp"].iloc[-1])
    now_ms = int(time.time() * 1000)

    if now_ms - last_ts < _INTERVAL_MS["5m"]:
        log.backtest(f"[backtest] {asset} CSV already up to date")
        return

    missing_approx = (now_ms - last_ts) // _INTERVAL_MS["5m"]
    if progress_cb:
        progress_cb(f"Buscando ~{missing_approx} candles novos de {asset} na Lighter...")
    log.backtest(f"[backtest] {asset}: fetching ~{missing_approx} new 5m candles from Lighter (since {last_ts})")

    new_df = _fetch_lighter_candles_since(asset, "5m", last_ts)
    if new_df.empty:
        log.backtest(f"[backtest] {asset}: no new candles returned from Lighter")
        return

    combined = pd.concat(
        [existing[["timestamp", "open", "high", "low", "close", "volume"]], new_df],
        ignore_index=True,
    )
    combined = (
        combined.drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    combined.to_csv(csv_path, index=False)
    log.backtest(f"[backtest] {asset}: +{len(new_df)} candles appended (CSV now {len(combined)} rows)")
