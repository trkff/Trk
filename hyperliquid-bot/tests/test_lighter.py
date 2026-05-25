"""Unit tests for Lighter exchange helpers."""

import pytest
from bot.exchanges.lighter_exchange import price_to_int, size_to_int, LighterExchangeClient
from bot.exchanges.lighter_client import _is_waf_blocked


# ── price_to_int / size_to_int ────────────────────────────────────────────────

def test_price_to_int_basic():
    assert price_to_int(1.5, 2) == 150

def test_price_to_int_zero_decimals():
    assert price_to_int(100.0, 0) == 100

def test_price_to_int_high_decimals():
    assert price_to_int(0.001, 6) == 1000

def test_price_to_int_rounds_correctly():
    # 65000.12345 with 2 decimals → 6500012
    assert price_to_int(65000.12345, 2) == 6500012

def test_size_to_int_basic():
    assert size_to_int(0.5, 3) == 500

def test_size_to_int_zero_decimals():
    assert size_to_int(10.0, 0) == 10

def test_size_to_int_small_size():
    assert size_to_int(0.001, 3) == 1


# ── WAF detection ─────────────────────────────────────────────────────────────

def test_waf_blocked_html_content_type():
    assert _is_waf_blocked("anything", "text/html; charset=utf-8") is True

def test_waf_blocked_html_body_doctype():
    assert _is_waf_blocked("<!doctype html><html>", "application/json") is True

def test_waf_blocked_html_body_tag():
    assert _is_waf_blocked("<html><body>blocked</body></html>", "application/json") is True

def test_waf_blocked_aws_marker():
    body = '{"awsWafCookieDomainList": []}'
    assert _is_waf_blocked(body, "application/json") is True

def test_waf_not_blocked_normal_json():
    body = '{"order_book_details": []}'
    assert _is_waf_blocked(body, "application/json") is False

def test_waf_not_blocked_empty():
    assert _is_waf_blocked("", "application/json") is False


# ── get_open_positions sign field parsing ─────────────────────────────────────

def test_sign_long():
    """sign '1' must produce side='long'."""
    client = _make_lighter_client_with_positions([
        _make_position(sign="1", position="0.5", market_id=1, symbol="BTC"),
    ])
    positions = _parse_positions(client)
    assert positions[0]["side"] == "long"
    assert positions[0]["size"] == 0.5

def test_sign_short():
    """sign '-1' must produce side='short'."""
    client = _make_lighter_client_with_positions([
        _make_position(sign="-1", position="0.5", market_id=1, symbol="BTC"),
    ])
    positions = _parse_positions(client)
    assert positions[0]["side"] == "short"
    assert positions[0]["size"] == 0.5

def test_sign_size_always_positive():
    """position value is always positive regardless of sign."""
    for sign in ("1", "-1"):
        client = _make_lighter_client_with_positions([
            _make_position(sign=sign, position="1.23", market_id=1, symbol="ETH"),
        ])
        positions = _parse_positions(client)
        assert positions[0]["size"] > 0, f"size must be positive for sign={sign}"

def test_zero_size_position_excluded():
    """Positions with size=0 must not appear in get_open_positions()."""
    client = _make_lighter_client_with_positions([
        _make_position(sign="1", position="0", market_id=1, symbol="BTC"),
    ])
    positions = _parse_positions(client)
    assert positions == []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_position(sign: str, position: str, market_id: int, symbol: str) -> dict:
    return {
        "marketId": market_id,
        "symbol": symbol,
        "position": position,
        "avgEntryPrice": "50000",
        "positionValue": position,
        "unrealizedPnl": "0",
        "sign": sign,
    }


def _make_lighter_client_with_positions(positions: list[dict]) -> LighterExchangeClient:
    """Build a partially-initialised LighterExchangeClient with stubbed account data."""
    from unittest.mock import MagicMock

    client = LighterExchangeClient.__new__(LighterExchangeClient)
    client._wallet_address = "0xtest"
    client._public_key = ""
    client._private_key = ""
    client._account_index = 0
    client._api_key_index = 0
    client._auth_token = "tok"
    client._auth_token_expiry = float("inf")
    client._initialized = True
    client._client_order_counter = 0

    mock_lighter = MagicMock()
    mock_lighter._market_by_id = {1: {"symbol": positions[0]["symbol"], "marketId": 1}} if positions else {}
    mock_lighter.get_account.return_value = {
        "index": 0,
        "collateral": "1000",
        "availableBalance": "1000",
        "positions": positions,
    }
    client._client = mock_lighter
    return client


def _parse_positions(client: LighterExchangeClient) -> list[dict]:
    """Call get_open_positions bypassing _ensure_init and _ensure_auth_token."""
    # Patch the guards so we can call the method directly in unit tests
    from unittest.mock import patch
    with patch.object(client, "_ensure_init"), patch.object(client, "_ensure_auth_token", return_value="tok"):
        return client.get_open_positions()
