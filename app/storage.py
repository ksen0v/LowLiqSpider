"""SQLite: настройки, история алертов, кэш листингов ликвидных бирж."""

from __future__ import annotations

import json
import time
from pathlib import Path

import aiosqlite

from app.models import Alert

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    detector TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts);
CREATE TABLE IF NOT EXISTS liquid_cache (
    key TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    assets TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Storage.open() не вызван"
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ----------------------------------------------------------------- настройки

    async def load_settings(self) -> dict | None:
        async with self.db.execute("SELECT data FROM settings WHERE id = 1") as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def save_settings(self, data: dict) -> None:
        await self.db.execute(
            "INSERT INTO settings (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (json.dumps(data, ensure_ascii=False),),
        )
        await self.db.commit()

    # ----------------------------------------------------------------- алерты

    async def add_alert(self, alert: Alert) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO alerts (id, ts, exchange, symbol, detector, data) VALUES (?, ?, ?, ?, ?, ?)",
            (alert.id, alert.ts, alert.exchange, alert.symbol, alert.detector, alert.model_dump_json()),
        )
        await self.db.commit()

    async def list_alerts(
        self, limit: int = 200, exchange: str | None = None, detector: str | None = None
    ) -> list[Alert]:
        query = "SELECT data FROM alerts"
        where, args = [], []
        if exchange:
            where.append("exchange = ?")
            args.append(exchange)
        if detector:
            where.append("detector = ?")
            args.append(detector)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        async with self.db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [Alert.model_validate_json(r[0]) for r in rows]

    async def last_alert_times(self, since: float) -> dict[tuple[str, str, str], float]:
        """Для восстановления пауз между алертами после перезапуска."""
        async with self.db.execute(
            "SELECT exchange, symbol, detector, MAX(ts) FROM alerts WHERE ts >= ? GROUP BY exchange, symbol, detector",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    async def clear_alerts(self) -> None:
        await self.db.execute("DELETE FROM alerts")
        await self.db.commit()

    async def prune_alerts(self, older_than_days: float) -> int:
        cur = await self.db.execute("DELETE FROM alerts WHERE ts < ?", (time.time() - older_than_days * 86400,))
        await self.db.commit()
        return cur.rowcount

    # ----------------------------------------------------------------- листинги

    async def load_liquid_cache(self) -> dict[str, tuple[float, list[str]]]:
        async with self.db.execute("SELECT key, ts, assets FROM liquid_cache") as cur:
            rows = await cur.fetchall()
        return {r[0]: (r[1], json.loads(r[2])) for r in rows}

    async def save_liquid_cache(self, key: str, ts: float, assets: list[str]) -> None:
        await self.db.execute(
            "INSERT INTO liquid_cache (key, ts, assets) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET ts = excluded.ts, assets = excluded.assets",
            (key, ts, json.dumps(assets)),
        )
        await self.db.commit()
