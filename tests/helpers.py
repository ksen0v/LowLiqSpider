from __future__ import annotations

from app.models import Candle, ScanContext, SymbolInfo, Ticker, Trade

NOW_MS = 1_800_000_000_000
NOW = NOW_MS // 1000


def ctx(candles=None, trades=None, ticker=None) -> ScanContext:
    return ScanContext(
        exchange="mexc",
        info=SymbolInfo("ABCUSDT", "ABC", "USDT"),
        now_ms=NOW_MS,
        candles=candles or [],
        trades=trades,
        ticker=ticker,
    )


def flat_candles(n: int, price: float = 1.0, quote_volume: float = 10.0, end: int = NOW) -> list[Candle]:
    """n минутных свечей, последняя — текущая незакрытая."""
    start = (end // 60 - n + 1) * 60
    return [Candle(start + i * 60, price, price, price, price, quote_volume / price, quote_volume) for i in range(n)]


def ping_pong_trades(n: int = 40, low: float = 0.995, high: float = 1.005, qty: float = 100.0) -> list[Trade]:
    """Алгоритм: buy по верху, sell по низу, одним и тем же объёмом, каждые 10 секунд."""
    trades = []
    for i in range(n):
        side = "buy" if i % 2 == 0 else "sell"
        trades.append(Trade(NOW_MS - (n - i) * 10_000, high if side == "buy" else low, qty, side))
    return trades


def ticker(bid: float, ask: float, volume: float = 50_000) -> Ticker:
    return Ticker("ABCUSDT", (bid + ask) / 2, bid, ask, volume)
