"""Сервис целиком: API, сканер на демо-бирже, пауза между алертами."""

import asyncio
import importlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.alerts import AlertHub, Broadcaster
from app.exchanges.demo import DemoClient
from app.liquid import LiquidRegistry
from app.scanner import ExchangeScanner
from app.settings import AppSettings
from app.storage import Storage
from app.telegram import TelegramNotifier, format_alert


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_DEMO", "1")
    monkeypatch.setenv("SPIDER_DISABLE_SCANNER", "1")
    monkeypatch.setenv("SPIDER_DB", str(tmp_path / "test.db"))
    import app.main

    main = importlib.reload(app.main)
    with TestClient(main.app) as c:
        yield c


def test_settings_roundtrip_and_validation(client):
    s = client.get("/api/settings").json()
    assert s["mexc"]["detectors"]["ping_pong"]["enabled"] is True

    s["gate"]["detectors"]["ping_pong"]["min_trades"] = 42
    s["ui"]["grid_rows"] = 2
    saved = client.put("/api/settings", json=s).json()
    assert saved["gate"]["detectors"]["ping_pong"]["min_trades"] == 42
    assert client.get("/api/settings").json()["ui"]["grid_rows"] == 2

    s["mexc"]["detectors"]["ping_pong"]["min_score"] = 5  # максимум 1
    r = client.put("/api/settings", json=s)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == "mexc.detectors.ping_pong.min_score"
    assert r.json()["detail"][0]["msg"] == "Должно быть не больше 1"

    s["mexc"]["detectors"]["ping_pong"]["min_score"] = 0.5
    s["gate"]["proxy"] = "1.2.3.4:1080"
    r = client.put("/api/settings", json=s)
    assert r.json()["detail"][0] == {
        "loc": "gate.proxy",
        "msg": "Прокси должен начинаться с http://, https://, socks5:// или socks5h://",
    }

    reset = client.post("/api/settings/reset").json()
    assert reset["ui"]["grid_rows"] == 3


def test_schema_has_russian_titles(client):
    schema = client.get("/api/settings/schema").json()
    assert schema["properties"]["mexc"]["title"] == "MEXC"
    assert schema["$defs"]["PingPongParams"]["properties"]["min_capture_pct"]["title"] == "Мин. ширина коридора, %"


def test_market_data_endpoints(client):
    meta = client.get("/api/meta").json()
    assert [e["id"] for e in meta["exchanges"]] == ["mexc", "gate"]
    candles = client.get("/api/candles", params={"exchange": "mexc", "symbol": "ERSHUSDT", "limit": 60}).json()
    assert len(candles) == 60 and {"time", "open", "high", "low", "close", "volume"} <= set(candles[0])
    trades = client.get("/api/trades", params={"exchange": "gate", "symbol": "SAWX_USDT", "limit": 10}).json()
    assert len(trades) <= 10
    assert client.get("/api/candles", params={"exchange": "nope", "symbol": "X"}).status_code == 404
    assert client.get("/api/candles", params={"exchange": "mexc", "symbol": "X", "interval": "3m"}).status_code == 400


def test_index_and_status(client):
    assert "LowLiqSpider" in client.get("/").text
    st = client.get("/api/status").json()
    assert st["demo"] is True and len(st["exchanges"]) == 2


def test_websocket_sends_status(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "status"


def test_basic_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_DEMO", "1")
    monkeypatch.setenv("SPIDER_DISABLE_SCANNER", "1")
    monkeypatch.setenv("SPIDER_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("SPIDER_AUTH_USER", "me")
    monkeypatch.setenv("SPIDER_AUTH_PASSWORD", "secret")
    import app.main

    main = importlib.reload(app.main)
    with TestClient(main.app) as c:
        assert c.get("/api/settings").status_code == 401
        assert c.get("/api/settings", auth=("me", "wrong")).status_code == 401
        assert c.get("/api/settings", auth=("me", "secret")).status_code == 200


async def test_scanner_finds_demo_patterns_and_respects_cooldown(tmp_path, monkeypatch):
    # фиксируем время там, где демо-ёрш ERSH уже 10+ минут крутит объём
    shift = sum(map(ord, "ERSH")) * 97
    t0 = 1_800_000_000
    now = t0 - (t0 + shift) % 2400 + 1000
    monkeypatch.setattr(time, "time", lambda: now)

    storage = Storage(tmp_path / "scan.db")
    await storage.open()
    settings = AppSettings()
    telegram = TelegramNotifier()
    hub = AlertHub(storage, lambda: settings, Broadcaster(), telegram, {"mexc": "MEXC"})
    client = DemoClient("mexc", "MEXC")
    scanner = ExchangeScanner(client, lambda: settings, LiquidRegistry(storage, demo_assets={"LIQD"}), hub)
    scanner.symbols = await client.load_symbols()
    scanner.tickers = await client.fetch_tickers()
    scanner._update_universe(settings.mexc)
    assert "LIQDUSDT" not in scanner.universe  # «есть на Binance»
    assert "ERSHUSDT" in scanner.universe

    fired = await scanner.scan_symbol("ERSHUSDT")
    assert any(a.detector == "ping_pong" for a in fired)
    assert fired[0].url.startswith("https://www.mexc.com/exchange/")

    # повторный скан в пределах паузы не шлёт тот же алерт
    again = await scanner.scan_symbol(fired[0].symbol)
    assert not any(a.detector == fired[0].detector for a in again)
    stored = await storage.list_alerts()
    assert {a.id for a in stored} >= {a.id for a in fired}

    text = format_alert(fired[0], "MEXC")
    assert fired[0].base in text and "<b>" in text

    await client.aclose()
    await telegram.aclose()
    await storage.close()


async def test_cooldown_restored_after_restart(tmp_path):
    storage = Storage(tmp_path / "cd.db")
    await storage.open()
    from app.models import Alert

    await storage.add_alert(
        Alert(
            id="a",
            ts=time.time() - 60,
            exchange="mexc",
            symbol="XUSDT",
            base="X",
            quote="USDT",
            detector="wicks",
            detector_name="Иглы",
            title="t",
            reason="r",
        )
    )
    settings = AppSettings()
    hub = AlertHub(storage, lambda: settings, Broadcaster(), TelegramNotifier(), {})
    await hub.restore_cooldowns()
    assert hub.in_cooldown("mexc", "XUSDT", "wicks", 15)
    assert not hub.in_cooldown("mexc", "XUSDT", "wicks", 0.5)
    assert not hub.in_cooldown("mexc", "XUSDT", "spread", 15)
    await storage.close()


class _SlowClient(DemoClient):
    async def fetch_candles(self, symbol, interval, limit, ui=False):
        await asyncio.sleep(0.2)
        return await super().fetch_candles(symbol, interval, limit, ui)


def test_more_workers_than_symbols_does_not_block_event_loop(tmp_path):
    """Регрессия: свободный воркер крутился без await, когда все монеты уже сканируются."""

    async def scenario():
        storage = Storage(tmp_path / "w.db")
        await storage.open()
        settings = AppSettings()
        hub = AlertHub(storage, lambda: settings, Broadcaster(), TelegramNotifier(), {})
        client = _SlowClient("mexc", "MEXC")
        scanner = ExchangeScanner(client, lambda: settings, LiquidRegistry(storage, demo_assets=set()), hub)
        scanner.symbols = await client.load_symbols()
        scanner.universe = {"ERSHUSDT"}
        scanner._ensure_workers(4)
        ticks = 0
        for _ in range(10):
            await asyncio.sleep(0.05)
            ticks += 1
        await scanner.stop()
        await client.aclose()
        await storage.close()
        return ticks, scanner._last_scan

    result = {}
    thread = threading.Thread(target=lambda: result.update(out=asyncio.run(scenario())), daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "event loop завис"
    ticks, last_scan = result["out"]
    assert ticks == 10
    assert "ERSHUSDT" in last_scan
