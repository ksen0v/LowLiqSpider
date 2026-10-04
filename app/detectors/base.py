from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.models import Candle, Detection, ScanContext, Trade

P = TypeVar("P", bound=BaseModel)


class Detector(ABC, Generic[P]):
    key: str
    name: str
    short: str  # подпись маркера на графике
    needs_trades: bool = False

    @abstractmethod
    def detect(self, ctx: ScanContext, p: P) -> Detection | None: ...


def trades_in_window(trades: list[Trade], now_ms: int, minutes: float) -> list[Trade]:
    start = now_ms - minutes * 60_000
    return [t for t in trades if t.ts >= start]


def candles_in_window(candles: list[Candle], now_ms: int, minutes: float) -> list[Candle]:
    start = now_ms / 1000 - minutes * 60
    return [c for c in candles if c.time + 60 > start]


def closed_candles(candles: list[Candle], now_ms: int, step_sec: int = 60) -> list[Candle]:
    return [c for c in candles if c.time + step_sec <= now_ms / 1000]


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def fmt_price(p: float) -> str:
    if p == 0:
        return "0"
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    digits = 4
    x = p
    while x < 0.1 and digits < 12:
        x *= 10
        digits += 1
    return f"{p:.{digits}f}"


def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:.0f}"
