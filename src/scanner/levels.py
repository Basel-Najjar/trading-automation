"""Support/resistance levels: SMA, horizontal pivot clusters, diagonal trendlines, manual levels, confluence.

Every builder takes `cutoff`, the last bar whose data may shape structure (pivots, clusters, lines).
Level `values` are defined for every bar in the window so zones can be evaluated on any bar, including a
provisional bar after the cutoff (where SMA values come from that bar and lines are extrapolated).
"""

from __future__ import annotations

import bisect
from itertools import combinations

import numpy as np

from scanner.models import Bars, Level, Pivot


def recency(bars_since: int, half_life: float) -> float:
    return float(0.5 ** (max(bars_since, 0) / half_life)) if half_life > 0 else 1.0


def zone_at(level: Level, t: int, bars: Bars, k: float) -> tuple[float, float]:
    return level.zone(t, bars.atr, k)


def sma_levels(bars: Bars, cutoff: int, settings) -> list[Level]:
    out = []
    base = settings.levels.sma_base_quality
    for name, arr in (("sma50", bars.sma50), ("sma200", bars.sma200)):
        if np.isnan(arr[cutoff]):
            continue  # insufficient history (partial analysis) → n/a
        slope = float(arr[cutoff] - arr[cutoff - 1]) if cutoff > 0 and not np.isnan(arr[cutoff - 1]) else 0.0
        out.append(Level(id=name, type=name, values=arr, slope=slope, quality=float(base.get(name, 1.0))))
    return out


def cluster_prices(pivots: list[Pivot], tol: float) -> list[list[Pivot]]:
    """Greedy clustering on sorted price: a cluster's max − min never exceeds `tol` (deterministic)."""
    clusters: list[list[Pivot]] = []
    for p in sorted(pivots, key=lambda p: (p.price, p.idx)):
        if clusters and p.price - clusters[-1][0].price <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return clusters


def _usable(pivots: list[Pivot], cutoff: int, lookback: int | None) -> list[Pivot]:
    lo = -1 if lookback is None else cutoff - lookback
    return [p for p in pivots if p.confirm_idx <= cutoff and p.idx >= lo]


def horizontal_levels(bars: Bars, highs: list[Pivot], lows: list[Pivot], cutoff: int, settings) -> list[Level]:
    ls = settings.levels
    atr = float(bars.atr[cutoff])
    if np.isnan(atr):
        return []
    tol = ls.cluster_atr_mult * atr
    out: list[Level] = []
    for origin, pivots, tag in (("resistance", highs, "res"), ("support", lows, "sup")):
        clusters = [c for c in cluster_prices(_usable(pivots, cutoff, ls.horizontal_lookback_bars), tol)
                    if len(c) >= ls.min_touches_horizontal]
        for n, cl in enumerate(clusters, start=1):
            price = float(np.median([p.price for p in cl]))
            last = max(p.idx for p in cl)
            by_date = sorted(cl, key=lambda p: p.idx)
            out.append(Level(
                id=f"h_{tag}_{n}", type="horizontal", values=np.full(len(bars), price), origin=origin,
                touches=len(cl), anchors=[(p.date, p.price) for p in by_date],
                touch_dates=[p.date for p in by_date], last_touch_idx=last,
                quality=len(cl) * recency(cutoff - last, ls.recency_half_life_bars),
            ))
    return out


def _line(y_a: float, i_a: int, y_b: float, i_b: int, n: int, log: bool) -> np.ndarray:
    slope = (y_b - y_a) / (i_b - i_a)
    y = y_a + slope * (np.arange(n) - i_a)
    return np.exp(y) if log else y


def trendlines(bars: Bars, highs: list[Pivot], lows: list[Pivot], cutoff: int, settings) -> list[Level]:
    """Best pivot-pair lines per side (OQ-5 / TE-11).

    Candidate = any pair of confirmed same-side pivots within the lookback. A candidate is discarded if any
    close from its first anchor up to `cutoff − max_signal_age_bars − 1` is beyond its zone (closes through
    the line between anchors, or since the second anchor). Excluding the most recent bars keeps a line
    alive on the bar(s) whose break is the current signal. Touches = same-side pivots inside the zone.
    """
    ls, k = settings.levels, settings.levels.zone_atr_mult
    log = ls.trendline_scale == "log"
    n = len(bars)
    inv_end = cutoff - settings.signals.max_signal_age_bars - 1
    close, atr = bars.close, bars.atr
    out: list[Level] = []
    for origin, pivots, tag in (("support", lows, "sup"), ("resistance", highs, "res")):
        piv = _usable(pivots, cutoff, ls.trendline_lookback_bars)
        cands = []
        for a, b in combinations(piv, 2):
            if log and (a.price <= 0 or b.price <= 0):
                continue
            ya, yb = (np.log(a.price), np.log(b.price)) if log else (a.price, b.price)
            vals = _line(ya, a.idx, yb, b.idx, n, log)
            if not vals[cutoff] > 0:
                continue
            seg = slice(a.idx + 1, inv_end + 1)
            with np.errstate(invalid="ignore"):
                if origin == "support":
                    bad = close[seg] < vals[seg] - k * atr[seg]
                else:
                    bad = close[seg] > vals[seg] + k * atr[seg]
            if np.any(bad):
                continue
            touches = [p for p in piv if abs(p.price - vals[p.idx]) <= k * atr[p.idx] + 1e-12]
            if len(touches) < ls.min_touches_diagonal:
                continue
            last = max(p.idx for p in touches)
            q = len(touches) * recency(cutoff - last, ls.recency_half_life_bars)
            cands.append((q, b.idx, a.idx, a, b, vals, touches))
        cands.sort(key=lambda c: (-c[0], -c[1], -c[2]))
        kept: list[tuple] = []
        for c in cands:
            vals, a = c[5], c[3]
            dup = False
            for kc in kept:
                kv = kc[5]
                i0 = min(a.idx, kc[3].idx)
                if (abs(vals[cutoff] - kv[cutoff]) <= 2 * k * atr[cutoff]
                        and abs(vals[i0] - kv[i0]) <= 2 * k * (atr[i0] if not np.isnan(atr[i0]) else atr[cutoff])):
                    dup = True
                    break
            if not dup:
                kept.append(c)
            if len(kept) >= ls.max_trendlines_per_side:
                break
        for m, (q, _, _, a, b, vals, touches) in enumerate(kept, start=1):
            ts = sorted(touches, key=lambda p: p.idx)
            out.append(Level(
                id=f"d_{tag}_{m}", type="diagonal", values=vals, origin=origin, touches=len(touches),
                anchors=[(a.date, a.price), (b.date, b.price)], touch_dates=[p.date for p in ts],
                last_touch_idx=ts[-1].idx, slope=float(vals[cutoff] - vals[cutoff - 1]), quality=q,
            ))
    return out


def manual_levels(bars: Bars, manual: list, cutoff: int, settings) -> tuple[list[Level], list[str]]:
    """Owner-drawn levels (TE-15). Diagonal anchors after the cutoff date are excluded (no look-ahead)."""
    out, notes = [], []
    n = len(bars)
    log = settings.levels.trendline_scale == "log"
    cutoff_date = bars.dates[cutoff]
    for m, ml in enumerate(manual, start=1):
        if ml.type == "horizontal":
            out.append(Level(id=f"m_{m}", type="horizontal", values=np.full(n, float(ml.price)), origin=ml.role,
                             quality=settings.levels.manual_quality, manual=True, note=ml.note))
            continue
        (d1, p1), (d2, p2) = ml.anchors
        if d2 > cutoff_date:
            notes.append(f"manual diagonal m_{m} skipped: anchor {d2} is after {cutoff_date}")
            continue
        i1 = bisect.bisect_right(bars.dates, d1) - 1
        i2 = bisect.bisect_right(bars.dates, d2) - 1
        if i1 < 0 or i2 <= i1:
            notes.append(f"manual diagonal m_{m} skipped: anchors outside the analysis window")
            continue
        ya, yb = (np.log(p1), np.log(p2)) if log else (p1, p2)
        vals = _line(float(ya), i1, float(yb), i2, n, log)
        out.append(Level(id=f"m_{m}", type="diagonal", values=vals, origin=ml.role,
                         anchors=[(d1, p1), (d2, p2)], slope=float(vals[cutoff] - vals[cutoff - 1]),
                         quality=settings.levels.manual_quality, manual=True, note=ml.note))
    return out, notes


def build_levels(bars: Bars, highs: list[Pivot], lows: list[Pivot], cutoff: int, settings,
                 manual: list | None = None) -> tuple[list[Level], list[str]]:
    levels = sma_levels(bars, cutoff, settings)
    levels += horizontal_levels(bars, highs, lows, cutoff, settings)
    levels += trendlines(bars, highs, lows, cutoff, settings)
    notes: list[str] = []
    if manual:
        ml, notes = manual_levels(bars, manual, cutoff, settings)
        levels += ml
    return levels, notes


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def confluence_types(level: Level, levels: list[Level], t: int, bars: Bars, k: float) -> list[str]:
    """Distinct level types whose zones overlap `level`'s zone on bar t (includes level's own type)."""
    z = level.zone(t, bars.atr, k)
    types = {level.type}
    for other in levels:
        if other is level:
            continue
        oz = other.zone(t, bars.atr, k)
        if not np.isnan(oz[0]) and _overlap(z, oz):
            types.add(other.type)
    return sorted(types)


def confluence_zones(levels: list[Level], t: int, bars: Bars, k: float) -> list[dict]:
    """Merged zones (transitive overlap) containing ≥ 2 distinct level types on bar t (TE-13)."""
    items = []
    for lv in levels:
        z = lv.zone(t, bars.atr, k)
        if not np.isnan(z[0]):
            items.append((z[0], z[1], lv))
    items.sort(key=lambda x: (x[0], x[1], x[2].id))
    groups: list[list] = []
    for lo, hi, lv in items:
        if groups and lo <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], hi)
            groups[-1][2].append(lv)
        else:
            groups.append([lo, hi, [lv]])
    out = []
    for lo, hi, members in groups:
        types = sorted({m.type for m in members})
        if len(types) >= 2:
            out.append({"zone_low": lo, "zone_high": hi, "sources": [m.id for m in members], "types": types,
                        "level": float(np.mean([m.value(t) for m in members]))})
    return out


def nearest_levels(levels: list[Level], t: int, bars: Bars, n: int) -> tuple[list[dict], list[dict]]:
    """Nearest n supports below and n resistances above the close on bar t (TE-14)."""
    c, atr = float(bars.close[t]), float(bars.atr[t])
    sup, res = [], []
    for lv in levels:
        v = lv.value(t)
        if np.isnan(v):
            continue
        row = {"level": lv, "value": v, "dist_pct": (v - c) / c * 100, "dist_atr": (v - c) / atr if atr else np.nan}
        (sup if v < c else res).append(row)
    sup.sort(key=lambda r: (-r["value"], r["level"].id))
    res.sort(key=lambda r: (r["value"], r["level"].id))
    return sup[:n], res[:n]


def project_next(level: Level, t: int) -> float:
    """Level value one bar ahead (linear extrapolation for SMAs/diagonals; constant for horizontals)."""
    v = level.value(t)
    if level.type == "horizontal":
        return v
    if level.type == "diagonal" and t + 1 < len(level.values):
        return level.value(t + 1)
    prev = level.value(t - 1)
    return v + (v - prev) if not np.isnan(prev) else v
