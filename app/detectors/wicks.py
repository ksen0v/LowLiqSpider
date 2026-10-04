"""Иглы / прострелы: свечи с маленьким телом и длинными тенями.

Алгоритм периодически выбивает цену вверх и вниз. Лимитки, выставленные на уровнях игл,
собирают эти прострелы.
"""

from __future__ import annotations

from statistics import median

from app.detectors.base import Detector, closed_candles, fmt_price, fmt_usd
from app.models import Detection, Level, ScanContext
from app.settings import WicksParams


class WicksDetector(Detector[WicksParams]):
    key = "wicks"
    name = "Иглы / прострелы"
    short = "ИГЛЫ"

    def detect(self, ctx: ScanContext, p: WicksParams) -> Detection | None:
        candles = closed_candles(ctx.candles, ctx.now_ms)[-p.lookback_candles :]
        if len(candles) < min(p.lookback_candles, 3):
            return None
        volume = sum(c.quote_volume for c in candles)
        if volume < p.min_volume_usdt:
            return None

        up_highs: list[float] = []
        down_lows: list[float] = []
        lengths: list[float] = []
        wick_candles = 0
        for c in candles:
            rng = c.high - c.low
            if rng <= 0:
                continue
            mid = (c.high + c.low) / 2
            body_ratio = abs(c.close - c.open) / rng
            if body_ratio > p.max_body_ratio:
                continue
            upper = (c.high - max(c.open, c.close)) / mid * 100
            lower = (min(c.open, c.close) - c.low) / mid * 100
            if upper >= p.min_wick_pct:
                up_highs.append(c.high)
                lengths.append(upper)
            if lower >= p.min_wick_pct:
                down_lows.append(c.low)
                lengths.append(lower)
            if upper >= p.min_wick_pct or lower >= p.min_wick_pct:
                wick_candles += 1

        count = len(up_highs) + len(down_lows)
        if wick_candles < p.min_wick_candles:
            return None
        if p.require_both_sides and (not up_highs or not down_lows):
            return None

        avg_len = sum(lengths) / len(lengths)
        levels = []
        parts = []
        if down_lows:
            low = median(down_lows)
            levels.append(Level(price=low, label="Иглы вниз ~", color="#0ecb81"))
            parts.append(f"вниз {len(down_lows)} (медиана {fmt_price(low)})")
        if up_highs:
            high = median(up_highs)
            levels.append(Level(price=high, label="Иглы вверх ~", color="#f6465d"))
            parts.append(f"вверх {len(up_highs)} (медиана {fmt_price(high)})")

        reason = (
            f"За {len(candles)} мин {count} игл в {wick_candles} свечах: {', '.join(parts)}. "
            f"Средняя длина тени {avg_len:.2f}%, оборот {fmt_usd(volume)}."
        )
        return Detection(
            title=f"Иглы: {count} шт, тень ~{avg_len:.2f}%",
            reason=reason,
            score=round(min(wick_candles / (p.min_wick_candles * 3), 1.0), 3),
            metrics={
                "Игл вверх": len(up_highs),
                "Игл вниз": len(down_lows),
                "Средняя тень, %": round(avg_len, 3),
                "Оборот, USDT": round(volume, 2),
            },
            levels=levels,
        )
