"""Демо-биржа с синтетическими данными.

Включается переменной окружения SPIDER_DEMO=1. Нужна, чтобы посмотреть интерфейс и
проверить детекторы без доступа к API бирж. Сделки генерируются детерминированно
от (символ, 10-секундный бакет), поэтому свечи и лента всегда согласованы.
"""

from __future__ import annotations

import math
import random
import time
from functools import lru_cache

from app.exchanges.base import ExchangeClient
from app.models import Candle, SymbolInfo, Ticker, Trade

BUCKET_SEC = 10
_INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

# base -> (поведение, базовая цена, объём 24ч)
_MARKETS = {
    "mexc": {
        "ERSH": ("pingpong", 0.04210, 60_000),
        "PINGO": ("pingpong", 1.873, 140_000),
        "WIDE": ("spread", 0.3150, 9_000),
        "NEEDL": ("wicks", 0.00872, 25_000),
        "PUMPX": ("pump", 0.1230, 40_000),
        "BURST": ("volume", 2.410, 30_000),
        "QUIET": ("quiet", 0.5500, 4_000),
        "SLEEP": ("quiet", 12.30, 2_500),
        "LIQD": ("pingpong", 3.300, 900_000),  # «есть на Binance» — должна отфильтроваться
    },
    "gate": {
        "SAWX": ("pingpong", 0.7710, 35_000),
        "GAPY": ("spread", 0.0521, 7_000),
        "SPIKY": ("wicks", 1.120, 18_000),
        "MOONR": ("pump", 0.00341, 22_000),
        "DOZE": ("quiet", 0.2000, 3_000),
    },
}

DEMO_LIQUID_ASSETS = {"BTC", "ETH", "SOL", "LIQD"}


def _phase(t: int, period: int, active: int, shift: int = 0) -> float | None:
    """Позиция внутри активной фазы (0..1) или None, если фаза неактивна."""
    pos = (t + shift) % period
    return pos / active if pos < active else None


@lru_cache(maxsize=200_000)
def _bucket_trades(base: str, behavior: str, price0: float, bucket: int) -> tuple[Trade, ...]:
    rng = random.Random(f"{base}:{bucket}")
    t0 = bucket * BUCKET_SEC
    shift = sum(map(ord, base)) * 97
    drift = 1 + 0.015 * math.sin((t0 + shift) / 2400)
    p = price0 * drift
    out: list[Trade] = []

    def add(price: float, qty: float, side: str, offset: float | None = None) -> None:
        ts = (t0 + (offset if offset is not None else rng.random() * BUCKET_SEC)) * 1000
        out.append(Trade(ts=int(ts), price=round(price, 8), qty=round(qty, 4), side=side))

    if behavior == "pingpong":
        if _phase(t0, 2400, 1500, shift) is not None:
            size = round(150 / price0, 1)
            for i in range(rng.choice((1, 1, 2))):
                if rng.random() < 0.85:
                    add(p * 1.0045, size * (1 + rng.uniform(-0.005, 0.005)), "buy", i * 4 + 1)
                    add(p * 0.9955, size * (1 + rng.uniform(-0.005, 0.005)), "sell", i * 4 + 3)
        elif rng.random() < 0.15:
            add(p * (1 + rng.uniform(-0.003, 0.003)), rng.uniform(5, 60) / price0, rng.choice(("buy", "sell")))
    elif behavior == "spread":
        if rng.random() < 0.45:
            side = rng.choice(("buy", "sell"))
            add(p * (1.018 if side == "buy" else 0.982), rng.uniform(10, 80) / price0, side)
    elif behavior == "wicks":
        if rng.random() < 0.35:
            add(p * (1 + rng.uniform(-0.002, 0.002)), rng.uniform(10, 50) / price0, rng.choice(("buy", "sell")))
        if bucket % 6 == 0 and rng.random() < 0.35:
            up = rng.random() < 0.5
            add(p * (1.035 if up else 0.965), rng.uniform(30, 120) / price0, "buy" if up else "sell", 5)
    elif behavior == "pump":
        pos = _phase(t0, 2700, 480, shift)
        if pos is not None:
            p *= 1 + 0.14 * min(pos * 2, 1.0)
            for _ in range(rng.randint(2, 5)):
                add(
                    p * (1 + rng.uniform(-0.004, 0.004)),
                    rng.uniform(80, 600) / price0,
                    rng.choice(("buy", "buy", "sell")),
                )
        elif rng.random() < 0.25:
            add(p * (1 + rng.uniform(-0.002, 0.002)), rng.uniform(5, 40) / price0, rng.choice(("buy", "sell")))
    elif behavior == "volume":
        burst = _phase(t0, 1800, 180, shift) is not None
        n = rng.randint(4, 9) if burst else (1 if rng.random() < 0.3 else 0)
        for _ in range(n):
            add(
                p * (1 + rng.uniform(-0.003, 0.003)),
                rng.uniform(100 if burst else 5, 900 if burst else 50) / price0,
                rng.choice(("buy", "sell")),
            )
    else:  # quiet
        if rng.random() < 0.08:
            add(p * (1 + rng.uniform(-0.002, 0.002)), rng.uniform(5, 40) / price0, rng.choice(("buy", "sell")))
    out.sort(key=lambda t: t.ts)
    return tuple(out)


class DemoClient(ExchangeClient):
    base_url = ""

    def __init__(self, exchange_id: str, name: str, scan_rate: float = 10, ui_rate: float = 6):
        self.id = exchange_id
        self.name = f"{name} (демо)"
        super().__init__(scan_rate, ui_rate)
        self._markets = _MARKETS[exchange_id]

    def _symbol(self, base: str) -> str:
        return f"{base}_USDT" if self.id == "gate" else f"{base}USDT"

    def _base(self, symbol: str) -> str:
        return symbol.replace("_USDT", "").removesuffix("USDT")

    def _trades_between(self, base: str, start: int, end: int) -> list[Trade]:
        behavior, price0, _ = self._markets[base]
        trades: list[Trade] = []
        for bucket in range(start // BUCKET_SEC, end // BUCKET_SEC + 1):
            trades.extend(_bucket_trades(base, behavior, price0, bucket))
        return [t for t in trades if start * 1000 <= t.ts <= end * 1000]

    async def load_symbols(self) -> dict[str, SymbolInfo]:
        return {self._symbol(b): SymbolInfo(self._symbol(b), b, "USDT") for b in self._markets}

    async def fetch_tickers(self) -> dict[str, Ticker]:
        now = int(time.time())
        out = {}
        for base, (behavior, price0, vol24) in self._markets.items():
            recent = self._trades_between(base, now - 600, now)
            last = recent[-1].price if recent else price0
            half = {"spread": 0.018, "pingpong": 0.0045}.get(behavior, 0.0015)
            sym = self._symbol(base)
            out[sym] = Ticker(sym, last, last * (1 - half), last * (1 + half), vol24 + sum(t.quote for t in recent))
        return out

    async def fetch_candles(self, symbol: str, interval: str, limit: int, ui: bool = False) -> list[Candle]:
        self.check_interval(interval)
        step = _INTERVAL_SEC[interval]
        now = int(time.time())
        start = (now // step - limit + 1) * step
        base = self._base(symbol)
        price0 = self._markets[base][1]
        trades = self._trades_between(base, start, now)
        candles: list[Candle] = []
        prev_close = trades[0].price if trades else price0
        idx = 0
        for k in range(limit):
            t = start + k * step
            bucket: list[Trade] = []
            while idx < len(trades) and trades[idx].ts < (t + step) * 1000:
                bucket.append(trades[idx])
                idx += 1
            if bucket:
                prices = [x.price for x in bucket]
                candles.append(
                    Candle(
                        t,
                        prev_close,
                        max(prices + [prev_close]),
                        min(prices + [prev_close]),
                        prices[-1],
                        sum(x.qty for x in bucket),
                        sum(x.quote for x in bucket),
                    )
                )
                prev_close = prices[-1]
            else:
                candles.append(Candle(t, prev_close, prev_close, prev_close, prev_close, 0.0, 0.0))
        return candles

    async def fetch_trades(self, symbol: str, limit: int, ui: bool = False) -> list[Trade]:
        now = int(time.time())
        trades = self._trades_between(self._base(symbol), now - 3600, now)
        return trades[-limit:]
