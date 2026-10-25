"""Alert prices for the price ladder: invalidation, level and zone prices for a scan's primary signal."""

from __future__ import annotations

import numpy as np

from scanner.levels import next_zone
from scanner.models import AlertPrice, Level, SignalResult, SymbolAnalysis, Tri

KIND_ORDER = {"break": 0, "hold": 1, "held": 2, "pattern": 3, "context": 4}


def primary_signal(signals: list[SignalResult]) -> SignalResult | None:
    """The signal the ladder is built around: confirmed before pending, then break > hold > held > pattern > context,
    the newest, then the best level. It only picks the alert prices; it never affects ranking."""
    def key(h: SignalResult) -> tuple:
        return (h.status is not Tri.TRUE, KIND_ORDER.get(h.kind, 9), h.age,
                -(h.level.quality if h.level else 0.0), h.name, h.level.id if h.level else "")

    live = [h for h in signals if h.status is not Tri.FALSE]
    return min(live, key=key) if live else None


def _zone_next(lv: Level, sa: SymbolAnalysis, s) -> tuple[float, float, float]:
    return next_zone(lv, sa.last_idx, sa.bars.atr, s.levels.zone_atr_mult)


def build_alerts(sig: SignalResult | None, sa: SymbolAnalysis, s) -> list[AlertPrice]:
    out: list[AlertPrice] = []
    b = sa.bars
    if sig is not None:
        lv = sig.level
        if sig.name in ("support_hold", "resistance_hold", "support_break", "resistance_break") and lv is not None:
            nxt, zlo, zhi = _zone_next(lv, sa, s)
            inval = {
                "support_hold": (zlo, f"close below ${zlo:.2f} — {lv.label} support zone low fails"),
                "resistance_hold": (zhi, f"close above ${zhi:.2f} — {lv.label} resistance zone high fails"),
                "support_break": (zhi, f"close back above ${zhi:.2f} — reclaim of broken {lv.label}"),
                "resistance_break": (zlo, f"close back below ${zlo:.2f} — failed breakout of {lv.label}"),
            }[sig.name]
            out.append(AlertPrice("Invalidation", *inval))
            out += [AlertPrice("Level", nxt, f"{lv.label} value (next bar)"),
                    AlertPrice("Zone low", zlo, f"{lv.label} zone low (next bar)"),
                    AlertPrice("Zone high", zhi, f"{lv.label} zone high (next bar)")]
        elif sig.kind == "held" and lv is not None:
            nxt, zlo, zhi = _zone_next(lv, sa, s)
            if sig.evidence.get("held_side") == "support":
                out.append(AlertPrice("Invalidation", zlo, f"close below ${zlo:.2f} — {lv.label} stops holding"))
            else:
                out.append(AlertPrice("Invalidation", zhi, f"close above ${zhi:.2f} — {lv.label} stops holding"))
            out += [AlertPrice("Level", nxt, f"{lv.label} value (next bar)"),
                    AlertPrice("Zone low", zlo, f"{lv.label} zone low (next bar)"),
                    AlertPrice("Zone high", zhi, f"{lv.label} zone high (next bar)")]
        elif sig.name in ("bullish_engulfing", "bearish_engulfing"):
            i = sig.bar_idx
            if sig.name == "bullish_engulfing":
                lo_i = float(b.low[i])
                out.append(AlertPrice("Invalidation", lo_i, f"close below engulfing bar low ${lo_i:.2f}"))
            else:
                hi_i = float(b.high[i])
                out.append(AlertPrice("Invalidation", hi_i, f"close above engulfing bar high ${hi_i:.2f}"))
            if lv is not None:
                nxt, zlo, zhi = _zone_next(lv, sa, s)
                out.append(AlertPrice("Level", nxt, f"{lv.label} touched by the engulfing bar"))
        elif sig.name in ("range_bound", "range_mid") and sa.trend.range_high is not None:
            hi, lo = sa.trend.range_high.value(sa.completed_idx), sa.trend.range_low.value(sa.completed_idx)
            band = s.range.mid_band
            out += [AlertPrice("Range high", hi, "range high — a close above ends the range"),
                    AlertPrice("Range low", lo, "range low — a close below ends the range"),
                    AlertPrice("Mid-band high", lo + band[1] * (hi - lo), "top of the middle band"),
                    AlertPrice("Mid-band low", lo + band[0] * (hi - lo), "bottom of the middle band")]
        elif lv is not None:
            nxt, zlo, zhi = _zone_next(lv, sa, s)
            out += [AlertPrice("Zone low", zlo, f"{lv.label} zone low (next bar)"),
                    AlertPrice("Zone high", zhi, f"{lv.label} zone high (next bar)")]
    for row, label, edge in ((sa.nearest_support[:1], "Nearest support", 0), (sa.nearest_resistance[:1],
                                                                              "Nearest resistance", 1)):
        for r in row:
            _, zlo, zhi = _zone_next(r["level"], sa, s)
            price = zlo if edge == 0 else zhi
            out.append(AlertPrice(label, price, f"{r['level'].label} {'zone low' if edge == 0 else 'zone high'}"))
    seen, uniq = set(), []
    for a in out:
        key = (a.label, round(a.price, 4))
        if key not in seen and not np.isnan(a.price):
            seen.add(key)
            uniq.append(a)
    return uniq
