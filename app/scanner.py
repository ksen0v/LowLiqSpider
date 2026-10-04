"""Сканер одной биржи.

Два уровня:
1. Тикеры (один запрос на все пары раз в несколько секунд): цена, спред, объём 24ч.
   По ним строится список монет для скана и находятся «горячие» монеты с резким движением.
2. Глубокий скан монеты: 1m-свечи + лента сделок, по ним работают детекторы.
   Монеты обходятся по кругу, горячие — вне очереди.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Callable

from app.alerts import AlertHub
from app.detectors import DETECTORS
from app.detectors.base import candles_in_window
from app.exchanges.base import ExchangeClient
from app.http import ExchangeError
from app.liquid import LiquidRegistry, normalize_asset
from app.models import Alert, Candle, Detection, ScanContext, SymbolInfo, Ticker
from app.settings import AppSettings, DetectorsSettings, ExchangeSettings, UniverseSettings

log = logging.getLogger(__name__)

_LEVERAGED = re.compile(r"^[A-Z0-9]+?[2-9](L|S)$")
HOT_MIN_RESCAN_SEC = 10


def build_universe(
    symbols: dict[str, SymbolInfo],
    tickers: dict[str, Ticker],
    cfg: UniverseSettings,
    listed_on: Callable[[str], list[str]],
) -> tuple[list[str], dict[str, int]]:
    """Какие пары сканировать. Возвращает список пар и счётчики причин отсева."""
    quotes = {q.strip().upper() for q in cfg.quote_assets if q.strip()}
    black = {normalize_asset(a) for a in cfg.blacklist if a.strip()}
    white = {normalize_asset(a) for a in cfg.whitelist if a.strip()}
    stats: Counter[str] = Counter()
    selected: list[str] = []
    for sym, info in symbols.items():
        if info.quote not in quotes:
            continue
        stats["total"] += 1
        base = normalize_asset(info.base)
        if base in black:
            stats["blacklist"] += 1
            continue
        if base in white:
            selected.append(sym)
            continue
        if cfg.exclude_leveraged_tokens and _LEVERAGED.match(base):
            stats["leveraged"] += 1
            continue
        if cfg.exclude_listed_on_liquid and listed_on(base):
            stats["liquid"] += 1
            continue
        t = tickers.get(sym)
        if t is None:
            stats["no_ticker"] += 1
            continue
        if t.quote_volume_24h < cfg.min_volume_24h_usdt:
            stats["low_volume"] += 1
            continue
        if t.quote_volume_24h > cfg.max_volume_24h_usdt:
            stats["high_volume"] += 1
            continue
        selected.append(sym)
    stats["selected"] = len(selected)
    return sorted(selected), dict(stats)


def pair_url(template: str, info: SymbolInfo) -> str:
    try:
        return template.format(base=info.base, quote=info.quote, symbol=info.symbol)
    except (KeyError, IndexError, ValueError):
        return template


class ExchangeScanner:
    def __init__(
        self,
        client: ExchangeClient,
        get_settings: Callable[[], AppSettings],
        liquid: LiquidRegistry,
        hub: AlertHub,
    ):
        self.client = client
        self.id = client.id
        self._get_settings = get_settings
        self._liquid = liquid
        self._hub = hub

        self.symbols: dict[str, SymbolInfo] = {}
        self.tickers: dict[str, Ticker] = {}
        self.universe: set[str] = set()
        self.universe_stats: dict[str, int] = {}

        self._ticker_hist: dict[str, deque[tuple[float, float, float]]] = defaultdict(deque)
        self._hot: deque[str] = deque()
        self._hot_set: set[str] = set()
        self._last_scan: dict[str, float] = {}
        self._in_progress: set[str] = set()
        self._rr: list[str] = []
        self._rr_idx = 0

        self._tasks: list[asyncio.Task] = []
        self._workers: list[asyncio.Task] = []
        self._scan_times: deque[float] = deque()
        self._errors: deque[float] = deque()
        self.last_error: str | None = None
        self.last_tickers_at: float | None = None
        self.last_symbols_at: float | None = None
        self.alerts = 0

    @property
    def cfg(self) -> ExchangeSettings:
        return self._get_settings().exchange(self.id)

    # ----------------------------------------------------------------- жизненный цикл

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._symbols_loop(), name=f"{self.id}-symbols"),
            asyncio.create_task(self._tickers_loop(), name=f"{self.id}-tickers"),
        ]

    async def stop(self) -> None:
        for task in self._tasks + self._workers:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._workers, return_exceptions=True)
        self._tasks, self._workers = [], []

    def _ensure_workers(self, count: int) -> None:
        self._workers = [w for w in self._workers if not w.done()]
        while len(self._workers) < count:
            self._workers.append(asyncio.create_task(self._worker(), name=f"{self.id}-worker"))
        while len(self._workers) > count:
            self._workers.pop().cancel()

    def _record_error(self, exc: Exception) -> None:
        self.last_error = str(exc)[:300]
        self._errors.append(time.time())

    # ----------------------------------------------------------------- уровень 1: тикеры

    async def _symbols_loop(self) -> None:
        while True:
            if not self.cfg.enabled:
                await asyncio.sleep(2)
                continue
            try:
                self.symbols = await self.client.load_symbols()
                self.last_symbols_at = time.time()
                log.info("%s: %d торгуемых пар", self.id, len(self.symbols))
                await asyncio.sleep(self.cfg.scan.symbols_refresh_min * 60)
            except ExchangeError as exc:
                self._record_error(exc)
                log.warning("%s: не удалось загрузить список пар: %s", self.id, exc)
                await asyncio.sleep(30)

    async def _tickers_loop(self) -> None:
        # без листингов ликвидных бирж первый проход насканировал бы всё подряд
        try:
            await asyncio.wait_for(self._liquid.ready.wait(), timeout=120)
        except TimeoutError:
            log.warning("Листинги ликвидных бирж не загрузились за 2 минуты, стартуем без них")
        while True:
            cfg = self.cfg
            if not cfg.enabled:
                self.universe = set()
                self._ensure_workers(0)
                await asyncio.sleep(2)
                continue
            self.client.scan_limiter.rate = cfg.scan.max_requests_per_sec
            self._ensure_workers(cfg.scan.workers)
            started = time.monotonic()
            if self.symbols:
                try:
                    self.tickers = await self.client.fetch_tickers()
                    self.last_tickers_at = time.time()
                    self._update_universe(cfg)
                    self._update_hot(cfg)
                except ExchangeError as exc:
                    self._record_error(exc)
                    log.warning("%s: ошибка тикеров: %s", self.id, exc)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(cfg.scan.tickers_interval_sec - elapsed, 0.5))

    def _update_universe(self, cfg: ExchangeSettings) -> None:
        liquid_cfg = self._get_settings().liquid
        selected, stats = build_universe(
            self.symbols, self.tickers, cfg.universe, lambda base: self._liquid.listed_on(base, liquid_cfg)
        )
        self.universe = set(selected)
        self.universe_stats = stats
        for sym in list(self._ticker_hist):
            if sym not in self.universe:
                del self._ticker_hist[sym]

    def _update_hot(self, cfg: ExchangeSettings) -> None:
        s = cfg.scan
        now = time.time()
        for sym in self.universe:
            t = self.tickers.get(sym)
            if t is None:
                continue
            hist = self._ticker_hist[sym]
            hist.append((now, t.last, t.quote_volume_24h))
            while hist and now - hist[0][0] > s.hot_window_sec:
                hist.popleft()
            if len(hist) < 2:
                continue
            _, first_price, first_vol = hist[0]
            price_move = abs(t.last / first_price - 1) * 100 if first_price else 0.0
            vol_jump = t.quote_volume_24h - first_vol
            if (s.hot_price_change_pct and price_move >= s.hot_price_change_pct) or (
                s.hot_volume_jump_usdt and vol_jump >= s.hot_volume_jump_usdt
            ):
                self.mark_hot(sym)

    def mark_hot(self, sym: str) -> None:
        if sym in self._hot_set:
            return
        if time.time() - self._last_scan.get(sym, 0) < HOT_MIN_RESCAN_SEC:
            return
        self._hot.append(sym)
        self._hot_set.add(sym)

    # ----------------------------------------------------------------- уровень 2: глубокий скан

    async def _next_symbol(self) -> str:
        skipped = 0
        while True:
            cfg = self.cfg
            if not cfg.enabled or not self.universe:
                await asyncio.sleep(1)
                continue
            if skipped > len(self._rr):
                # прошли весь круг, и все монеты уже сканируют другие воркеры: без паузы
                # этот цикл занял бы event loop целиком
                skipped = 0
                await asyncio.sleep(0.5)
            now = time.time()
            while self._hot:
                sym = self._hot.popleft()
                self._hot_set.discard(sym)
                if (
                    sym in self.universe
                    and sym not in self._in_progress
                    and now - self._last_scan.get(sym, 0) >= HOT_MIN_RESCAN_SEC
                ):
                    return sym
            if self._rr_idx >= len(self._rr):
                self._rr = sorted(self.universe, key=lambda s: self._last_scan.get(s, 0))
                self._rr_idx = 0
            sym = self._rr[self._rr_idx]
            if sym not in self.universe or sym in self._in_progress:
                self._rr_idx += 1
                skipped += 1
                continue
            wait = cfg.scan.deep_scan_min_interval_sec - (now - self._last_scan.get(sym, 0))
            if wait <= 0:
                self._rr_idx += 1
                return sym
            await asyncio.sleep(min(wait, 1.0))

    async def _worker(self) -> None:
        while True:
            sym = await self._next_symbol()
            self._in_progress.add(sym)
            try:
                await self.scan_symbol(sym)
            except ExchangeError as exc:
                self._record_error(exc)
            except Exception as exc:
                self._record_error(exc)
                log.exception("%s: ошибка скана %s", self.id, sym)
            finally:
                self._in_progress.discard(sym)
                self._last_scan[sym] = time.time()

    def _needs_trades(self, sym: str, d: DetectorsSettings, candles: list[Candle], now_ms: int) -> bool:
        """Ленту качаем, только если свечи показывают хоть какой-то оборот — экономим лимиты API."""
        cooldown = self.cfg.alert_cooldown_min
        checks: list[tuple[float, float]] = []
        pp = d.ping_pong
        if pp.enabled and not self._hub.in_cooldown(self.id, sym, "ping_pong", cooldown):
            checks.append((pp.window_minutes, pp.min_volume_usdt))
        sp = d.spread
        ticker = self.tickers.get(sym)
        spread = ticker.spread_pct if ticker else None
        if (
            sp.enabled
            and spread is not None
            and sp.min_spread_pct <= spread <= sp.max_spread_pct
            and not self._hub.in_cooldown(self.id, sym, "spread", cooldown)
        ):
            checks.append((sp.window_minutes, sp.min_volume_usdt))
        for window, min_volume in checks:
            if sum(c.quote_volume for c in candles_in_window(candles, now_ms, window)) >= max(min_volume, 1e-9):
                return True
        return False

    async def scan_symbol(self, sym: str) -> list[Alert]:
        cfg = self.cfg
        info = self.symbols.get(sym)
        if info is None:
            return []
        candles = await self.client.fetch_candles(sym, "1m", cfg.scan.kline_limit)
        now_ms = int(time.time() * 1000)
        trades = None
        if self._needs_trades(sym, cfg.detectors, candles, now_ms):
            trades = await self.client.fetch_trades(sym, cfg.scan.trades_limit)
            now_ms = int(time.time() * 1000)
        ctx = ScanContext(
            exchange=self.id, info=info, now_ms=now_ms, candles=candles, trades=trades, ticker=self.tickers.get(sym)
        )
        self._scan_times.append(time.time())

        fired: list[Alert] = []
        for det in DETECTORS:
            params = getattr(cfg.detectors, det.key)
            if not params.enabled or (det.needs_trades and trades is None):
                continue
            if self._hub.in_cooldown(self.id, sym, det.key, cfg.alert_cooldown_min):
                continue
            try:
                result = det.detect(ctx, params)
            except Exception:  # ошибка одного детектора не должна ронять скан
                log.exception("%s: детектор %s упал на %s", self.id, det.key, sym)
                continue
            if result is None:
                continue
            alert = self._make_alert(info, det, result, cfg)
            if await self._hub.submit(alert):
                self.alerts += 1
                fired.append(alert)
        return fired

    def _make_alert(self, info: SymbolInfo, det, result: Detection, cfg: ExchangeSettings) -> Alert:
        return Alert(
            id=uuid.uuid4().hex,
            ts=time.time(),
            exchange=self.id,
            symbol=info.symbol,
            base=info.base,
            quote=info.quote,
            detector=det.key,
            detector_name=det.name,
            title=result.title,
            reason=result.reason,
            score=result.score,
            metrics=result.metrics,
            levels=result.levels,
            url=pair_url(cfg.pair_url_template, info),
        )

    # ----------------------------------------------------------------- статус

    def status(self) -> dict:
        now = time.time()
        while self._scan_times and now - self._scan_times[0] > 60:
            self._scan_times.popleft()
        while self._errors and now - self._errors[0] > 300:
            self._errors.popleft()
        per_min = len(self._scan_times)
        return {
            "id": self.id,
            "name": self.client.name,
            "enabled": self.cfg.enabled,
            "symbols": len(self.symbols),
            "universe": len(self.universe),
            "universe_stats": self.universe_stats,
            "hot": len(self._hot),
            "scans_per_min": per_min,
            "pass_minutes": round(len(self.universe) / per_min, 1) if per_min else None,
            "errors_5m": len(self._errors),
            "last_error": self.last_error,
            "last_tickers_at": self.last_tickers_at,
            "alerts": self.alerts,
        }
