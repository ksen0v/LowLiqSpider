"""Нормализованные рыночные данные и модель алерта.

Клиенты бирж приводят ответы API к этим структурам, детекторы работают только с ними.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field


@dataclass(slots=True)
class SymbolInfo:
    symbol: str  # нативный идентификатор пары на бирже: "ABCUSDT" (MEXC), "ABC_USDT" (Gate)
    base: str
    quote: str


@dataclass(slots=True)
class Ticker:
    symbol: str
    last: float
    bid: float | None
    ask: float | None
    quote_volume_24h: float
    change_pct_24h: float | None = None

    @property
    def spread_pct(self) -> float | None:
        if not self.bid or not self.ask or self.bid <= 0 or self.ask < self.bid:
            return None
        return (self.ask - self.bid) / self.bid * 100


@dataclass(slots=True)
class Candle:
    time: int  # время открытия свечи, unix-секунды
    open: float
    high: float
    low: float
    close: float
    volume: float  # в базовой монете
    quote_volume: float  # в котируемой (USDT)


@dataclass(slots=True)
class Trade:
    ts: int  # unix-миллисекунды
    price: float
    qty: float
    side: str  # сторона тейкера: "buy" или "sell"

    @property
    def quote(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class ScanContext:
    exchange: str
    info: SymbolInfo
    now_ms: int
    candles: list[Candle]  # 1m, по возрастанию времени, последняя может быть незакрытой
    trades: list[Trade] | None  # по возрастанию времени; None, если сделки не запрашивались
    ticker: Ticker | None = None


class Level(BaseModel):
    price: float
    label: str
    color: str = "#f0b90b"


class Alert(BaseModel):
    id: str
    ts: float = Field(description="unix-секунды")
    exchange: str
    symbol: str
    base: str
    quote: str
    detector: str
    detector_name: str
    title: str
    reason: str
    score: float | None = None
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    levels: list[Level] = Field(default_factory=list)
    url: str = ""


@dataclass(slots=True)
class Detection:
    title: str
    reason: str
    metrics: dict[str, float | int | str] = field(default_factory=dict)
    levels: list[Level] = field(default_factory=list)
    score: float | None = None
