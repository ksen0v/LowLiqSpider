"""Проверка доступа к API бирж и Telegram из вашей сети.

    python -m app.diag                 # с прокси из сохранённых настроек
    python -m app.diag --proxy socks5://127.0.0.1:1080

Для каждого API проверяется DNS и HTTPS-запрос. Ответ 2xx/3xx — API доступно, 403/451 — биржа
блокирует ваш регион или IP, ошибка соединения или таймаут — хост недоступен из вашей сети.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.http import TIMEOUT, USER_AGENT, _mask, describe_http_error, describe_status
from app.settings import AppSettings

ROOT = Path(__file__).resolve().parent.parent

# (название, URL, чей прокси использовать: mexc / gate / liquid / none)
CHECKS = [
    ("MEXC", "https://api.mexc.com/api/v3/ping", "mexc"),
    ("Gate", "https://api.gateio.ws/api/v4/spot/currency_pairs/BTC_USDT", "gate"),
    ("Binance спот", "https://api.binance.com/api/v3/ping", "liquid"),
    ("Binance фьючерсы", "https://fapi.binance.com/fapi/v1/ping", "liquid"),
    ("Bybit", "https://api.bybit.com/v5/market/time", "liquid"),
    ("OKX", "https://www.okx.com/api/v5/public/time", "liquid"),
    ("Bitget", "https://api.bitget.com/api/v2/public/time", "liquid"),
    ("Coinbase", "https://api.exchange.coinbase.com/time", "liquid"),
    ("Upbit", "https://api.upbit.com/v1/ticker?markets=KRW-BTC", "liquid"),
    ("Kraken", "https://api.kraken.com/0/public/Time", "liquid"),
    ("KuCoin", "https://api.kucoin.com/api/v1/timestamp", "liquid"),
    ("KuCoin фьючерсы", "https://api-futures.kucoin.com/api/v1/timestamp", "liquid"),
    ("HTX", "https://api.huobi.pro/v1/common/timestamp", "liquid"),
    ("HTX фьючерсы", "https://api.hbdm.com/api/v1/timestamp", "liquid"),
    ("Telegram", "https://api.telegram.org/", "none"),
]


def load_settings() -> AppSettings:
    path = Path(os.getenv("SPIDER_DB", str(ROOT / "data" / "spider.db")))
    if not path.exists():
        return AppSettings()
    try:
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT data FROM settings WHERE id = 1").fetchone()
        return AppSettings.model_validate(json.loads(row[0])) if row else AppSettings()
    except Exception:  # noqa: BLE001 — диагностика должна работать и без базы
        return AppSettings()


async def resolve(host: str) -> str:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"DNS: ошибка ({exc})"
    ips = sorted({info[4][0] for info in infos})
    return "DNS: " + ", ".join(ips[:3])


async def check(name: str, url: str, proxy: str) -> tuple[bool, str]:
    host = urlsplit(url).hostname or ""
    dns = await resolve(host)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, proxy=proxy or None, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        elapsed = time.monotonic() - started
        return False, f"{elapsed:5.1f} с  {describe_http_error(exc, via_proxy=bool(proxy))}  [{dns}]"
    except ImportError as exc:
        return False, f"прокси {_mask(proxy)}: {exc}. Выполните pip install -r requirements.txt"
    elapsed = time.monotonic() - started
    if resp.status_code < 400:
        return True, f"{elapsed:5.1f} с  HTTP {resp.status_code}  [{dns}]"
    hint = describe_status(resp.status_code) or resp.text[:80].replace("\n", " ")
    return False, f"{elapsed:5.1f} с  HTTP {resp.status_code}: {hint}  [{dns}]"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка доступа к API бирж")
    parser.add_argument("--proxy", default=None, help="прокси для всех проверок, перекрывает настройки")
    args = parser.parse_args()

    settings = load_settings()
    proxies = {
        "mexc": settings.mexc.proxy,
        "gate": settings.gate.proxy,
        "liquid": settings.liquid.proxy,
        "none": "",
    }
    if args.proxy is not None:
        proxies = dict.fromkeys(proxies, args.proxy)
    env_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if env_proxy:
        print(f"Переменная окружения HTTPS_PROXY: {_mask(env_proxy)} (используется там, где прокси не задан)\n")

    results = await asyncio.gather(*(check(name, url, proxies[group]) for name, url, group in CHECKS))
    failed = []
    for (name, _, group), (ok, text) in zip(CHECKS, results, strict=True):
        via = f"  через {_mask(proxies[group])}" if proxies[group] else ""
        print(f"{'OK  ' if ok else 'FAIL'}  {name:<17}{text}{via}")
        if not ok:
            failed.append(name)

    print()
    if not failed:
        print("Все API доступны.")
        return
    print("Недоступны: " + ", ".join(failed))
    if "Gate" in failed or "MEXC" in failed:
        print(
            "- Для MEXC/Gate укажите прокси в интерфейсе: Настройки → вкладка биржи → «Прокси для API биржи» "
            "(или включите VPN). Проверить прокси заранее: python -m app.diag --proxy <адрес>"
        )
    if set(failed) - {"MEXC", "Gate", "Telegram"}:
        print(
            "- Недоступную ликвидную биржу либо пустите через прокси (Настройки → Ликвидные биржи → прокси), "
            "либо выключите там же: иначе её монеты не будут отфильтрованы."
        )
    if "Telegram" in failed:
        print("- Telegram недоступен: алерты в бота отправляться не будут.")


if __name__ == "__main__":
    asyncio.run(main())
