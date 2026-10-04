"""Широкий спред, в который регулярно наливают.

Если между лучшими бидом и аском несколько процентов, а сделки при этом идут с обеих сторон,
можно встать лимитками внутрь спреда и собирать разницу.
"""

from __future__ import annotations

from app.detectors.base import Detector, fmt_price, fmt_usd, trades_in_window
from app.models import Detection, Level, ScanContext
from app.settings import SpreadParams


class SpreadDetector(Detector[SpreadParams]):
    key = "spread"
    name = "Широкий спред + наливы"
    short = "СПРЕД"
    needs_trades = True

    def detect(self, ctx: ScanContext, p: SpreadParams) -> Detection | None:
        t = ctx.ticker
        if t is None or ctx.trades is None:
            return None
        spread = t.spread_pct
        if spread is None or not (p.min_spread_pct <= spread <= p.max_spread_pct):
            return None

        w = trades_in_window(ctx.trades, ctx.now_ms, p.window_minutes)
        if len(w) < p.min_trades:
            return None
        buys = [x for x in w if x.side == "buy"]
        sells = [x for x in w if x.side == "sell"]
        if p.require_both_sides and (not buys or not sells):
            return None
        volume = sum(x.quote for x in w)
        if volume < p.min_volume_usdt:
            return None

        per_min = len(w) / p.window_minutes
        reason = (
            f"Спред {spread:.2f}%: бид {fmt_price(t.bid)}, аск {fmt_price(t.ask)}. "
            f"За {p.window_minutes:g} мин {len(w)} сделок ({len(buys)} покупок, {len(sells)} продаж), "
            f"оборот {fmt_usd(volume)}."
        )
        return Detection(
            title=f"Спред {spread:.2f}%, {per_min:.1f} сд/мин",
            reason=reason,
            score=round(min(spread / (p.min_spread_pct * 4), 1.0), 3),
            metrics={
                "Спред, %": round(spread, 3),
                "Бид": t.bid,
                "Аск": t.ask,
                "Сделок": len(w),
                "Покупок": len(buys),
                "Продаж": len(sells),
                "Оборот, USDT": round(volume, 2),
            },
            levels=[
                Level(price=t.bid, label="Бид", color="#0ecb81"),
                Level(price=t.ask, label="Аск", color="#f6465d"),
            ],
        )
