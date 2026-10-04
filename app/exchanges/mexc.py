"""MEXC spot, публичное REST API v3: https://mexcdevelop.github.io/apidocs/spot_v3_en/"""

from __future__ import annotations

from typing import Any

from app.exchanges.base import ExchangeClient, to_float
from app.models import Candle, SymbolInfo, Ticker, Trade

_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "60m"}
_ONLINE = {"1", "ENABLED", "TRADING"}


def parse_symbols(data: dict[str, Any]) -> dict[str, SymbolInfo]:
    out: dict[str, SymbolInfo] = {}
    for s in data.get("symbols", []):
        if str(s.get("status", "")) not in _ONLINE:
            continue
        if s.get("isSpotTradingAllowed") is False:
            continue
        out[s["symbol"]] = SymbolInfo(symbol=s["symbol"], base=s["baseAsset"].upper(), quote=s["quoteAsset"].upper())
    return out


def parse_tickers(data: list[dict[str, Any]]) -> dict[str, Ticker]:
    out: dict[str, Ticker] = {}
    for t in data:
        last = to_float(t.get("lastPrice"))
        if not last:
            continue
        quote_vol = to_float(t.get("quoteVolume"))
        if quote_vol is None:
            quote_vol = (to_float(t.get("volume"), 0.0) or 0.0) * last
        open_price = to_float(t.get("openPrice"))
        change = (last / open_price - 1) * 100 if open_price else None
        out[t["symbol"]] = Ticker(
            symbol=t["symbol"],
            last=last,
            bid=to_float(t.get("bidPrice")),
            ask=to_float(t.get("askPrice")),
            quote_volume_24h=quote_vol,
            change_pct_24h=change,
        )
    return out


def parse_candles(data: list[list[Any]]) -> list[Candle]:
    # [openTime, open, high, low, close, volume, closeTime, quoteVolume]
    candles = [
        Candle(
            time=int(row[0]) // 1000,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]) if len(row) > 7 and row[7] is not None else float(row[5]) * float(row[4]),
        )
        for row in data
    ]
    candles.sort(key=lambda c: c.time)
    return candles


def parse_trades(data: list[dict[str, Any]]) -> list[Trade]:
    trades = []
    for t in data:
        side = "sell" if t.get("isBuyerMaker") else "buy"
        trades.append(Trade(ts=int(t["time"]), price=float(t["price"]), qty=float(t["qty"]), side=side))
    trades.sort(key=lambda t: t.ts)
    return trades


class MexcClient(ExchangeClient):
    id = "mexc"
    name = "MEXC"
    base_url = "https://api.mexc.com"

    async def load_symbols(self) -> dict[str, SymbolInfo]:
        return parse_symbols(await self.http.get("/api/v3/exchangeInfo", limiter=self.scan_limiter))

    async def fetch_tickers(self) -> dict[str, Ticker]:
        return parse_tickers(await self.http.get("/api/v3/ticker/24hr", limiter=self.scan_limiter))

    async def fetch_candles(self, symbol: str, interval: str, limit: int, ui: bool = False) -> list[Candle]:
        self.check_interval(interval)
        params = {"symbol": symbol, "interval": _INTERVALS[interval], "limit": min(limit, 1000)}
        return parse_candles(await self.http.get("/api/v3/klines", params, limiter=self.limiter(ui)))

    async def fetch_trades(self, symbol: str, limit: int, ui: bool = False) -> list[Trade]:
        params = {"symbol": symbol, "limit": min(limit, 1000)}
        return parse_trades(await self.http.get("/api/v3/trades", params, limiter=self.limiter(ui)))
