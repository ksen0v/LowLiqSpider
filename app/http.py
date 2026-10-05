"""HTTP-клиент с ограничением частоты запросов, повторами и прокси."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "LowLiqSpider/1.0 (+https://github.com/ksen0v/LowLiqSpider)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks5h://")


class ExchangeError(Exception):
    pass


def describe_http_error(exc: httpx.HTTPError, via_proxy: bool = False) -> str:
    """Человеческое описание сетевой ошибки.

    Многие исключения httpx приходят с пустым текстом (таймаут, сброс соединения),
    поэтому всегда пишем тип и подсказку, что это обычно значит.
    """
    if isinstance(exc, httpx.ProxyError):
        hint = "ошибка прокси: проверьте адрес, логин и пароль прокси"
    elif via_proxy and isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        hint = "не удалось подключиться к прокси или через него: проверьте, что прокси запущен и адрес верный"
    elif isinstance(exc, httpx.ConnectTimeout):
        hint = "не удалось подключиться за 10 с: хост недоступен из вашей сети (блокировка провайдера, файрвол)"
    elif isinstance(exc, httpx.ConnectError):
        hint = (
            "соединение не установлено: DNS не находит адрес, соединение сбрасывается "
            "(часто так выглядит блокировка у провайдера) или нет интернета"
        )
    elif isinstance(exc, httpx.TimeoutException):
        hint = "биржа не ответила вовремя"
    elif isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError)):
        hint = "соединение оборвалось во время запроса"
    else:
        hint = ""
    detail = str(exc).strip()
    parts = [type(exc).__name__]
    if detail:
        parts.append(detail)
    if hint:
        parts.append(hint)
    return ": ".join(parts)


def describe_status(status: int) -> str:
    if status in (403, 451):
        return "доступ запрещён: сервер блокирует ваш регион или IP — нужен прокси или VPN"
    if status == 429:
        return "слишком много запросов — уменьшите «Лимит запросов в секунду»"
    return ""


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
    def __init__(
        self,
        base_url: str = "",
        timeout: httpx.Timeout | float = TIMEOUT,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.proxy = proxy or None
        try:
            # без явного прокси httpx берёт HTTPS_PROXY / HTTP_PROXY из окружения
            self._client = httpx.AsyncClient(
                base_url=base_url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                follow_redirects=True,
                proxy=self.proxy,
                transport=transport,
            )
        except ImportError as exc:  # socks5 без пакета socksio
            raise ExchangeError(
                f"Прокси {_mask(self.proxy)}: {exc}. Выполните pip install -r requirements.txt"
            ) from exc
        except ValueError as exc:
            raise ExchangeError(f"Некорректный прокси {_mask(self.proxy)}: {exc}") from exc

    def url(self, path: str) -> str:
        # так же, как httpx склеивает base_url и путь: /api/v4 + /spot/tickers
        base = str(self._client.base_url)
        return f"{base.rstrip('/')}/{path.lstrip('/')}" if base else path

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limiter: RateLimiter | None = None,
        retries: int = 2,
    ) -> Any:
        url = self.url(path)
        last_error = ""
        for attempt in range(retries + 1):
            if limiter is not None:
                await limiter.acquire()
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = describe_http_error(exc, via_proxy=bool(self.proxy))
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    hint = describe_status(resp.status_code)
                    last_error = f"HTTP {resp.status_code}" + (f": {hint}" if hint else "")
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * (attempt + 1)
                    await asyncio.sleep(min(delay, 30))
                    continue
                if resp.status_code >= 400:
                    hint = describe_status(resp.status_code)
                    hint = f" ({hint})" if hint else ""
                    raise ExchangeError(f"{url} → HTTP {resp.status_code}{hint}: {resp.text[:200]}")
                try:
                    return resp.json()
                except ValueError as exc:
                    raise ExchangeError(f"{url} → ответ не JSON: {resp.text[:120]!r}") from exc
            await asyncio.sleep(0.5 * (attempt + 1))
        via = f" (через прокси {_mask(self.proxy)})" if self.proxy else ""
        raise ExchangeError(f"{url}{via} → {last_error}")

    async def aclose(self) -> None:
        await self._client.aclose()


_retiring: set[asyncio.Task] = set()


def retire_later(client: JsonClient, delay: float = 60.0) -> None:
    """Закрыть клиент чуть позже: на нём могут ещё идти запросы, начатые до смены прокси."""

    async def close() -> None:
        await asyncio.sleep(delay)
        await client.aclose()

    try:
        task = asyncio.get_running_loop().create_task(close())
    except RuntimeError:  # нет запущенного цикла (тесты, выход) — просто бросаем клиент
        return
    _retiring.add(task)
    task.add_done_callback(_retiring.discard)


def _mask(proxy: str | None) -> str:
    """Не показываем пароль прокси в логах и интерфейсе."""
    if not proxy:
        return ""
    try:
        u = httpx.URL(proxy)
    except Exception:  # noqa: BLE001
        return "***"
    host = f"{u.host}:{u.port}" if u.port else u.host
    return f"{u.scheme}://{'***@' if u.userinfo else ''}{host}"
