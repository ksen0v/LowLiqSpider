"""Приём алертов от сканеров: пауза между повторами, запись в историю, рассылка в браузер и Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

from fastapi import WebSocket

from app.models import Alert
from app.settings import AppSettings
from app.storage import Storage
from app.telegram import TelegramNotifier

log = logging.getLogger(__name__)


class Broadcaster:
    """Все открытые вкладки браузера, подключённые по WebSocket."""

    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def send(self, message: dict) -> None:
        if not self._clients:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self._clients):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=5)
            except Exception:  # noqa: BLE001 — вкладку закрыли или она зависла
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


class AlertHub:
    def __init__(
        self,
        storage: Storage,
        get_settings: Callable[[], AppSettings],
        broadcaster: Broadcaster,
        telegram: TelegramNotifier,
        exchange_names: dict[str, str],
    ):
        self._storage = storage
        self._get_settings = get_settings
        self._broadcaster = broadcaster
        self._telegram = telegram
        self._names = exchange_names
        self._last: dict[tuple[str, str, str], float] = {}
        self.total = 0

    async def restore_cooldowns(self) -> None:
        self._last = await self._storage.last_alert_times(time.time() - 86400)

    def in_cooldown(self, exchange: str, symbol: str, detector: str, cooldown_min: float) -> bool:
        last = self._last.get((exchange, symbol, detector))
        return last is not None and time.time() - last < cooldown_min * 60

    async def submit(self, alert: Alert) -> bool:
        settings = self._get_settings()
        cooldown = settings.exchange(alert.exchange).alert_cooldown_min
        if self.in_cooldown(alert.exchange, alert.symbol, alert.detector, cooldown):
            return False
        self._last[(alert.exchange, alert.symbol, alert.detector)] = alert.ts
        self.total += 1
        log.info("Алерт %s %s %s: %s", alert.exchange, alert.symbol, alert.detector, alert.title)
        await self._storage.add_alert(alert)
        await self._broadcaster.send({"type": "alert", "alert": alert.model_dump()})
        self._telegram.enqueue(settings.telegram, alert, self._names.get(alert.exchange, alert.exchange))
        return True
