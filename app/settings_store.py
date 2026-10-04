from __future__ import annotations

import logging
from collections.abc import Callable

from pydantic import ValidationError

from app.settings import AppSettings
from app.storage import Storage

log = logging.getLogger(__name__)


class SettingsStore:
    """Текущие настройки в памяти + сохранение в SQLite. Изменения применяются на лету."""

    def __init__(self, storage: Storage):
        self._storage = storage
        self.current = AppSettings()
        self._listeners: list[Callable[[AppSettings], None]] = []

    async def load(self) -> None:
        raw = await self._storage.load_settings()
        if raw is None:
            await self._storage.save_settings(self.current.model_dump())
            return
        try:
            # неизвестные поля игнорируются, недостающие берутся по умолчанию — так старый
            # файл настроек переживает обновление сервиса
            self.current = AppSettings.model_validate(raw)
        except ValidationError:
            log.exception("Сохранённые настройки невалидны, используются значения по умолчанию")

    def subscribe(self, listener: Callable[[AppSettings], None]) -> None:
        self._listeners.append(listener)

    async def update(self, data: dict) -> AppSettings:
        settings = AppSettings.model_validate(data)
        await self._storage.save_settings(settings.model_dump())
        self.current = settings
        for listener in self._listeners:
            listener(settings)
        return settings

    async def reset(self) -> AppSettings:
        return await self.update(AppSettings().model_dump())
