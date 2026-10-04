from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from app.http import ExchangeError, JsonClient, RateLimiter
from app.models import Candle, SymbolInfo, Ticker, Trade

INTERVALS = ("1m", "5m", "15m", "1h")


def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ExchangeClient(ABC):
    """Публичное REST API спота биржи. Ключи не нужны."""

    id: str
    name: str
    base_url: str

    def __init__(self, scan_rate: float = 10, ui_rate: float = 6):
        self.http = JsonClient(self.base_url)
        # запросы сканера и интерфейса лимитируются отдельно, чтобы графики не ждали сканер
        self.scan_limiter = RateLimiter(scan_rate)
        self.ui_limiter = RateLimiter(ui_rate)
        self._ui_cache: dict[tuple, tuple[float, Any]] = {}

    @abstractmethod
    async def load_symbols(self) -> dict[str, SymbolInfo]: ...

    @abstractmethod
    async def fetch_tickers(self) -> dict[str, Ticker]: ...

    @abstractmethod
    async def fetch_candles(self, symbol: str, interval: str, limit: int, ui: bool = False) -> list[Candle]: ...

    @abstractmethod
    async def fetch_trades(self, symbol: str, limit: int, ui: bool = False) -> list[Trade]: ...

    def limiter(self, ui: bool) -> RateLimiter:
        return self.ui_limiter if ui else self.scan_limiter

    async def cached(self, key: tuple, ttl: float, factory):
        """Короткий кэш для запросов интерфейса: несколько вкладок не умножают нагрузку."""
        now = time.monotonic()
        hit = self._ui_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = await factory()
        self._ui_cache[key] = (now, value)
        if len(self._ui_cache) > 500:
            for k, (ts, _) in list(self._ui_cache.items()):
                if now - ts > 60:
                    del self._ui_cache[k]
        return value

    def check_interval(self, interval: str) -> None:
        if interval not in INTERVALS:
            raise ExchangeError(f"Неподдерживаемый интервал {interval}")

    async def aclose(self) -> None:
        await self.http.aclose()
