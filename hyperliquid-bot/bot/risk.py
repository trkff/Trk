"""
Risk management module.
- Max % capital per trade
- Max simultaneous positions
- Daily loss pause
- All parameters configurable via dashboard/SQLite
"""

import time
from datetime import datetime

from bot.logger import get_logger
from bot import db
from bot.exchanges.base import BaseExchangeClient

log = get_logger("risk")


class RiskManager:
    def __init__(self, client: BaseExchangeClient):
        self.client = client

    def _cfg(self) -> dict:
        return db.get_all_config()

    def can_open_trade(self, asset: str) -> tuple[bool, str]:
        """
        Check all risk rules before opening a new trade.
        Returns (allowed, reason).
        """
        cfg = self._cfg()

        # 1. Check max positions
        max_pos = int(cfg.get("max_positions", 2))
        open_trades = db.get_open_trades()
        if len(open_trades) >= max_pos:
            reason = f"Max positions reached ({len(open_trades)}/{max_pos})"
            log.warning(f"[{asset}] Trade blocked: {reason}")
            return False, reason

        # 2. Check if already in this asset
        if any(t["asset"] == asset for t in open_trades):
            reason = f"Already have open position in {asset}"
            log.warning(f"[{asset}] Trade blocked: {reason}")
            return False, reason

        # 3. Check daily loss limit
        max_daily_loss_pct = float(cfg.get("max_daily_loss_pct", 5.0))
        daily_pnl = db.get_daily_pnl()
        try:
            account_value = self.client.get_account_value()
        except Exception as e:
            log.error(f"Failed to get account value: {e}")
            return False, "Cannot fetch account value"

        if account_value <= 0:
            return False, "Account value is 0"

        daily_loss_pct = abs(min(daily_pnl, 0)) / account_value * 100
        if daily_loss_pct >= max_daily_loss_pct:
            reason = f"Daily loss limit hit: {daily_loss_pct:.2f}% >= {max_daily_loss_pct}%"
            log.warning(f"Trade blocked: {reason}")
            db.set_config("bot_status", "paused")
            return False, reason

        return True, "OK"

    def calculate_position_size(self, asset: str | None = None) -> float:
        """Calculate position size in USD.

        Two modes (selected by cfg["sizing_mode"]):
          - "risk_pct" (default): size = account_value * risk_pct_per_trade / 100
          - "fixed": size = fixed_trade_size_usd (constante por trade)

        Em ambos os modos faz guard de margem usando a alavancagem máxima do ativo:
        margem_necessária = size_usd / max_leverage; se account_value < margem_necessária
        bloqueia retornando 0.0 (logado como warning).
        """
        cfg = self._cfg()
        sizing_mode = str(cfg.get("sizing_mode", "risk_pct")).lower()

        try:
            account_value = self.client.get_account_value()
        except Exception as e:
            log.error(f"Failed to get account value for sizing: {e}")
            return 0.0

        if account_value <= 0:
            log.warning("Position size = 0: account_value <= 0")
            return 0.0

        if sizing_mode == "fixed":
            size_usd = float(cfg.get("fixed_trade_size_usd", 0) or 0)
            if size_usd <= 0:
                log.warning("sizing_mode=fixed mas fixed_trade_size_usd <= 0 — bloqueando trade")
                return 0.0
            label = f"fixed ${size_usd:.2f}"
        else:
            risk_pct = float(cfg.get("risk_pct_per_trade", 1.0))
            size_usd = account_value * (risk_pct / 100.0)
            label = f"{risk_pct}% of ${account_value:.2f}"

        # Margin guard usando max leverage do ativo
        if asset:
            try:
                max_lev = self.client.get_max_leverage(asset)
            except Exception as e:
                log.warning(f"[{asset}] Failed to fetch max leverage — assuming 1x: {e}")
                max_lev = 1.0
            if max_lev <= 0:
                max_lev = 1.0
            required_margin = size_usd / max_lev
            if account_value < required_margin:
                log.warning(
                    f"[{asset}] Trade bloqueado: margem insuficiente "
                    f"(size=${size_usd:.2f} / lev={max_lev:.0f}x = ${required_margin:.2f} > "
                    f"account=${account_value:.2f})"
                )
                return 0.0
            log.debug(
                f"[{asset}] Position size: ${size_usd:.2f} ({label}); "
                f"margin=${required_margin:.2f} @ {max_lev:.0f}x, account=${account_value:.2f}"
            )
        else:
            log.debug(f"Position size: ${size_usd:.2f} ({label})")

        return size_usd

    def check_open_positions_tp_sl(self):
        """
        Check if any open trades have been closed by TP/SL on the exchange.
        If the exchange position no longer exists, close the trade record.
        """
        open_trades = db.get_open_trades()
        if not open_trades:
            return

        try:
            exchange_positions = self.client.get_open_positions()
        except Exception as e:
            log.error(f"Failed to fetch exchange positions: {e}")
            return

        exchange_coins = {p["coin"] for p in exchange_positions}

        for trade in open_trades:
            if trade["asset"] in exchange_coins:
                # Position is still open — verify TP/SL trigger orders exist
                tp_price = trade.get("tp_price")
                sl_price = trade.get("sl_price")
                if tp_price and sl_price:
                    try:
                        active_triggers = self.client.get_open_trigger_order_types(trade["asset"])
                        missing = {"tp", "sl"} - active_triggers
                        if missing:
                            asset = trade["asset"]
                            log.warning(
                                f"[{asset}] Trade #{trade['id']} missing trigger orders: {missing} — re-placing"
                            )
                            is_long = trade["side"] == "long"
                            sz_dec = self.client.get_asset_sz_decimals(asset)
                            self.client.place_tp_sl(
                                asset, not is_long, trade["size"],
                                float(tp_price), float(sl_price), sz_dec,
                                which=missing,
                            )
                    except Exception as e:
                        log.warning(f"[{trade['asset']}] TP/SL recovery check failed: {e}")
                continue

            if trade["asset"] not in exchange_coins:
                # Position was closed (TP or SL hit)
                log.info(f"[{trade['asset']}] Position no longer on exchange — recording close")
                entry_px = trade["entry_price"]
                size = trade["size"]

                # Look for fills since trade entry (covers open + close legs regardless of hold duration)
                try:
                    since_ms = int(datetime.fromisoformat(trade["entry_time"]).timestamp() * 1000)
                except (ValueError, TypeError, KeyError):
                    since_ms = int(time.time() * 1000) - 600_000  # fallback: 10 min
                fills = self.client.get_recent_fills(trade["asset"], since_ms)
                close_side = "B" if trade["side"] == "short" else "A"  # closing a long = sell (A), short = buy (B)
                all_close_fills = [f for f in fills if f.get("side") == close_side]

                # Fills arrive newest-first. Accumulate only until we reach our position
                # size to avoid pulling in fills from previous trades on the same asset.
                close_fills = []
                remaining = float(trade["size"])
                for cf in all_close_fills:
                    if remaining <= 1e-9:
                        break
                    close_fills.append(cf)
                    remaining -= float(cf.get("sz", 0.0))

                exit_price = 0.0
                if close_fills:
                    # Aggregate all partial close fills (weighted avg price, sum fees/pnl)
                    total_sz = 0.0
                    weighted_px = 0.0
                    close_fee = 0.0
                    closed_pnl = 0.0
                    for cf in close_fills:
                        sz = float(cf.get("sz", 0.0))
                        weighted_px += float(cf["px"]) * sz
                        total_sz += sz
                        close_fee += float(cf.get("fee", 0.0))
                        closed_pnl += float(cf.get("closedPnl", 0.0))
                    exit_price = weighted_px / total_sz if total_sz > 0 else float(close_fills[0]["px"])
                    log.info(f"[{trade['asset']}] Exit price from fill: {exit_price} ({len(close_fills)} fill(s))")
                else:
                    mid = self.client.get_mid_price(trade["asset"])
                    tp = trade.get("tp_price") or 0
                    sl = trade.get("sl_price") or 0
                    if tp > 0 and sl > 0 and mid > 0:
                        exit_price = tp if abs(mid - tp) < abs(mid - sl) else sl
                    elif tp > 0:
                        exit_price = tp
                    elif sl > 0:
                        exit_price = sl
                    else:
                        exit_price = mid if mid > 0 else entry_px
                    log.warning(f"[{trade['asset']}] No closing fill found — using estimated exit {exit_price:.4f}")
                    close_fee = 0.0
                    closed_pnl = (exit_price - entry_px) * size if trade["side"] == "long" else (entry_px - exit_price) * size

                # Fees: open fee from DB (stored at open time), close fee from fill(s)
                open_fee = float(trade.get("fees") or 0.0)
                total_fees = open_fee + close_fee

                # closedPnl da HL = lucro bruto (sem fees). PnL real = closedPnl - fees

                # Fetch funding payments only within the trade's hold period
                net_funding = 0.0
                try:
                    trade_entry_ms = since_ms  # already parsed from trade["entry_time"]
                    trade_exit_ms = int(time.time() * 1000)
                    funding_records = self.client.get_user_funding_history(trade_entry_ms)
                    close_side_sign = 1 if trade["side"] == "long" else -1
                    for fp in funding_records:
                        d = fp.get("delta", {})
                        fp_time = int(fp.get("time", 0))
                        if (d.get("coin") == trade["asset"]
                                and float(d.get("szi", 0)) * close_side_sign > 0
                                and trade_entry_ms <= fp_time <= trade_exit_ms):
                            net_funding += float(d.get("usdc", 0))
                except Exception as fe:
                    log.warning(f"[{trade['asset']}] Could not fetch funding: {fe}")

                pnl = closed_pnl - total_fees + net_funding
                pnl_pct = (pnl / (entry_px * size)) * 100 if entry_px * size > 0 else 0
                db.close_trade(trade["id"], round(exit_price, 4), round(pnl, 4), round(pnl_pct, 2),
                               fees=round(total_fees, 6), funding=round(net_funding, 6))
                log.info(f"[{trade['asset']}] Trade #{trade['id']} closed — PnL=${pnl:.2f} ({pnl_pct:.2f}%) fees=${total_fees:.4f} funding=${net_funding:.4f}")

                # Limpa a trigger que sobrou (a outra leg da OCO sintética: se TP disparou,
                # SL fica órfã; se SL disparou, TP fica órfã). Sem isso, novos market_open
                # nesse asset podem voltar com `canceled-reduce-only`.
                try:
                    n_orphan = self.client.cleanup_orphan_triggers(trade["asset"])
                    if n_orphan:
                        log.info(f"[{trade['asset']}] Post-close cleanup: canceled {n_orphan} orphan trigger(s)")
                except Exception as e:
                    log.warning(f"[{trade['asset']}] Post-close cleanup failed: {e}")
