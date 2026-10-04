"""Резкое изменение цены за короткое окно."""

from __future__ import annotations

from app.detectors.base import Detector, candles_in_window, fmt_price, fmt_usd
from app.models import Detection, Level, ScanContext
from app.settings import PriceChangeParams


class PriceChangeDetector(Detector[PriceChangeParams]):
    key = "price_change"
    name = "Резкое изменение цены"
    short = "ЦЕНА"

    def detect(self, ctx: ScanContext, p: PriceChangeParams) -> Detection | None:
        window = candles_in_window(ctx.candles, ctx.now_ms, p.window_minutes)
        if len(window) < 2:
            return None
        ref = window[0].open
        if ref <= 0:
            return None
        volume = sum(c.quote_volume for c in window)
        if volume < p.min_volume_usdt:
            return None

        last = window[-1].close
        change = (last / ref - 1) * 100
        high = max(c.high for c in window)
        low = min(c.low for c in window)
        # учитываем и движение, которое уже откатилось: прострел на 8% и возврат тоже важен
        up = (high / ref - 1) * 100
        down = (low / ref - 1) * 100
        moves = {"up": up, "down": down}
        if p.direction == "up":
            move = up
        elif p.direction == "down":
            move = down
        else:
            move = max(moves.values(), key=abs)
        if abs(move) < p.threshold_pct:
            return None

        arrow = "рост" if move > 0 else "падение"
        reason = (
            f"За {p.window_minutes} мин {arrow} до {move:+.2f}% от {fmt_price(ref)} "
            f"(экстремум {fmt_price(high if move > 0 else low)}), сейчас {change:+.2f}% "
            f"по {fmt_price(last)}. Оборот за окно {fmt_usd(volume)}."
        )
        return Detection(
            title=f"Цена {move:+.2f}% за {p.window_minutes} мин",
            reason=reason,
            score=round(min(abs(move) / (p.threshold_pct * 3), 1.0), 3),
            metrics={
                "Макс. движение, %": round(move, 3),
                "Сейчас, %": round(change, 3),
                "Цена начала окна": ref,
                "Оборот, USDT": round(volume, 2),
            },
            levels=[Level(price=ref, label="Старт движения", color="#8b8fa3")],
        )
