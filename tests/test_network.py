"""Сетевые ошибки должны быть понятными, а циклы сканера — переживать любые сбои."""

import asyncio

import httpx
import pytest

from app.alerts import AlertHub, Broadcaster
from app.exchanges.demo import DemoClient
from app.http import ExchangeError, JsonClient, describe_http_error
from app.liquid import LiquidRegistry
from app.scanner import ExchangeScanner
from app.settings import AppSettings
from app.storage import Storage
from app.telegram import TelegramNotifier


def _client(handler) -> JsonClient:
    return JsonClient("https://api.gateio.ws/api/v4", transport=httpx.MockTransport(handler))


def test_empty_httpx_errors_get_type_and_hint():
    text = describe_http_error(httpx.ConnectError(""))
    assert text.startswith("ConnectError: ") and "блокировка" in text
    assert describe_http_error(httpx.ConnectTimeout("")).startswith("ConnectTimeout: не удалось подключиться")
    assert describe_http_error(httpx.ReadError("")).startswith("ReadError: соединение оборвалось")


async def test_connection_error_names_url_and_cause():
    def handler(request):
        raise httpx.ConnectError("", request=request)

    client = _client(handler)
    with pytest.raises(ExchangeError) as err:
        await client.get("/spot/currency_pairs", retries=0)
    msg = str(err.value)
    assert msg.startswith("https://api.gateio.ws/api/v4/spot/currency_pairs → ConnectError")
    await client.aclose()


async def test_geo_block_status_has_hint():
    client = _client(lambda request: httpx.Response(403, text="Forbidden"))
    with pytest.raises(ExchangeError, match="403 .*блокирует ваш регион"):
        await client.get("/spot/tickers")
    await client.aclose()


async def test_ok_response_and_proxy_masked_in_errors():
    client = _client(lambda request: httpx.Response(200, json=[{"id": "A_USDT"}]))
    assert await client.get("/spot/currency_pairs") == [{"id": "A_USDT"}]
    await client.aclose()

    def handler(request):
        raise httpx.ConnectTimeout("", request=request)

    proxied = JsonClient("https://x.test", proxy="http://user:secret@10.0.0.1:3128")
    await proxied.aclose()
    # с прокси httpx не использует подставной транспорт, поэтому подменяем сам клиент
    proxied._client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler))
    with pytest.raises(ExchangeError) as err:
        await proxied.get("/ping", retries=0)
    assert "через прокси http://***@10.0.0.1:3128" in str(err.value)
    assert "secret" not in str(err.value)
    await proxied.aclose()


async def test_client_switches_proxy_on_the_fly():
    client = DemoClient("gate", "Gate")
    first = client.http
    client.set_proxy("socks5://127.0.0.1:1080")
    assert client.http is not first and client.http.proxy == "socks5://127.0.0.1:1080"
    same = client.http
    client.set_proxy("socks5://127.0.0.1:1080")
    assert client.http is same
    await client.aclose()


def test_proxy_setting_is_validated():
    with pytest.raises(ValueError, match="Прокси должен начинаться"):
        AppSettings.model_validate({"gate": {"proxy": "127.0.0.1:1080"}})
    assert AppSettings.model_validate({"gate": {"proxy": " http://h:1 "}}).gate.proxy == "http://h:1"


class _FlakyClient(DemoClient):
    """Первый запрос списка пар падает с неожиданной ошибкой парсинга."""

    calls = 0

    async def load_symbols(self):
        self.calls += 1
        if self.calls == 1:
            raise KeyError("id")
        return await super().load_symbols()


async def test_symbols_loop_survives_unexpected_errors(tmp_path):
    storage = Storage(tmp_path / "s.db")
    await storage.open()
    settings = AppSettings()
    hub = AlertHub(storage, lambda: settings, Broadcaster(), TelegramNotifier(), {})
    client = _FlakyClient("gate", "Gate")
    scanner = ExchangeScanner(client, lambda: settings, LiquidRegistry(storage, demo_assets=set()), hub)
    scanner.ERROR_RETRY_SEC = 0.01
    task = asyncio.create_task(scanner._symbols_loop())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if scanner.symbols:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert scanner.symbols, "цикл должен был повторить запрос после KeyError"
    assert scanner.last_error == "KeyError: 'id'"
    await client.aclose()
    await storage.close()


async def test_settings_change_wakes_retry(tmp_path):
    storage = Storage(tmp_path / "w.db")
    await storage.open()
    settings = AppSettings()
    hub = AlertHub(storage, lambda: settings, Broadcaster(), TelegramNotifier(), {})
    client = _FlakyClient("gate", "Gate")
    scanner = ExchangeScanner(client, lambda: settings, LiquidRegistry(storage, demo_assets=set()), hub)
    scanner.ERROR_RETRY_SEC = 60  # без пробуждения повтор был бы через минуту
    task = asyncio.create_task(scanner._symbols_loop())
    await asyncio.sleep(0.05)
    assert not scanner.symbols
    scanner.wake()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if scanner.symbols:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert scanner.symbols
    await client.aclose()
    await storage.close()
