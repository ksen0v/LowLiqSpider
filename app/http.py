"""HTTP-клиент с ограничением частоты запросов и повторами."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "LowLiqSpider/1.0 (+https://github.com/ksen0v/LowLiqSpider)"


class ExchangeError(Exception):
    pass


class RateLimiter:
    """Простой лимитер: не чаще `rate` запросов в секунду. rate можно менять на лету."""

    def __init__(self, rate: float):
        self.rate = rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = max(now, self._next) + 1.0 / max(self.rate, 0.01)


class JsonClient:
    def __init__(self, base_url: str = "", timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limiter: RateLimiter | None = None,
        retries: int = 2,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            if limiter is not None:
                await limiter.acquire()
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = ExchangeError(f"HTTP {resp.status_code} {path}")
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * (attempt + 1)
                    await asyncio.sleep(min(delay, 30))
                    continue
                if resp.status_code >= 400:
                    raise ExchangeError(f"HTTP {resp.status_code} {path}: {resp.text[:200]}")
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ExchangeError(f"Некорректный JSON от {path}") from exc
            await asyncio.sleep(0.5 * (attempt + 1))
        raise ExchangeError(f"{path}: {last_exc}")

    async def aclose(self) -> None:
        await self._client.aclose()
