"""Реестр монет, которые торгуются на ликвидных биржах (Binance, Bybit, OKX…).

Монета с MEXC/Gate, которая есть хотя бы на одной включённой ликвидной бирже, в скан не
попадает: там её цену держат арбитражники, и алгоритм на неликвиде не выживает.
Листинги грузятся через публичные API раз в несколько часов и кэшируются в SQLite, так что
после перезапуска фильтр работает сразу, даже если какая-то биржа временно недоступна.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.http import JsonClient
from app.settings import LiquidSettings

log = logging.getLogger(__name__)

_MULTIPLIER = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")
_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}


def normalize_asset(asset: str) -> str:
    a = asset.strip().upper()
    return _ALIASES.get(a, a)


def asset_variants(asset: str) -> set[str]:
    """«1000PEPE» на фьючерсах Binance — это та же PEPE, что и на споте MEXC."""
    a = normalize_asset(asset)
    return {a, _MULTIPLIER.sub("", a)}


# --------------------------------------------------------------------------- парсеры ответов


def parse_binance(data: dict[str, Any]) -> set[str]:
    return {s["baseAsset"] for s in data.get("symbols", []) if s.get("status") == "TRADING"}


def parse_bybit(data: dict[str, Any]) -> set[str]:
    return {s["baseCoin"] for s in data.get("result", {}).get("list", []) if s.get("status") == "Trading"}


def parse_okx_spot(data: dict[str, Any]) -> set[str]:
    return {s["baseCcy"] for s in data.get("data", []) if s.get("state") == "live" and s.get("baseCcy")}


def parse_okx_swap(data: dict[str, Any]) -> set[str]:
    out = set()
    for s in data.get("data", []):
        family = s.get("instFamily") or s.get("uly") or ""
        if s.get("state") == "live" and family:
            out.add(family.split("-")[0])
    return out


def parse_bitget_spot(data: dict[str, Any]) -> set[str]:
    return {s["baseCoin"] for s in data.get("data", []) if s.get("status") == "online"}


def parse_bitget_futures(data: dict[str, Any]) -> set[str]:
    return {s["baseCoin"] for s in data.get("data", []) if s.get("symbolStatus", "normal") == "normal"}


def parse_coinbase(data: list[dict[str, Any]]) -> set[str]:
    return {p["base_currency"] for p in data if p.get("status") == "online" and not p.get("trading_disabled", False)}


def parse_upbit(data: list[dict[str, Any]]) -> set[str]:
    return {m["market"].split("-", 1)[1] for m in data if "-" in m.get("market", "")}


def parse_kraken(data: dict[str, Any]) -> set[str]:
    out = set()
    for p in data.get("result", {}).values():
        ws = p.get("wsname") or ""
        if "/" in ws and p.get("status", "online") == "online":
            out.add(ws.split("/")[0])
    return out


def parse_kucoin_spot(data: dict[str, Any]) -> set[str]:
    return {s["baseCurrency"] for s in data.get("data", []) if s.get("enableTrading")}


def parse_kucoin_futures(data: dict[str, Any]) -> set[str]:
    return {s["baseCurrency"] for s in data.get("data", []) if s.get("status", "Open") == "Open"}


def parse_htx_spot(data: dict[str, Any]) -> set[str]:
    return {s["bc"] for s in data.get("data", []) if s.get("state") == "online"}


def parse_htx_futures(data: dict[str, Any]) -> set[str]:
    return {s["symbol"] for s in data.get("data", []) if s.get("contract_status") == 1}


# --------------------------------------------------------------------------- загрузчики

Fetcher = Callable[[JsonClient], Awaitable[set[str]]]


def _simple(url: str, parser: Callable[[Any], set[str]], params: dict | None = None) -> Fetcher:
    async def fetch(http: JsonClient) -> set[str]:
        return parser(await http.get(url, params))

    return fetch


async def _bybit_linear(http: JsonClient) -> set[str]:
    out: set[str] = set()
    cursor = ""
    for _ in range(20):
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = await http.get("https://api.bybit.com/v5/market/instruments-info", params)
        out |= parse_bybit(data)
        cursor = data.get("result", {}).get("nextPageCursor") or ""
        if not cursor:
            break
    return out


FETCHERS: dict[str, dict[str, Fetcher]] = {
    "binance": {
        "spot": _simple("https://api.binance.com/api/v3/exchangeInfo", parse_binance),
        "futures": _simple("https://fapi.binance.com/fapi/v1/exchangeInfo", parse_binance),
    },
    "bybit": {
        "spot": _simple("https://api.bybit.com/v5/market/instruments-info", parse_bybit, {"category": "spot"}),
        "futures": _bybit_linear,
    },
    "okx": {
        "spot": _simple("https://www.okx.com/api/v5/public/instruments", parse_okx_spot, {"instType": "SPOT"}),
        "futures": _simple("https://www.okx.com/api/v5/public/instruments", parse_okx_swap, {"instType": "SWAP"}),
    },
    "bitget": {
        "spot": _simple("https://api.bitget.com/api/v2/spot/public/symbols", parse_bitget_spot),
        "futures": _simple(
            "https://api.bitget.com/api/v2/mix/market/contracts", parse_bitget_futures, {"productType": "USDT-FUTURES"}
        ),
    },
    "coinbase": {"spot": _simple("https://api.exchange.coinbase.com/products", parse_coinbase)},
    "upbit": {"spot": _simple("https://api.upbit.com/v1/market/all", parse_upbit)},
    "kraken": {"spot": _simple("https://api.kraken.com/0/public/AssetPairs", parse_kraken)},
    "kucoin": {
        "spot": _simple("https://api.kucoin.com/api/v2/symbols", parse_kucoin_spot),
        "futures": _simple("https://api-futures.kucoin.com/api/v1/contracts/active", parse_kucoin_futures),
    },
    "htx": {
        "spot": _simple("https://api.huobi.pro/v2/settings/common/symbols", parse_htx_spot),
        "futures": _simple("https://api.hbdm.com/linear-swap-api/v1/swap_contract_info", parse_htx_futures),
    },
}

VENUE_NAMES = {
    "binance": "Binance",
    "bybit": "Bybit",
    "okx": "OKX",
    "bitget": "Bitget",
    "coinbase": "Coinbase",
    "upbit": "Upbit",
    "kraken": "Kraken",
    "kucoin": "KuCoin",
    "htx": "HTX",
}


def enabled_sources(cfg: LiquidSettings) -> list[str]:
    """Ключи вида "binance:spot" для всех включённых бирж и рынков."""
    keys = []
    for venue, markets in FETCHERS.items():
        v = getattr(cfg, venue)
        if not v.enabled:
            continue
        for market in markets:
            if getattr(v, market, True):
                keys.append(f"{venue}:{market}")
    return keys


class LiquidRegistry:
    def __init__(self, storage, demo_assets: set[str] | None = None):
        self._storage = storage
        self._demo_assets = demo_assets
        self._assets: dict[str, set[str]] = {}  # "binance:spot" -> нормализованные тикеры
        self._meta: dict[str, dict[str, Any]] = {}
        self._http = JsonClient(timeout=30)
        self.ready = asyncio.Event()
        self.wake = asyncio.Event()

    async def load_cache(self) -> None:
        for key, (ts, assets) in (await self._storage.load_liquid_cache()).items():
            self._set(key, assets, ts)

    def _set(self, key: str, assets: set[str] | list[str], ts: float) -> None:
        normalized: set[str] = set()
        for a in assets:
            normalized |= asset_variants(a)
        self._assets[key] = normalized
        self._meta[key] = {"ts": ts, "count": len(normalized), "error": None}

    def listed_on(self, base: str, cfg: LiquidSettings) -> list[str]:
        variants = asset_variants(base)
        if variants & {normalize_asset(a) for a in cfg.extra_assets}:
            return ["вручную"]
        if self._demo_assets is not None:
            return ["binance:spot"] if variants & self._demo_assets else []
        return [key for key in enabled_sources(cfg) if variants & self._assets.get(key, set())]

    def status(self, cfg: LiquidSettings) -> list[dict[str, Any]]:
        if self._demo_assets is not None:
            return [
                {"key": "demo", "name": "Демо-список", "count": len(self._demo_assets), "updated": None, "error": None}
            ]
        rows = []
        for key in enabled_sources(cfg):
            venue, market = key.split(":")
            meta = self._meta.get(key, {})
            rows.append(
                {
                    "key": key,
                    "name": f"{VENUE_NAMES[venue]} {'спот' if market == 'spot' else 'фьючерсы'}",
                    "count": meta.get("count", 0),
                    "updated": meta.get("ts"),
                    "error": meta.get("error"),
                }
            )
        return rows

    async def refresh(self, cfg: LiquidSettings, force: bool = False) -> None:
        if self._demo_assets is not None:
            self.ready.set()
            return
        max_age = cfg.refresh_hours * 3600
        now = time.time()
        stale = [k for k in enabled_sources(cfg) if force or now - (self._meta.get(k, {}).get("ts") or 0) > max_age]
        if stale:
            await asyncio.gather(*(self._refresh_one(k) for k in stale))
        self.ready.set()

    async def _refresh_one(self, key: str) -> None:
        venue, market = key.split(":")
        try:
            assets = await FETCHERS[venue][market](self._http)
            if not assets:
                raise ValueError("пустой ответ")
        except Exception as exc:  # noqa: BLE001 — любая ошибка одной биржи не должна валить остальные
            log.warning("Не удалось загрузить листинги %s: %s", key, exc)
            meta = self._meta.setdefault(key, {"ts": None, "count": 0})
            meta["error"] = str(exc)[:200]
            return
        ts = time.time()
        self._set(key, assets, ts)
        await self._storage.save_liquid_cache(key, ts, sorted(assets))
        log.info("Листинги %s: %d монет", key, len(assets))

    async def run(self, get_settings: Callable[[], LiquidSettings]) -> None:
        while True:
            try:
                await self.refresh(get_settings())
            except Exception:
                log.exception("Ошибка обновления ликвидных бирж")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=300)
            except TimeoutError:
                pass

    async def aclose(self) -> None:
        await self._http.aclose()
