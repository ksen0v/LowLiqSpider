"""Всплеск объёма: на неликвиде это обычно значит, что включился алгоритм."""

from __future__ import annotations

from app.detectors.base import Detector, closed_candles, fmt_usd
from app.models import Detection, ScanContext
from app.settings import VolumeSpikeParams


class VolumeSpikeDetector(Detector[VolumeSpikeParams]):
    key = "volume_spike"
    name = "Всплеск объёма"
    short = "ОБЪЁМ"

    def detect(self, ctx: ScanContext, p: VolumeSpikeParams) -> Detection | None:
        candles = ctx.candles if p.include_current else closed_candles(ctx.candles, ctx.now_ms)
        if len(candles) < p.window_candles + 5:
            return None
        window = candles[-p.window_candles :]
        base = candles[-(p.window_candles + p.baseline_candles) : -p.window_candles]

        window_sum = sum(c.quote_volume for c in window)
        if window_sum < p.min_volume_usdt:
            return None
        window_avg = window_sum / len(window)
        base_avg = max(sum(c.quote_volume for c in base) / len(base), p.min_baseline_usdt)
        ratio = window_avg / base_avg
        if ratio < p.multiplier:
            return None

        first, last = window[0], window[-1]
        move = (last.close / first.open - 1) * 100 if first.open else 0.0
        reason = (
            f"За последние {len(window)} мин оборот {fmt_usd(window_sum)} — в {ratio:.1f} раза выше "
            f"среднего за {len(base)} мин ({fmt_usd(base_avg)}/мин). Цена за это время {move:+.2f}%."
        )
        return Detection(
            title=f"Объём x{ratio:.1f}: {fmt_usd(window_sum)} за {len(window)} мин",
            reason=reason,
            score=round(min(ratio / (p.multiplier * 4), 1.0), 3),
            metrics={
                "Кратность": round(ratio, 2),
                "Объём окна, USDT": round(window_sum, 2),
                "База, USDT/мин": round(base_avg, 2),
                "Цена за окно, %": round(move, 3),
            },
        )
