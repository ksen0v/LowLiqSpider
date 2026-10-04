"""Ёрш / пинг-понг: маркетмейкер крутит объём сам в себя.

Признаки в ленте сделок за окно:
- покупки и продажи примерно на одну сумму (баланс сторон);
- объёмы сделок повторяются (бот ставит один и тот же размер);
- покупка и продажа одного объёма идут парами с небольшим интервалом;
- стороны чередуются: buy, sell, buy, sell;
- цены сделок сидят на нескольких уровнях (верх и низ коридора).

Коридор = средняя цена покупок тейкеров (бьют в аск) минус средняя цена продаж тейкеров
(бьют в бид). Если поставить лимитку на покупку у низа и на продажу у верха, алгоритм
наливает в обе.
"""

from __future__ import annotations

from collections import Counter
from itertools import pairwise

from app.detectors.base import Detector, fmt_price, fmt_usd, percentile, trades_in_window
from app.models import Detection, Level, ScanContext, Trade
from app.settings import PingPongParams


def size_repeat_ratio(qtys: list[float], tolerance: float) -> float:
    """Доля сделок, объём которых совпадает (в пределах допуска) хотя бы с ещё одной сделкой."""
    if len(qtys) < 2:
        return 0.0
    values = sorted(qtys)
    repeated = 0
    i = 0
    while i < len(values):
        j = i + 1
        while j < len(values) and values[j] <= values[i] * (1 + tolerance) + 1e-12:
            j += 1
        if j - i >= 2:
            repeated += j - i
        i = j
    return repeated / len(values)


def matched_pair_ratio(trades: list[Trade], tolerance: float, window_ms: float) -> float:
    """Доля сделок, которые образуют пару «покупка ↔ продажа того же объёма» в пределах окна."""
    if len(trades) < 2:
        return 0.0
    pending: dict[str, list[Trade]] = {"buy": [], "sell": []}
    pairs = 0
    for t in trades:
        opposite = pending["sell" if t.side == "buy" else "buy"]
        while opposite and t.ts - opposite[0].ts > window_ms:
            opposite.pop(0)
        match = next(
            (i for i, o in enumerate(opposite) if abs(o.qty - t.qty) <= tolerance * max(o.qty, t.qty)),
            None,
        )
        if match is None:
            pending[t.side].append(t)
        else:
            opposite.pop(match)
            pairs += 1
    return 2 * pairs / len(trades)


def alternation_ratio(trades: list[Trade]) -> float:
    if len(trades) < 2:
        return 0.0
    switches = sum(1 for a, b in pairwise(trades) if a.side != b.side)
    return switches / (len(trades) - 1)


def price_concentration(trades: list[Trade], levels: int = 4) -> float:
    """Доля сделок, пришедшихся на `levels` самых частых цен."""
    if not trades:
        return 0.0
    counts = Counter(t.price for t in trades)
    return sum(c for _, c in counts.most_common(levels)) / len(trades)


def vwap(trades: list[Trade]) -> float:
    qty = sum(t.qty for t in trades)
    return sum(t.price * t.qty for t in trades) / qty if qty else 0.0


class PingPongDetector(Detector[PingPongParams]):
    key = "ping_pong"
    name = "Ёрш / пинг-понг"
    short = "ЁРШ"
    needs_trades = True

    def detect(self, ctx: ScanContext, p: PingPongParams) -> Detection | None:
        if not ctx.trades:
            return None
        w = trades_in_window(ctx.trades, ctx.now_ms, p.window_minutes)
        n = len(w)
        if n < p.min_trades:
            return None
        buys = [t for t in w if t.side == "buy"]
        sells = [t for t in w if t.side == "sell"]
        if not buys or not sells:
            return None

        buy_q = sum(t.quote for t in buys)
        sell_q = sum(t.quote for t in sells)
        total = buy_q + sell_q
        if total < p.min_volume_usdt:
            return None
        imbalance = abs(buy_q - sell_q) / total
        if imbalance > p.max_imbalance:
            return None

        buy_px = vwap(buys)
        sell_px = vwap(sells)
        capture = (buy_px - sell_px) / sell_px * 100 if sell_px else 0.0
        if capture < p.min_capture_pct:
            return None

        prices = sorted(t.price for t in w)
        mid = percentile(prices, 0.5)
        range_pct = (percentile(prices, 0.95) - percentile(prices, 0.05)) / mid * 100 if mid else 0.0
        if range_pct > p.max_range_pct:
            return None

        tol = p.size_tolerance_pct / 100
        repeat = size_repeat_ratio([t.qty for t in w], tol)
        pairs = matched_pair_ratio(w, tol, p.pair_window_sec * 1000)
        alternation = alternation_ratio(w)
        concentration = price_concentration(w)
        balance = 1 - imbalance
        score = 0.30 * repeat + 0.25 * pairs + 0.15 * balance + 0.15 * alternation + 0.15 * concentration
        if score < p.min_score:
            return None

        per_min = n / p.window_minutes
        reason = (
            f"{n} сделок за {p.window_minutes:g} мин ({per_min:.1f}/мин), оборот {fmt_usd(total)}. "
            f"Покупки/продажи {buy_q / total * 100:.0f}/{sell_q / total * 100:.0f}%. "
            f"Повтор объёмов {repeat * 100:.0f}%, парных сделок {pairs * 100:.0f}%, "
            f"чередование {alternation * 100:.0f}%. "
            f"Тейкеры покупают в среднем по {fmt_price(buy_px)}, продают по {fmt_price(sell_px)} — "
            f"коридор {capture:.2f}%."
        )
        return Detection(
            title=f"Ёрш: коридор {capture:.2f}%, {per_min:.1f} сд/мин",
            reason=reason,
            score=round(score, 3),
            metrics={
                "Коридор, %": round(capture, 3),
                "Сделок": n,
                "Сделок/мин": round(per_min, 2),
                "Оборот, USDT": round(total, 2),
                "Повтор объёмов, %": round(repeat * 100, 1),
                "Парные сделки, %": round(pairs * 100, 1),
                "Чередование, %": round(alternation * 100, 1),
                "Дисбаланс": round(imbalance, 3),
                "Диапазон цены, %": round(range_pct, 3),
            },
            levels=[
                Level(price=sell_px, label="Покупать ~", color="#0ecb81"),
                Level(price=buy_px, label="Продавать ~", color="#f6465d"),
            ],
        )
