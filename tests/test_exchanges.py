"""Парсеры ответов бирж на образцах из документации API."""

from app.exchanges import gate, mexc


def test_mexc_symbols():
    data = {
        "symbols": [
            {
                "symbol": "ABCUSDT",
                "status": "1",
                "baseAsset": "ABC",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {
                "symbol": "OFFUSDT",
                "status": "3",
                "baseAsset": "OFF",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
            },
            {"symbol": "NOSPOT", "status": "1", "baseAsset": "NS", "quoteAsset": "USDT", "isSpotTradingAllowed": False},
            {"symbol": "OLDUSDT", "status": "ENABLED", "baseAsset": "old", "quoteAsset": "usdt"},
        ]
    }
    out = mexc.parse_symbols(data)
    assert set(out) == {"ABCUSDT", "OLDUSDT"}
    assert out["OLDUSDT"].base == "OLD" and out["OLDUSDT"].quote == "USDT"


def test_mexc_tickers():
    data = [
        {
            "symbol": "ABCUSDT",
            "lastPrice": "0.0105",
            "openPrice": "0.01",
            "bidPrice": "0.0104",
            "askPrice": "0.0106",
            "volume": "1000000",
            "quoteVolume": "10500.5",
        },
        {
            "symbol": "NOVOL",
            "lastPrice": "2",
            "openPrice": "2",
            "bidPrice": "",
            "askPrice": None,
            "volume": "10",
            "quoteVolume": None,
        },
        {"symbol": "DEAD", "lastPrice": "0", "openPrice": "0"},
    ]
    out = mexc.parse_tickers(data)
    assert set(out) == {"ABCUSDT", "NOVOL"}
    t = out["ABCUSDT"]
    assert t.quote_volume_24h == 10500.5
    assert round(t.change_pct_24h, 6) == 5.0
    assert round(t.spread_pct, 4) == round((0.0106 - 0.0104) / 0.0104 * 100, 4)
    assert out["NOVOL"].quote_volume_24h == 20  # нет quoteVolume — считаем volume * last
    assert out["NOVOL"].spread_pct is None


def test_mexc_candles_and_trades():
    candles = mexc.parse_candles(
        [
            [1640804940000, "1.1", "1.3", "1.0", "1.2", "100", 1640805000000, "120"],
            [1640804880000, "1.0", "1.1", "0.9", "1.1", "50", 1640804940000, "52"],
        ]
    )
    assert [c.time for c in candles] == [1640804880, 1640804940]
    assert candles[1].high == 1.3 and candles[1].quote_volume == 120

    trades = mexc.parse_trades(
        [
            {"price": "1.2", "qty": "5", "time": 1640830411000, "isBuyerMaker": True},
            {"price": "1.3", "qty": "6", "time": 1640830410000, "isBuyerMaker": False},
        ]
    )
    assert [(t.side, t.qty) for t in trades] == [("buy", 6), ("sell", 5)]


def test_gate_symbols_and_tickers():
    pairs = gate.parse_symbols(
        [
            {"id": "ABC_USDT", "base": "ABC", "quote": "USDT", "trade_status": "tradable"},
            {"id": "OFF_USDT", "base": "OFF", "quote": "USDT", "trade_status": "untradable"},
        ]
    )
    assert list(pairs) == ["ABC_USDT"]
    tickers = gate.parse_tickers(
        [
            {
                "currency_pair": "ABC_USDT",
                "last": "2.46",
                "lowest_ask": "2.477",
                "highest_bid": "2.46",
                "change_percentage": "-8.91",
                "base_volume": "656614",
                "quote_volume": "1602221.66",
            },
            {"currency_pair": "EMPTY_USDT", "last": "1", "lowest_ask": "", "highest_bid": "", "quote_volume": "0"},
        ]
    )
    assert tickers["ABC_USDT"].quote_volume_24h == 1602221.66
    assert tickers["ABC_USDT"].change_pct_24h == -8.91
    assert tickers["EMPTY_USDT"].spread_pct is None


def test_gate_candles_and_trades():
    # [time, quote volume, close, high, low, open, base volume, closed]
    candles = gate.parse_candles(
        [
            ["1539852540", "200", "1.2", "1.3", "1.0", "1.1", "170", "false"],
            ["1539852480", "971.5", "0.0021724", "0.0021922", "0.0021724", "0.0021737", "447000", "true"],
        ]
    )
    assert [c.time for c in candles] == [1539852480, 1539852540]
    c = candles[0]
    assert (c.open, c.high, c.low, c.close, c.quote_volume) == (0.0021737, 0.0021922, 0.0021724, 0.0021724, 971.5)

    trades = gate.parse_trades(
        [
            {
                "id": "2",
                "create_time": "1548000001",
                "create_time_ms": "1548000001123.456",
                "side": "sell",
                "amount": "3",
                "price": "1",
            },
            {"id": "1", "create_time": "1548000000", "side": "buy", "amount": "2", "price": "1.1"},
        ]
    )
    assert [(t.ts, t.side) for t in trades] == [(1548000000000, "buy"), (1548000001123, "sell")]
