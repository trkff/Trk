"""Tests for _drop_open_candle helper — garante que vela em formação é descartada."""
import pandas as pd

from bot.exchanges.lighter_exchange import _drop_open_candle


def _mk(timestamps_ms: list[int]) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": timestamps_ms,
        "open":  [1.0] * len(timestamps_ms),
        "high":  [1.0] * len(timestamps_ms),
        "low":   [1.0] * len(timestamps_ms),
        "close": [1.0] * len(timestamps_ms),
        "volume":[0.0] * len(timestamps_ms),
    })


def test_drops_open_candle_5m():
    # now=20:40:11 UTC → boundary atual = 20:40 UTC (1779655200000)
    now_ms = 1779655211000
    # candles: [20:30, 20:35, 20:40] — o último (20:40) é o aberto
    df = _mk([1779654600000, 1779654900000, 1779655200000])
    out = _drop_open_candle(df, "5m", now_ms=now_ms)
    assert len(out) == 2
    assert out["timestamp"].iloc[-1] == 1779654900000   # último = vela [20:35, 20:40) fechada


def test_keeps_all_when_no_open_candle():
    # now=20:39:59 → boundary atual ainda é 20:35 → vela [20:35,20:40) é a aberta;
    # se o array só tem até [20:30,20:35) (fechada), não deve descartar nada.
    now_ms = 1779655199000  # 20:39:59 UTC
    df = _mk([1779654600000, 1779654900000])   # [20:30,20:35) e [20:35,20:40)
    out = _drop_open_candle(df, "5m", now_ms=now_ms)
    # Boundary atual = 20:35 → vela com ts=1779654900000 (=20:35) é a aberta, descarta.
    assert len(out) == 1
    assert out["timestamp"].iloc[0] == 1779654600000


def test_empty_passthrough():
    assert _drop_open_candle(pd.DataFrame(columns=["timestamp"]), "5m").empty


def test_unknown_interval_passthrough():
    df = _mk([1779654600000])
    out = _drop_open_candle(df, "weird", now_ms=1779655211000)
    assert len(out) == 1


def test_15m_boundary():
    # boundary 15m: now=21:07 UTC → atual aberto = 21:00 UTC (1779656400000)
    now_ms = 1779656820000  # 21:07:00 UTC
    df = _mk([
        1779654600000,  # 20:30 closed
        1779655500000,  # 20:45 closed
        1779656400000,  # 21:00 OPEN
    ])
    out = _drop_open_candle(df, "15m", now_ms=now_ms)
    assert list(out["timestamp"]) == [1779654600000, 1779655500000]
