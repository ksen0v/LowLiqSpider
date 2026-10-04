"""Gate spot, публичное REST API v4: https://www.gate.com/docs/developers/apiv4/"""

from __future__ import annotations

from typing import Any

from app.exchanges.base import ExchangeClient, to_float
from app.models import Candle, SymbolInfo, Ticker, Trade

_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h"}


def parse_symbols(data: list[dict[str, Any]]) -> dict[str, SymbolInfo]:
    out: dict[str, SymbolInfo] = {}
    for p in data:
        if p.get("trade_status") != "tradable":
            continue
        out[p["id"]] = SymbolInfo(symbol=p["id"], base=p["base"].upper(), quote=p["quote"].upper())
    return out


def parse_tickers(data: list[dict[str, Any]]) -> dict[str, Ticker]:
    out: dict[str, Ticker] = {}
    for t in data:
        last = to_float(t.get("last"))
        if not last:
            continue
        out[t["currency_pair"]] = Ticker(
            symbol=t["currency_pair"],
            last=last,
            bid=to_float(t.get("highest_bid")),
            ask=to_float(t.get("lowest_ask")),
            quote_volume_24h=to_float(t.get("quote_volume"), 0.0) or 0.0,
            change_pct_24h=to_float(t.get("change_percentage")),
        )
    return out


def parse_candles(data: list[list[Any]]) -> list[Candle]:
    # [time(sec), quote volume, close, high, low, open, base volume, closed]
    candles = [
        Candle(
            time=int(float(row[0])),
            open=float(row[5]),
            high=float(row[3]),
            low=float(row[4]),
            close=float(row[2]),
            volume=float(row[6]) if len(row) > 6 else 0.0,
            quote_volume=float(row[1]),
        )
        for row in data
    ]
    candles.sort(key=lambda c: c.time)
    return candles


def parse_trades(data: list[dict[str, Any]]) -> list[Trade]:
    trades = []
    for t in data:
        ts = to_float(t.get("create_time_ms"))
        ts_ms = int(ts) if ts else int(float(t["create_time"]) * 1000)
        side = "buy" if t.get("side") == "buy" else "sell"
        trades.append(Trade(ts=ts_ms, price=float(t["price"]), qty=float(t["amount"]), side=side))
    trades.sort(key=lambda t: t.ts)
    return trades


class GateClient(ExchangeClient):
    id = "gate"
    name = "Gate"
    base_url = "https://api.gateio.ws/api/v4"

    async def load_symbols(self) -> dict[str, SymbolInfo]:
        return parse_symbols(await self.http.get("/spot/currency_pairs", limiter=self.scan_limiter))

    async def fetch_tickers(self) -> dict[str, Ticker]:
        return parse_tickers(await self.http.get("/spot/tickers", limiter=self.scan_limiter))

    async def fetch_candles(self, symbol: str, interval: str, limit: int, ui: bool = False) -> list[Candle]:
        self.check_interval(interval)
        params = {"currency_pair": symbol, "interval": _INTERVALS[interval], "limit": min(limit, 1000)}
        return parse_candles(await self.http.get("/spot/candlesticks", params, limiter=self.limiter(ui)))

    async def fetch_trades(self, symbol: str, limit: int, ui: bool = False) -> list[Trade]:
        params = {"currency_pair": symbol, "limit": min(limit, 1000)}
        return parse_trades(await self.http.get("/spot/trades", params, limiter=self.limiter(ui)))
