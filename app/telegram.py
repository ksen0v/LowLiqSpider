"""Отправка алертов в Telegram-бота."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

import httpx

from app.models import Alert
from app.settings import TelegramSettings

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def format_alert(alert: Alert, exchange_name: str) -> str:
    when = datetime.fromtimestamp(alert.ts, tz=UTC).astimezone().strftime("%H:%M:%S")
    lines = [
        (
            f"<b>{html.escape(alert.detector_name)}</b> · {html.escape(exchange_name)} · "
            f"<b>{html.escape(alert.base)}/{html.escape(alert.quote)}</b>"
        ),
        html.escape(alert.title),
        "",
        html.escape(alert.reason),
    ]
    if alert.levels:
        lines.append("")
        lines += [f"{html.escape(lv.label)} <code>{lv.price:.10g}</code>" for lv in alert.levels]
    lines.append(f"\n<i>{when}</i>")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self):
        self._queue: asyncio.Queue[tuple[TelegramSettings, Alert, str]] = asyncio.Queue(maxsize=200)
        self._http = httpx.AsyncClient(timeout=15)
        self.last_error: str | None = None

    def enqueue(self, cfg: TelegramSettings, alert: Alert, exchange_name: str) -> None:
        if not (cfg.enabled and cfg.bot_token and cfg.chat_id):
            return
        if not getattr(cfg, alert.detector, True):
            return
        try:
            self._queue.put_nowait((cfg, alert, exchange_name))
        except asyncio.QueueFull:
            log.warning("Очередь Telegram переполнена, алерт %s пропущен", alert.id)

    async def send(self, cfg: TelegramSettings, text: str, url: str = "", button: str = "") -> None:
        payload: dict = {
            "chat_id": cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button or "Открыть", "url": url}]]}
        resp = await self._http.post(f"{API}/bot{cfg.bot_token}/sendMessage", json=payload)
        if resp.status_code == 429:
            retry = resp.json().get("parameters", {}).get("retry_after", 5)
            await asyncio.sleep(retry)
            resp = await self._http.post(f"{API}/bot{cfg.bot_token}/sendMessage", json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram {resp.status_code}: {resp.text[:200]}")

    async def run(self) -> None:
        while True:
            cfg, alert, exchange_name = await self._queue.get()
            try:
                await self.send(cfg, format_alert(alert, exchange_name), alert.url, f"Открыть на {exchange_name}")
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                # токен не должен попасть в логи и в интерфейс
                self.last_error = str(exc).replace(cfg.bot_token, "***")
                log.warning("Не удалось отправить алерт в Telegram: %s", self.last_error)
            # Telegram ограничивает ~1 сообщение в секунду в один чат
            await asyncio.sleep(1.1)

    async def aclose(self) -> None:
        await self._http.aclose()
