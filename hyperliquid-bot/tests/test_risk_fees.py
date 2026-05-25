"""Tests for fee handling in RiskManager.check_open_positions_tp_sl."""

from unittest.mock import MagicMock, patch
from bot.risk import RiskManager


def _make_open_trade(asset="ETH", side="long", entry_price=2066.20, size=0.0072,
                     order_id="0xabc123", open_fee=0.01):
    """Helper: simulates a trade row as returned by db.get_open_trades()."""
    return {
        "id": 1,
        "asset": asset,
        "side": side,
        "entry_price": entry_price,
        "size": size,
        "entry_time": "2026-03-30T13:05:21+00:00",
        "order_id": order_id,
        "fees": open_fee,  # stored at open time by executor
        "tp_price": entry_price + 2.0 if side == "long" else entry_price - 2.0,
        "sl_price": entry_price - 1.5 if side == "long" else entry_price + 1.5,
    }


def _make_fill(oid, side, px, fee, coin="ETH", time_ms=1743343200000):
    return {
        "oid": oid,
        "side": side,
        "px": str(px),
        "fee": str(fee),
        "coin": coin,
        "time": time_ms,
    }


class TestOpenFeeFromDB:
    """Bug: when the fills API doesn't return the open fill by oid,
    open_fee becomes 0.0 and total_fees = only close_fee (0.01).
    The stored open_fee (0.01) in the DB should be used instead,
    giving total_fees = 0.02."""

    @patch("bot.risk.db")
    def test_uses_stored_open_fee_when_fill_not_found(self, mock_db):
        """Open fill not in API response -> should use DB stored fee."""
        client = MagicMock()
        rm = RiskManager(client)

        trade = _make_open_trade(open_fee=0.01)
        mock_db.get_open_trades.return_value = [trade]

        # Exchange shows no position -> trade was closed by TP/SL
        client.get_open_positions.return_value = []

        # Fills API returns ONLY the close fill (open fill missing - the bug scenario)
        close_fill = _make_fill(oid="0xclose", side="A", px=2063.10, fee=0.0037)
        client.get_recent_fills.return_value = [close_fill]

        # Funding: none
        client.exchange.info.user_funding_history.return_value = []
        client.address = "0xuser"

        rm.check_open_positions_tp_sl()

        # Verify close_trade was called
        assert mock_db.close_trade.called
        call_args = mock_db.close_trade.call_args

        # Extract fees from the call: close_trade(trade_id, exit_price, pnl, pnl_pct, fees=..., funding=...)
        fees_kwarg = call_args.kwargs.get("fees") or call_args[1].get("fees")

        # open_fee from DB (0.01) + close_fee raw (0.0037) = 0.0137
        expected_fees = round(0.01 + 0.0037, 6)
        assert fees_kwarg == round(expected_fees, 6), (
            f"Expected total fees {expected_fees}, got {fees_kwarg}. "
            f"Open fee from DB should be used when fill API doesn't return open fill."
        )

    @patch("bot.risk.db")
    def test_uses_fill_fee_when_open_fill_found(self, mock_db):
        """Open fill IS in API response -> should use fill fee (existing behavior)."""
        client = MagicMock()
        rm = RiskManager(client)

        trade = _make_open_trade(order_id="0xopen1", open_fee=0.01)
        mock_db.get_open_trades.return_value = [trade]

        client.get_open_positions.return_value = []

        # Both fills returned by API
        open_fill = _make_fill(oid="0xopen1", side="B", px=2066.20, fee=0.004)
        close_fill = _make_fill(oid="0xclose1", side="A", px=2063.10, fee=0.0037,
                                time_ms=1743343300000)
        client.get_recent_fills.return_value = [open_fill, close_fill]

        client.exchange.info.user_funding_history.return_value = []
        client.address = "0xuser"

        rm.check_open_positions_tp_sl()

        assert mock_db.close_trade.called
        call_args = mock_db.close_trade.call_args
        fees_kwarg = call_args.kwargs.get("fees") or call_args[1].get("fees")

        # open_fee from DB (0.01) + close_fee raw (0.0037) = 0.0137
        expected_fees = round(0.01 + 0.0037, 6)
        assert fees_kwarg == round(expected_fees, 6), (
            f"Expected total fees {expected_fees}, got {fees_kwarg}."
        )
