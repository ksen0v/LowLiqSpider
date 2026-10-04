"""LowLiqSpider: скринер ершей, всплесков объёма и резких движений на неликвидных монетах MEXC и Gate.

Запуск: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.alerts import AlertHub, Broadcaster
from app.detectors import DETECTORS
from app.exchanges import NAMES, create_client
from app.exchanges.base import INTERVALS
from app.exchanges.demo import DEMO_LIQUID_ASSETS
from app.http import ExchangeError
from app.liquid import LiquidRegistry
from app.models import Alert, Level, SymbolInfo
from app.scanner import ExchangeScanner, pair_url
from app.settings import EXCHANGE_IDS, AppSettings
from app.settings_store import SettingsStore
from app.storage import Storage
from app.telegram import TelegramNotifier, format_alert

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

DEMO = os.getenv("SPIDER_DEMO", "") == "1"
DB_PATH = os.getenv("SPIDER_DB", str(ROOT / "data" / "spider.db"))
AUTH_USER = os.getenv("SPIDER_AUTH_USER", "")
AUTH_PASSWORD = os.getenv("SPIDER_AUTH_PASSWORD", "")
SCANNER_ENABLED = os.getenv("SPIDER_DISABLE_SCANNER", "") != "1"

logging.basicConfig(
    level=os.getenv("SPIDER_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
# httpx пишет полный URL каждого запроса, а в URL Telegram лежит токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("spider")


class Services:
    storage: Storage
    settings: SettingsStore
    liquid: LiquidRegistry
    broadcaster: Broadcaster
    telegram: TelegramNotifier
    hub: AlertHub
    scanners: dict[str, ExchangeScanner]
    started_at: float


svc = Services()


async def _status_loop() -> None:
    while True:
        await asyncio.sleep(3)
        with contextlib.suppress(Exception):
            await svc.broadcaster.send({"type": "status", "status": build_status()})


async def _prune_loop() -> None:
    while True:
        with contextlib.suppress(Exception):
            await svc.storage.prune_alerts(svc.settings.current.ui.alert_retention_days)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc.started_at = time.time()
    svc.storage = Storage(DB_PATH)
    await svc.storage.open()
    svc.settings = SettingsStore(svc.storage)
    await svc.settings.load()

    svc.liquid = LiquidRegistry(svc.storage, demo_assets=DEMO_LIQUID_ASSETS if DEMO else None)
    await svc.liquid.load_cache()
    svc.settings.subscribe(lambda _: svc.liquid.wake.set())

    svc.broadcaster = Broadcaster()
    svc.telegram = TelegramNotifier()
    svc.hub = AlertHub(svc.storage, lambda: svc.settings.current, svc.broadcaster, svc.telegram, NAMES)
    await svc.hub.restore_cooldowns()

    svc.scanners = {}
    for ex in EXCHANGE_IDS:
        client = create_client(ex, DEMO, svc.settings.current.exchange(ex).scan.max_requests_per_sec)
        svc.scanners[ex] = ExchangeScanner(client, lambda: svc.settings.current, svc.liquid, svc.hub)

    tasks = [
        asyncio.create_task(svc.telegram.run(), name="telegram"),
        asyncio.create_task(_status_loop(), name="status"),
        asyncio.create_task(_prune_loop(), name="prune"),
    ]
    if SCANNER_ENABLED:
        tasks.append(asyncio.create_task(svc.liquid.run(lambda: svc.settings.current.liquid), name="liquid"))
        for scanner in svc.scanners.values():
            scanner.start()
    log.info("LowLiqSpider запущен%s", " в демо-режиме" if DEMO else "")
    try:
        yield
    finally:
        for scanner in svc.scanners.values():
            await scanner.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for scanner in svc.scanners.values():
            await scanner.client.aclose()
        await svc.liquid.aclose()
        await svc.telegram.aclose()
        await svc.storage.close()


app = FastAPI(title="LowLiqSpider", lifespan=lifespan)


def _authorized(header: str) -> bool:
    if not AUTH_USER:
        return True
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(password, AUTH_PASSWORD)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Необязательная защита паролем, если сервис открыт в интернет (SPIDER_AUTH_USER/PASSWORD)."""
    if _authorized(request.headers.get("authorization", "")):
        return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="LowLiqSpider"'})


# --------------------------------------------------------------------------- статус


def build_status() -> dict:
    cfg = svc.settings.current
    return {
        "demo": DEMO,
        "uptime": round(time.time() - svc.started_at),
        "exchanges": [s.status() for s in svc.scanners.values()],
        "liquid": svc.liquid.status(cfg.liquid),
        "telegram": {
            "enabled": cfg.telegram.enabled,
            "configured": bool(cfg.telegram.bot_token and cfg.telegram.chat_id),
            "last_error": svc.telegram.last_error,
        },
        "clients": svc.broadcaster.count,
        "alerts_total": svc.hub.total,
    }


@app.get("/api/status")
async def get_status():
    return build_status()


@app.get("/api/meta")
async def get_meta():
    return {
        "demo": DEMO,
        "exchanges": [{"id": ex, "name": NAMES[ex]} for ex in EXCHANGE_IDS],
        "detectors": [{"key": d.key, "name": d.name, "short": d.short} for d in DETECTORS],
        "intervals": list(INTERVALS),
    }


# --------------------------------------------------------------------------- настройки


_ERRORS_RU = {
    "greater_than_equal": "Должно быть не меньше {ge}",
    "less_than_equal": "Должно быть не больше {le}",
    "greater_than": "Должно быть больше {gt}",
    "less_than": "Должно быть меньше {lt}",
    "float_parsing": "Введите число",
    "float_type": "Введите число",
    "int_parsing": "Введите целое число",
    "int_type": "Введите целое число",
    "int_from_float": "Введите целое число",
    "literal_error": "Недопустимое значение",
    "bool_type": "Ожидается да/нет",
    "string_type": "Ожидается текст",
}


def _error_text(err: dict) -> str:
    template = _ERRORS_RU.get(err["type"])
    if template is None:
        return err["msg"]
    ctx = {k: f"{v:g}" if isinstance(v, float) else v for k, v in err.get("ctx", {}).items()}
    try:
        return template.format(**ctx)
    except (KeyError, IndexError):
        return err["msg"]


@app.get("/api/settings")
async def get_settings():
    return svc.settings.current.model_dump()


@app.get("/api/settings/schema")
async def get_settings_schema():
    return AppSettings.model_json_schema()


@app.put("/api/settings")
async def put_settings(data: dict):
    try:
        settings = await svc.settings.update(data)
    except ValidationError as exc:
        errors = [{"loc": ".".join(map(str, e["loc"])), "msg": _error_text(e)} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})
    return settings.model_dump()


@app.post("/api/settings/reset")
async def reset_settings():
    return (await svc.settings.reset()).model_dump()


# --------------------------------------------------------------------------- алерты


@app.get("/api/alerts")
async def get_alerts(limit: int = Query(200, ge=1, le=2000), exchange: str | None = None, detector: str | None = None):
    return [a.model_dump() for a in await svc.storage.list_alerts(limit, exchange, detector)]


@app.delete("/api/alerts")
async def delete_alerts():
    await svc.storage.clear_alerts()
    return {"ok": True}


# --------------------------------------------------------------------------- рыночные данные для графиков


def _scanner(exchange: str) -> ExchangeScanner:
    scanner = svc.scanners.get(exchange)
    if scanner is None:
        raise HTTPException(404, f"Неизвестная биржа {exchange}")
    return scanner


@app.get("/api/candles")
async def get_candles(exchange: str, symbol: str, interval: str = "1m", limit: int = Query(180, ge=10, le=1000)):
    client = _scanner(exchange).client
    if interval not in INTERVALS:
        raise HTTPException(400, f"Интервал должен быть одним из {', '.join(INTERVALS)}")
    try:
        candles = await client.cached(
            ("candles", symbol, interval, limit), 2.0, lambda: client.fetch_candles(symbol, interval, limit, ui=True)
        )
    except ExchangeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return [
        {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.quote_volume}
        for c in candles
    ]


@app.get("/api/trades")
async def get_trades(exchange: str, symbol: str, limit: int = Query(40, ge=1, le=500)):
    client = _scanner(exchange).client
    try:
        trades = await client.cached(
            ("trades", symbol, limit), 2.0, lambda: client.fetch_trades(symbol, limit, ui=True)
        )
    except ExchangeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return [{"ts": t.ts, "price": t.price, "qty": t.qty, "side": t.side} for t in reversed(trades)]


@app.get("/api/symbols")
async def search_symbols(exchange: str, q: str = "", limit: int = Query(20, ge=1, le=100)):
    """Поиск пары, чтобы вручную открыть её в сетке."""
    scanner = _scanner(exchange)
    query = q.strip().upper()
    cfg = svc.settings.current
    out = []
    for sym, info in scanner.symbols.items():
        if query and query not in info.base and query not in sym.upper():
            continue
        ticker = scanner.tickers.get(sym)
        out.append(
            {
                "symbol": sym,
                "base": info.base,
                "quote": info.quote,
                "volume_24h": ticker.quote_volume_24h if ticker else None,
                "in_universe": sym in scanner.universe,
                "listed_on": svc.liquid.listed_on(info.base, cfg.liquid),
            }
        )
    out.sort(key=lambda r: (not r["base"].startswith(query), r["base"]))
    return out[:limit]


@app.get("/api/pair-url")
async def get_pair_url(exchange: str, symbol: str):
    scanner = _scanner(exchange)
    info = scanner.symbols.get(symbol) or SymbolInfo(symbol, symbol, "")
    return {"url": pair_url(svc.settings.current.exchange(exchange).pair_url_template, info)}


@app.get("/api/liquid/check")
async def liquid_check(base: str):
    return {"base": base.upper(), "listed_on": svc.liquid.listed_on(base, svc.settings.current.liquid)}


@app.post("/api/liquid/refresh")
async def liquid_refresh():
    await svc.liquid.refresh(svc.settings.current.liquid, force=True)
    return svc.liquid.status(svc.settings.current.liquid)


@app.post("/api/telegram/test")
async def telegram_test():
    cfg = svc.settings.current.telegram
    if not (cfg.bot_token and cfg.chat_id):
        raise HTTPException(400, "Укажите токен бота и chat_id и сохраните настройки")
    sample = Alert(
        id="test",
        ts=time.time(),
        exchange="mexc",
        symbol="TESTUSDT",
        base="TEST",
        quote="USDT",
        detector="ping_pong",
        detector_name="Тестовое сообщение",
        title="LowLiqSpider подключён",
        reason="Если вы видите это сообщение, алерты будут приходить сюда.",
        levels=[Level(price=0.01234, label="Покупать ~"), Level(price=0.01247, label="Продавать ~")],
    )
    try:
        await svc.telegram.send(cfg, format_alert(sample, "MEXC"))
    except Exception as exc:
        raise HTTPException(502, str(exc).replace(cfg.bot_token, "***")) from exc
    return {"ok": True}


# --------------------------------------------------------------------------- websocket и статика


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    # HTTP-middleware не видит websocket-запросы, поэтому пароль проверяем здесь
    if not _authorized(ws.headers.get("authorization", "")):
        await ws.close(code=1008)
        return
    await svc.broadcaster.connect(ws)
    try:
        await ws.send_json({"type": "status", "status": build_status()})
        while True:
            await ws.receive_text()  # клиент ничего не шлёт, просто держим соединение
    except WebSocketDisconnect:
        pass
    finally:
        svc.broadcaster.disconnect(ws)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
