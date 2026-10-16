"""Trend state from pivots (TE-16/17) and range geometry (TE-18).

Comparison of consecutive pivots uses a tolerance tol = cluster_atr_mult × ATR (decision B1):
higher if Δ > tol, equal if |Δ| ≤ tol, lower if Δ < −tol. Hence uptrend (HH+HL), downtrend (LH+LL)
and range (EH+EL + boundaries + price inside) are mutually exclusive; anything else is mixed.
"""

from __future__ import annotations

import numpy as np

from scanner.levels import cluster_prices, recency
from scanner.models import Level, Pivot, TrendResult


def compare(a: float, b: float, tol: float) -> str:
    d = b - a
    if d > tol:
        return "higher"
    if d < -tol:
        return "lower"
    return "equal"


def _boundary(pivots: list[Pivot], last_two: list[Pivot], tol: float, n_bars: int, cutoff: int,
              half_life: float, origin: str, name: str) -> Level:
    """Range boundary: the TE-10 cluster holding both recent pivots, else the pair itself."""
    members = last_two
    for cl in cluster_prices(pivots, tol):
        if all(p in cl for p in last_two):
            members = cl
            break
    price = float(np.median([p.price for p in members]))
    last = max(p.idx for p in members)
    ms = sorted(members, key=lambda p: p.idx)
    return Level(
        id=name, type="horizontal", values=np.full(n_bars, price), origin=origin, touches=len(members),
        anchors=[(p.date, p.price) for p in ms], touch_dates=[p.date for p in ms], last_touch_idx=last,
        quality=len(members) * recency(cutoff - last, half_life),
    )


def classify_trend(close: np.ndarray, atr: np.ndarray, highs: list[Pivot], lows: list[Pivot], cutoff: int,
                   settings) -> TrendResult:
    hs = [p for p in highs if p.confirm_idx <= cutoff]
    ls = [p for p in lows if p.confirm_idx <= cutoff]
    if len(hs) < 2 or len(ls) < 2:
        return TrendResult("mixed", f"too few confirmed pivots ({len(hs)} highs, {len(ls)} lows)", hs[-2:] + ls[-2:])
    a = float(atr[cutoff])
    if np.isnan(a):
        return TrendResult("mixed", "ATR unavailable", [])
    tol = settings.levels.cluster_atr_mult * a
    h1, h2 = hs[-2], hs[-1]
    l1, l2 = ls[-2], ls[-1]
    used = [h1, h2, l1, l2]
    ch, cl = compare(h1.price, h2.price, tol), compare(l1.price, l2.price, tol)
    desc = (f"pivot highs {h1.price:.2f} ({h1.date}) → {h2.price:.2f} ({h2.date}): {ch}; "
            f"pivot lows {l1.price:.2f} ({l1.date}) → {l2.price:.2f} ({l2.date}): {cl}; tol {tol:.2f}")
    if ch == "higher" and cl == "higher":
        return TrendResult("uptrend", "higher highs + higher lows — " + desc, used)
    if ch == "lower" and cl == "lower":
        return TrendResult("downtrend", "lower highs + lower lows — " + desc, used)
    if ch == "equal" and cl == "equal":
        n = len(close)
        hl = settings.levels.recency_half_life_bars
        top = _boundary(hs, [h1, h2], tol, n, cutoff, hl, "resistance", "range_high")
        bot = _boundary(ls, [l1, l2], tol, n, cutoff, hl, "support", "range_low")
        k = settings.levels.zone_atr_mult
        c = float(close[cutoff])
        hi, lo = top.value(cutoff), bot.value(cutoff)
        if hi <= lo:
            return TrendResult("mixed", "range boundaries overlap — " + desc, used)
        if not (lo - k * a <= c <= hi + k * a):
            return TrendResult("mixed", f"equal highs/lows but close {c:.2f} is outside the range — " + desc, used)
        pos = float(np.clip((c - lo) / (hi - lo), 0.0, 1.0))
        band = settings.range.mid_band
        return TrendResult(
            "range", "equal highs + equal lows, price inside — " + desc, used, range_high=top, range_low=bot,
            range_width_atr=(hi - lo) / a, range_position=pos, range_mid=band[0] <= pos <= band[1],
        )
    return TrendResult("mixed", "highs/lows disagree — " + desc, used)
