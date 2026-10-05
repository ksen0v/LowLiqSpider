"""Настройки сервиса.

Все настройки живут в одной pydantic-модели AppSettings, хранятся в SQLite и редактируются
из веб-интерфейса. Форма настроек строится по JSON-схеме этой модели, поэтому title и
description полей здесь — это подписи и подсказки в интерфейсе.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


PROXY_DESCRIPTION = (
    "Если API недоступно из вашей сети: http://user:pass@host:port или socks5://host:port. "
    "Пусто — напрямую (или через переменную окружения HTTPS_PROXY)"
)


def _check_proxy(value: str) -> str:
    value = value.strip()
    if value and not value.lower().startswith(("http://", "https://", "socks5://", "socks5h://")):
        raise ValueError("Прокси должен начинаться с http://, https://, socks5:// или socks5h://")
    return value


# --------------------------------------------------------------------------- детекторы


class PingPongParams(_Section):
    model_config = ConfigDict(title="Ёрш / пинг-понг")

    enabled: bool = Field(True, title="Включён")
    window_minutes: float = Field(10, ge=1, le=60, title="Окно анализа, мин")
    min_trades: int = Field(20, ge=4, le=1000, title="Мин. сделок в окне")
    min_volume_usdt: float = Field(
        200, ge=0, title="Мин. оборот в окне, USDT", description="Оборот всех сделок за окно анализа"
    )
    size_tolerance_pct: float = Field(
        2.0,
        ge=0,
        le=50,
        title="Допуск совпадения объёма, %",
        description="Объёмы двух сделок считаются одинаковыми, если различаются не больше чем на столько процентов",
    )
    pair_window_sec: float = Field(
        120,
        ge=1,
        le=3600,
        title="Окно поиска пары, сек",
        description="Покупка и продажа одинакового объёма считаются парой (маркетмейкер крутит сам в себя), если между ними не больше столько секунд",
    )
    max_imbalance: float = Field(
        0.35,
        ge=0,
        le=1,
        title="Макс. дисбаланс покупок/продаж",
        description="0 — покупок и продаж поровну, 1 — всё в одну сторону",
    )
    min_capture_pct: float = Field(
        0.3,
        ge=0,
        le=100,
        title="Мин. ширина коридора, %",
        description="Разница между средней ценой покупок и средней ценой продаж тейкеров. Это то, что можно забрать лимитками; ставьте выше комиссии мейкера x2",
    )
    max_range_pct: float = Field(
        8,
        ge=0.1,
        le=100,
        title="Макс. диапазон цены в окне, %",
        description="Если цена ушла дальше, это уже тренд, а не коридор",
    )
    min_score: float = Field(
        0.55,
        ge=0,
        le=1,
        title="Мин. скор (0–1)",
        description="Итоговая оценка: повтор объёмов, парные сделки, баланс сторон, чередование, концентрация цен",
    )


class SpreadParams(_Section):
    model_config = ConfigDict(title="Широкий спред + наливы")

    enabled: bool = Field(True, title="Включён")
    min_spread_pct: float = Field(1.0, ge=0, le=100, title="Мин. спред, %")
    max_spread_pct: float = Field(
        20, ge=0, le=1000, title="Макс. спред, %", description="Выше — скорее всего мёртвый стакан"
    )
    window_minutes: float = Field(10, ge=1, le=60, title="Окно анализа сделок, мин")
    min_trades: int = Field(8, ge=1, le=1000, title="Мин. сделок в окне")
    min_volume_usdt: float = Field(100, ge=0, title="Мин. оборот в окне, USDT")
    require_both_sides: bool = Field(
        True, title="Нужны и покупки, и продажи", description="В спред должны наливать с обеих сторон"
    )


class WicksParams(_Section):
    model_config = ConfigDict(title="Иглы / прострелы")

    enabled: bool = Field(True, title="Включён")
    lookback_candles: int = Field(30, ge=3, le=500, title="Сколько последних 1m-свечей смотреть")
    min_wick_pct: float = Field(1.5, ge=0.05, le=100, title="Мин. длина тени, %")
    max_body_ratio: float = Field(
        0.35,
        ge=0,
        le=1,
        title="Макс. доля тела в свече",
        description="Тело / (high − low). Маленькое тело + длинная тень = игла",
    )
    min_wick_candles: int = Field(4, ge=1, le=500, title="Мин. свечей с иглами")
    require_both_sides: bool = Field(True, title="Иглы и вверх, и вниз")
    min_volume_usdt: float = Field(100, ge=0, title="Мин. оборот за период, USDT")


class VolumeSpikeParams(_Section):
    model_config = ConfigDict(title="Всплеск объёма")

    enabled: bool = Field(True, title="Включён")
    window_candles: int = Field(3, ge=1, le=60, title="Окно всплеска, 1m-свечей")
    baseline_candles: int = Field(60, ge=5, le=900, title="База для сравнения, 1m-свечей")
    multiplier: float = Field(
        5, ge=1, le=1000, title="Во сколько раз выше нормы", description="Средний объём окна / средний объём базы"
    )
    min_volume_usdt: float = Field(1000, ge=0, title="Мин. объём окна, USDT")
    min_baseline_usdt: float = Field(
        5,
        ge=0,
        title="Пол базы, USDT/мин",
        description="Если монета стояла мёртвой, база считается не меньше этого значения, чтобы не делить на ноль",
    )
    include_current: bool = Field(True, title="Учитывать текущую незакрытую свечу")


class PriceChangeParams(_Section):
    model_config = ConfigDict(title="Резкое изменение цены")

    enabled: bool = Field(True, title="Включён")
    window_minutes: int = Field(5, ge=1, le=240, title="Окно, мин")
    threshold_pct: float = Field(5, ge=0.1, le=1000, title="Порог изменения, %")
    direction: Literal["both", "up", "down"] = Field(
        "both", title="Направление", description="both — любое, up — только рост, down — только падение"
    )
    min_volume_usdt: float = Field(300, ge=0, title="Мин. оборот в окне, USDT")


class DetectorsSettings(_Section):
    model_config = ConfigDict(title="Детекторы")

    ping_pong: PingPongParams = Field(default_factory=PingPongParams, title="Ёрш / пинг-понг")
    spread: SpreadParams = Field(default_factory=SpreadParams, title="Широкий спред + наливы")
    wicks: WicksParams = Field(default_factory=WicksParams, title="Иглы / прострелы")
    volume_spike: VolumeSpikeParams = Field(default_factory=VolumeSpikeParams, title="Всплеск объёма")
    price_change: PriceChangeParams = Field(default_factory=PriceChangeParams, title="Резкое изменение цены")


# --------------------------------------------------------------------------- биржи


class UniverseSettings(_Section):
    model_config = ConfigDict(title="Отбор монет")

    quote_assets: list[str] = Field(
        ["USDT"], title="Котируемые валюты", description="Через запятую, например USDT, USDC"
    )
    min_volume_24h_usdt: float = Field(1000, ge=0, title="Мин. объём за 24ч, USDT")
    max_volume_24h_usdt: float = Field(
        3_000_000, ge=0, title="Макс. объём за 24ч, USDT", description="Всё, что выше, считаем ликвидным и не сканируем"
    )
    exclude_listed_on_liquid: bool = Field(
        True,
        title="Исключать монеты с ликвидных бирж",
        description="Список ликвидных бирж — на вкладке «Ликвидные биржи»",
    )
    exclude_leveraged_tokens: bool = Field(True, title="Исключать плечевые токены (3L/3S/5L…)")
    blacklist: list[str] = Field(
        default_factory=list, title="Чёрный список монет", description="Тикеры через запятую: ABC, XYZ"
    )
    whitelist: list[str] = Field(
        default_factory=list,
        title="Белый список монет",
        description="Сканируются всегда, без проверки ликвидных бирж и объёма",
    )


class ScanSettings(_Section):
    model_config = ConfigDict(title="Сканирование")

    tickers_interval_sec: float = Field(
        5, ge=1, le=300, title="Опрос тикеров, сек", description="Один запрос на все пары: цена, спред, объём 24ч"
    )
    symbols_refresh_min: float = Field(30, ge=1, le=1440, title="Обновление списка пар, мин")
    deep_scan_min_interval_sec: float = Field(
        30,
        ge=1,
        le=3600,
        title="Мин. интервал глубокого скана монеты, сек",
        description="Глубокий скан — это свечи и лента сделок по одной монете",
    )
    workers: int = Field(6, ge=1, le=64, title="Параллельных воркеров")
    max_requests_per_sec: float = Field(
        12, ge=0.5, le=100, title="Лимит запросов в секунду", description="Общий лимит сканера к API биржи"
    )
    kline_limit: int = Field(120, ge=30, le=1000, title="Сколько 1m-свечей грузить")
    trades_limit: int = Field(500, ge=50, le=1000, title="Сколько последних сделок грузить")
    hot_window_sec: float = Field(
        60,
        ge=5,
        le=900,
        title="Окно «горячих» монет, сек",
        description="Монета с резким движением по тикерам сканируется вне очереди",
    )
    hot_price_change_pct: float = Field(1.5, ge=0, le=100, title="Горячая: изменение цены, %")
    hot_volume_jump_usdt: float = Field(500, ge=0, title="Горячая: прирост объёма 24ч, USDT")


class ExchangeSettings(_Section):
    enabled: bool = Field(True, title="Биржа включена")
    pair_url_template: str = Field("", title="Ссылка на пару", description="Плейсхолдеры {base}, {quote}, {symbol}")
    alert_cooldown_min: float = Field(
        15, ge=0, le=1440, title="Пауза между алертами, мин", description="Для одной монеты и одного детектора"
    )
    proxy: str = Field("", title="Прокси для API биржи", description=PROXY_DESCRIPTION)
    universe: UniverseSettings = Field(default_factory=UniverseSettings, title="Отбор монет")
    scan: ScanSettings = Field(default_factory=ScanSettings, title="Сканирование")
    detectors: DetectorsSettings = Field(default_factory=DetectorsSettings, title="Детекторы")

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, value: str) -> str:
        return _check_proxy(value)


def _mexc_defaults() -> ExchangeSettings:
    s = ExchangeSettings(pair_url_template="https://www.mexc.com/exchange/{base}_{quote}")
    # на споте MEXC мейкер платит 0%, поэтому коридор можно ловить уже
    s.detectors.ping_pong.min_capture_pct = 0.3
    return s


def _gate_defaults() -> ExchangeSettings:
    s = ExchangeSettings(pair_url_template="https://www.gate.com/trade/{base}_{quote}")
    # у Gate комиссия мейкера ~0.1–0.2%, коридор нужен шире
    s.detectors.ping_pong.min_capture_pct = 0.5
    return s


# --------------------------------------------------------------------------- ликвидные биржи


class LiquidVenue(_Section):
    enabled: bool = Field(True, title="Учитывать")
    spot: bool = Field(True, title="Спот")
    futures: bool = Field(True, title="Фьючерсы")


class LiquidSpotVenue(_Section):
    """Биржа, у которой учитываем только спот."""

    enabled: bool = Field(True, title="Учитывать")


def _venue(enabled: bool = True) -> LiquidVenue:
    return LiquidVenue(enabled=enabled)


class LiquidSettings(_Section):
    model_config = ConfigDict(title="Ликвидные биржи")

    refresh_hours: float = Field(6, ge=0.25, le=168, title="Обновлять листинги, раз в N часов")
    proxy: str = Field("", title="Прокси для API ликвидных бирж", description=PROXY_DESCRIPTION)
    binance: LiquidVenue = Field(default_factory=_venue, title="Binance")
    bybit: LiquidVenue = Field(default_factory=_venue, title="Bybit")
    okx: LiquidVenue = Field(default_factory=_venue, title="OKX")
    bitget: LiquidVenue = Field(default_factory=_venue, title="Bitget")
    coinbase: LiquidSpotVenue = Field(default_factory=LiquidSpotVenue, title="Coinbase (спот)")
    upbit: LiquidSpotVenue = Field(default_factory=LiquidSpotVenue, title="Upbit (спот)")
    kraken: LiquidSpotVenue = Field(default_factory=LiquidSpotVenue, title="Kraken (спот)")
    kucoin: LiquidVenue = Field(default_factory=lambda: _venue(enabled=False), title="KuCoin")
    htx: LiquidVenue = Field(default_factory=lambda: _venue(enabled=False), title="HTX")
    extra_assets: list[str] = Field(
        default_factory=list,
        title="Считать ликвидными вручную",
        description="Тикеры через запятую. Эти монеты никогда не попадут в скан",
    )

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, value: str) -> str:
        return _check_proxy(value)


# --------------------------------------------------------------------------- уведомления и UI


class TelegramSettings(_Section):
    model_config = ConfigDict(title="Telegram")

    enabled: bool = Field(False, title="Отправлять в Telegram")
    bot_token: str = Field("", title="Токен бота", description="Выдаёт @BotFather")
    chat_id: str = Field("", title="Chat ID", description="Ваш ID или ID группы/канала")
    ping_pong: bool = Field(True, title="Ёрш / пинг-понг")
    spread: bool = Field(True, title="Широкий спред")
    wicks: bool = Field(True, title="Иглы")
    volume_spike: bool = Field(True, title="Всплеск объёма")
    price_change: bool = Field(True, title="Изменение цены")


class UISettings(_Section):
    model_config = ConfigDict(title="Интерфейс")

    grid_rows: int = Field(3, ge=1, le=6, title="Строк в сетке")
    grid_cols: int = Field(3, ge=1, le=6, title="Столбцов в сетке")
    sound: bool = Field(True, title="Звук при алерте")
    chart_interval: Literal["1m", "5m", "15m", "1h"] = Field("1m", title="Таймфрейм графика")
    chart_candles: int = Field(180, ge=30, le=1000, title="Свечей на графике")
    card_refresh_sec: float = Field(5, ge=1, le=120, title="Обновление графиков, сек")
    show_tape: bool = Field(True, title="Показывать ленту сделок в карточке")
    alert_retention_days: float = Field(7, ge=0.1, le=365, title="Хранить историю алертов, дней")


class AppSettings(_Section):
    model_config = ConfigDict(title="Настройки")

    mexc: ExchangeSettings = Field(default_factory=_mexc_defaults, title="MEXC")
    gate: ExchangeSettings = Field(default_factory=_gate_defaults, title="Gate")
    liquid: LiquidSettings = Field(default_factory=LiquidSettings, title="Ликвидные биржи")
    telegram: TelegramSettings = Field(default_factory=TelegramSettings, title="Telegram")
    ui: UISettings = Field(default_factory=UISettings, title="Интерфейс")

    def exchange(self, exchange_id: str) -> ExchangeSettings:
        return getattr(self, exchange_id)


EXCHANGE_IDS = ("mexc", "gate")
