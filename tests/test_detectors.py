import random

from app.detectors.ping_pong import (
    PingPongDetector,
    alternation_ratio,
    matched_pair_ratio,
    size_repeat_ratio,
)
from app.detectors.price_change import PriceChangeDetector
from app.detectors.spread import SpreadDetector
from app.detectors.volume_spike import VolumeSpikeDetector
from app.detectors.wicks import WicksDetector
from app.models import Candle, Trade
from app.settings import PingPongParams, PriceChangeParams, SpreadParams, VolumeSpikeParams, WicksParams
from tests.helpers import NOW, NOW_MS, ctx, flat_candles, ping_pong_trades, ticker

# --------------------------------------------------------------------------- пинг-понг


def test_ping_pong_detects_market_maker_loop():
    res = PingPongDetector().detect(ctx(trades=ping_pong_trades()), PingPongParams())
    assert res is not None
    assert res.metrics["Повтор объёмов, %"] == 100
    assert res.metrics["Парные сделки, %"] == 100
    assert abs(res.metrics["Коридор, %"] - 1.005) < 0.01
    # линии на графике: где покупать (низ) и где продавать (верх)
    assert [lv.price for lv in res.levels] == [0.995, 1.005]


def test_ping_pong_ignores_one_sided_flow():
    trades = [Trade(NOW_MS - i * 10_000, 1 + i * 0.0001, 100, "buy") for i in range(40)]
    assert PingPongDetector().detect(ctx(trades=trades), PingPongParams()) is None


def test_ping_pong_ignores_random_retail_trades():
    rng = random.Random(1)
    trades = []
    price = 1.0
    for i in range(60):
        price *= 1 + rng.uniform(-0.002, 0.002)
        trades.append(Trade(NOW_MS - (60 - i) * 9_000, price, rng.uniform(5, 500), rng.choice(("buy", "sell"))))
    assert PingPongDetector().detect(ctx(trades=trades), PingPongParams()) is None


def test_ping_pong_requires_corridor_wider_than_fees():
    trades = ping_pong_trades(low=0.9999, high=1.0001)
    assert PingPongDetector().detect(ctx(trades=trades), PingPongParams(min_capture_pct=0.3)) is None


def test_ping_pong_respects_window_and_min_trades():
    old = [Trade(t.ts - 3_600_000, t.price, t.qty, t.side) for t in ping_pong_trades()]
    assert PingPongDetector().detect(ctx(trades=old), PingPongParams()) is None
    assert PingPongDetector().detect(ctx(trades=ping_pong_trades(n=10)), PingPongParams(min_trades=20)) is None


def test_ping_pong_helpers():
    assert size_repeat_ratio([1, 1.01, 5, 9], 0.02) == 0.5
    assert size_repeat_ratio([1, 2, 3], 0.01) == 0
    trades = ping_pong_trades(n=4)
    assert alternation_ratio(trades) == 1.0
    assert matched_pair_ratio(trades, 0.01, 60_000) == 1.0
    # пара не засчитывается, если между сделками больше окна
    assert matched_pair_ratio(trades, 0.01, 5_000) == 0.0


# --------------------------------------------------------------------------- спред


def test_spread_with_fills_on_both_sides():
    trades = [Trade(NOW_MS - i * 30_000, 1.02 if i % 2 else 0.98, 50, "buy" if i % 2 else "sell") for i in range(12)]
    res = SpreadDetector().detect(ctx(trades=trades, ticker=ticker(0.98, 1.02)), SpreadParams())
    assert res is not None
    assert abs(res.metrics["Спред, %"] - 4.08) < 0.01


def test_spread_needs_trades_and_width():
    trades = [Trade(NOW_MS - i * 30_000, 1.0, 50, "buy" if i % 2 else "sell") for i in range(12)]
    assert SpreadDetector().detect(ctx(trades=trades, ticker=ticker(0.999, 1.001)), SpreadParams()) is None
    assert SpreadDetector().detect(ctx(trades=trades[:2], ticker=ticker(0.98, 1.02)), SpreadParams()) is None
    buys_only = [Trade(t.ts, t.price, t.qty, "buy") for t in trades]
    assert SpreadDetector().detect(ctx(trades=buys_only, ticker=ticker(0.98, 1.02)), SpreadParams()) is None


# --------------------------------------------------------------------------- иглы


def _wicky_candles() -> list[Candle]:
    candles = flat_candles(31, quote_volume=20)
    for i in range(0, 30, 5):
        c = candles[i]
        if (i // 5) % 2:
            candles[i] = Candle(c.time, 1.0, 1.04, 0.999, 1.001, c.volume, c.quote_volume)
        else:
            candles[i] = Candle(c.time, 1.0, 1.001, 0.96, 0.999, c.volume, c.quote_volume)
    return candles


def test_wicks_both_directions():
    res = WicksDetector().detect(ctx(candles=_wicky_candles()), WicksParams())
    assert res is not None
    assert res.metrics["Игл вверх"] == 3
    assert res.metrics["Игл вниз"] == 3


def test_wicks_ignore_trend_candles_and_one_side_when_required():
    assert WicksDetector().detect(ctx(candles=flat_candles(31, quote_volume=20)), WicksParams()) is None
    up_only = [
        Candle(c.time, 1.0, 1.04, 0.999, 1.001, c.volume, c.quote_volume) if i % 5 == 0 else c
        for i, c in enumerate(flat_candles(31, quote_volume=20))
    ]
    assert WicksDetector().detect(ctx(candles=up_only), WicksParams(require_both_sides=True)) is None
    assert WicksDetector().detect(ctx(candles=up_only), WicksParams(require_both_sides=False)) is not None


# --------------------------------------------------------------------------- объём


def test_volume_spike():
    candles = flat_candles(70, quote_volume=20)
    for i in range(-3, 0):
        c = candles[i]
        candles[i] = Candle(c.time, 1, 1, 1, 1, 1000, 1000)
    res = VolumeSpikeDetector().detect(ctx(candles=candles), VolumeSpikeParams())
    assert res is not None
    assert res.metrics["Кратность"] == 50


def test_volume_spike_on_dead_coin_uses_floor():
    candles = flat_candles(70, quote_volume=0)
    c = candles[-1]
    candles[-1] = Candle(c.time, 1, 1, 1, 1, 30, 30)
    # 10 USDT/мин за окно против пола 5 USDT/мин: x2 — мало, плюс объём меньше минимума
    assert VolumeSpikeDetector().detect(ctx(candles=candles), VolumeSpikeParams()) is None


# --------------------------------------------------------------------------- цена


def test_price_change_up_and_direction_filter():
    candles = flat_candles(10, quote_volume=200)
    for k, i in enumerate(range(-4, 0), start=1):
        c = candles[i]
        p = 1 + 0.02 * k
        candles[i] = Candle(c.time, p - 0.02, p, p - 0.02, p, 200 / p, 200)
    res = PriceChangeDetector().detect(ctx(candles=candles), PriceChangeParams())
    assert res is not None
    assert res.metrics["Макс. движение, %"] == 8.0
    assert PriceChangeDetector().detect(ctx(candles=candles), PriceChangeParams(direction="down")) is None


def test_price_change_counts_reverted_spike():
    candles = flat_candles(10, quote_volume=200)
    c = candles[-2]
    candles[-2] = Candle(c.time, 1.0, 1.0, 0.9, 1.0, 200, 200)
    res = PriceChangeDetector().detect(ctx(candles=candles), PriceChangeParams())
    assert res is not None
    assert res.metrics["Макс. движение, %"] == -10.0
    assert res.metrics["Сейчас, %"] == 0.0


def test_candles_window_uses_now():
    # свечи из прошлого часа не попадают в 5-минутное окно
    candles = flat_candles(10, end=NOW - 3600)
    assert PriceChangeDetector().detect(ctx(candles=candles), PriceChangeParams()) is None
