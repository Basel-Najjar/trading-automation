"""Signal vocabulary (§6.4). Each signal is a small function registered in REGISTRY (NF-7).

Signal function contract: fn(tech, i, s) -> list[(level | None, evidence dict)], one entry per firing
(level-based signals can fire on several levels). `i` is the evaluation bar; `tech` holds structure built
from bars ≤ tech.cutoff. If i > tech.cutoff the bar is provisional and results become PENDING_CLOSE.

Near function contract (watch list, WL-1): near(tech, i, s) -> list[NearResult] for a completed bar i.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from scanner.levels import next_zone
from scanner.models import (
    FLAT_LEVEL_TYPES,
    SIGNAL_NAMES,
    AlertPrice,
    Bars,
    Level,
    NearResult,
    SignalResult,
    Tech,
    Tri,
)

Firing = tuple[Level | None, dict]


@dataclass(frozen=True)
class SignalDef:
    name: str
    kind: str  # break | hold | held | pattern | context
    direction: str  # bullish | bearish | neutral
    fn: Callable[[Tech, int, object], list[Firing]]
    params: tuple[str, ...]
    description: str
    near: Callable[[Tech, int, object], list[NearResult]] | None = None


REGISTRY: dict[str, SignalDef] = {}


def register(name: str, kind: str, direction: str, params: tuple[str, ...], description: str):
    if name not in SIGNAL_NAMES:
        raise ValueError(f"{name} must be added to models.SIGNAL_NAMES")

    def deco(fn):
        REGISTRY[name] = SignalDef(name, kind, direction, fn, params, description)
        return fn

    return deco


def register_near(name: str):
    def deco(fn):
        d = REGISTRY[name]
        REGISTRY[name] = SignalDef(d.name, d.kind, d.direction, d.fn, d.params, d.description, fn)
        return fn

    return deco


# ----------------------------------------------------------------------------------------------
# Candle helpers
# ----------------------------------------------------------------------------------------------


def body(b: Bars, t: int) -> float:
    return abs(b.close[t] - b.open[t])


def bar_range(b: Bars, t: int) -> float:
    return b.high[t] - b.low[t]


def body_ratio(b: Bars, t: int) -> float:
    r = bar_range(b, t)
    return body(b, t) / r if r > 0 else 0.0


def is_bull(b: Bars, t: int) -> bool:
    return b.close[t] > b.open[t]


def is_bear(b: Bars, t: int) -> bool:
    return b.close[t] < b.open[t]


def is_full_body(b: Bars, t: int, ratio: float) -> bool:
    return bar_range(b, t) > 0 and body_ratio(b, t) >= ratio


def engulfs(b: Bars, t: int, tol: float, require_opposite: bool) -> bool:
    """Bar t's body covers bar t−1's body (± tol). Optionally the two bodies must have opposite colours."""
    if t < 1:
        return False
    top, bot = max(b.open[t], b.close[t]), min(b.open[t], b.close[t])
    ptop, pbot = max(b.open[t - 1], b.close[t - 1]), min(b.open[t - 1], b.close[t - 1])
    if not (top >= ptop - tol and bot <= pbot + tol):
        return False
    if require_opposite:
        return (is_bull(b, t) and is_bear(b, t - 1)) or (is_bear(b, t) and is_bull(b, t - 1))
    return True


def _eng_tol(b: Bars, t: int, s) -> float:
    a = b.atr[t]
    return s.signals.engulfing_tolerance_atr * (0.0 if np.isnan(a) else float(a))


def _r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


def _lvl_ev(lv: Level, t: int, b: Bars, k: float) -> dict:
    lo, hi = lv.zone(t, b.atr, k)
    return {"level_id": lv.id, "level_type": lv.label, "level": _r(lv.value(t)), "zone_low": _r(lo),
            "zone_high": _r(hi), "atr": _r(b.atr[t])}


def _valid(lv: Level, *ts: int) -> bool:
    n = len(lv.values)
    return all(0 <= t < n and not math.isnan(lv.values[t]) for t in ts)


# ----------------------------------------------------------------------------------------------
# Hold signals (1-bar confirmation)
# ----------------------------------------------------------------------------------------------


def _hold_test(tech: Tech, lv: Level, i: int, s, side: str, window_end: int) -> int | None:
    """Most recent bar t in (window_end − lookback, window_end] that tested the zone from `side`."""
    b, k = tech.bars, s.levels.zone_atr_mult
    lb = s.signals.hold_test_lookback
    for t in range(min(window_end, i), max(window_end - lb, 0), -1):
        if not _valid(lv, t, t - 1) or np.isnan(b.atr[t]) or np.isnan(b.atr[t - 1]):
            continue
        lo, hi = lv.zone(t, b.atr, k)
        plo, phi = lv.zone(t - 1, b.atr, k)
        if side == "support":
            tested = b.low[t] <= hi and b.close[t] >= lo
            prior_ok = b.close[t - 1] >= plo
        else:
            tested = b.high[t] >= lo and b.close[t] <= hi
            prior_ok = b.close[t - 1] <= phi
        if tested and (prior_ok or not s.signals.hold_require_prior_side):
            return t
    return None


def _hold(tech: Tech, i: int, s, side: str) -> list[Firing]:
    b, k = tech.bars, s.levels.zone_atr_mult
    if i < 1:
        return []
    confirmed = b.close[i] > b.high[i - 1] if side == "support" else b.close[i] < b.low[i - 1]
    if not confirmed:
        return []
    broken = {lv.id for lv, _ in _break(tech, i, s, "resistance" if side == "support" else "support")}
    out = []
    for lv in tech.levels:
        if lv.id in broken:  # decision B5: the bar broke this level from the other side; it is a break, not a hold
            continue
        t = _hold_test(tech, lv, i, s, side, i)
        if t is None:
            continue
        ev = _lvl_ev(lv, t, b, k)
        ev.update({
            "test_bar": b.ohlc(t), "confirm_bar": b.ohlc(i), "prior_bar": b.ohlc(i - 1),
            "confirmation": (f"close {b.close[i]:.2f} > prior high {b.high[i - 1]:.2f}" if side == "support"
                             else f"close {b.close[i]:.2f} < prior low {b.low[i - 1]:.2f}"),
            "dist_from_level_atr": _r(abs(b.close[i] - lv.value(i)) / b.atr[i], 3),
        })
        out.append((lv, ev))
    return out


@register("support_hold", "hold", "bullish",
          ("hold_test_lookback", "hold_require_prior_side", "zone_atr_mult"),
          "Within the last `hold_test_lookback` bars a bar's low entered the support zone (low ≤ zone_high) "
          "and closed ≥ zone_low, coming from above (prior close ≥ zone_low); bar N closes above bar N-1's high.")
def support_hold(tech: Tech, i: int, s) -> list[Firing]:
    return _hold(tech, i, s, "support")


@register("resistance_hold", "hold", "bearish",
          ("hold_test_lookback", "hold_require_prior_side", "zone_atr_mult"),
          "Mirror of support_hold: a bar's high entered the resistance zone (high ≥ zone_low) and closed "
          "≤ zone_high, coming from below; bar N closes below bar N-1's low.")
def resistance_hold(tech: Tech, i: int, s) -> list[Firing]:
    return _hold(tech, i, s, "resistance")


def _hold_near(tech: Tech, i: int, s, side: str) -> list[NearResult]:
    b, k = tech.bars, s.levels.zone_atr_mult
    out = []
    a = float(b.atr[i])
    if np.isnan(a) or a <= 0:
        return out
    already = {lv.id for lv, _ in _hold(tech, i, s, side)}
    for lv in tech.levels:
        if lv.id in already or not _valid(lv, i):
            continue
        nxt, zlo, zhi = next_zone(lv, i, b.atr, k)
        # (a) zone tested within a window that is still open next bar, confirmation not yet printed
        t = _hold_test(tech, lv, i, s, side, i)
        if t is not None and t > i + 1 - s.signals.hold_test_lookback:
            if side == "support":
                trig = AlertPrice("Hold trigger", float(b.high[i]), f"close above ${b.high[i]:.2f} (prior bar's high)")
                inval = AlertPrice("Invalidation", zlo, f"close below ${zlo:.2f} ({lv.label} zone low)")
                dist = max(0.0, (b.high[i] - b.close[i]) / a)
            else:
                trig = AlertPrice("Hold trigger", float(b.low[i]), f"close below ${b.low[i]:.2f} (prior bar's low)")
                inval = AlertPrice("Invalidation", zhi, f"close above ${zhi:.2f} ({lv.label} zone high)")
                dist = max(0.0, (b.close[i] - b.low[i]) / a)
            out.append(NearResult(f"{side}_hold", lv, f"{side} test at {lv.label} on {b.dates[t]}; "
                                  "1-bar confirmation not printed yet", dist,
                                  [trig, inval, *_zone_alerts(lv, nxt, zlo, zhi)]))
            continue
        # (b) approaching the zone from the correct side
        c = float(b.close[i])
        if side == "support" and c > zhi and (c - zhi) / a <= s.signals.approach_atr:
            out.append(NearResult("support_hold", lv, f"approaching {lv.label} support", (c - zhi) / a,
                                  [AlertPrice("Zone entry", zhi, f"price trades into support zone at ${zhi:.2f}"),
                                   *_zone_alerts(lv, nxt, zlo, zhi)]))
        elif side == "resistance" and c < zlo and (zlo - c) / a <= s.signals.approach_atr:
            out.append(NearResult("resistance_hold", lv, f"approaching {lv.label} resistance", (zlo - c) / a,
                                  [AlertPrice("Zone entry", zlo, f"price trades into resistance zone at ${zlo:.2f}"),
                                   *_zone_alerts(lv, nxt, zlo, zhi)]))
    return out


def _zone_alerts(lv: Level, nxt: float, zlo: float, zhi: float) -> list[AlertPrice]:
    proj = " (projected next bar)" if lv.type in ("sma50", "sma200", "diagonal") else ""
    return [AlertPrice("Level", nxt, f"{lv.label} value{proj}"),
            AlertPrice("Zone low", zlo, f"{lv.label} zone low{proj}"),
            AlertPrice("Zone high", zhi, f"{lv.label} zone high{proj}")]


@register_near("support_hold")
def support_hold_near(tech: Tech, i: int, s) -> list[NearResult]:
    return _hold_near(tech, i, s, "support")


@register_near("resistance_hold")
def resistance_hold_near(tech: Tech, i: int, s) -> list[NearResult]:
    return _hold_near(tech, i, s, "resistance")


# ----------------------------------------------------------------------------------------------
# Held signals (passive: the level was tested and every close since stayed on its side)
# ----------------------------------------------------------------------------------------------


def _held_test(lv: Level, b: Bars, i: int, k: float, side: str, swing: int) -> bool:
    """Bar i tested the zone from `side` and closed without going through it; bar i−1 closed on that side; and bar i
    is a pullback extreme: its low is the lowest of the `swing` bars before it (support), its high the highest."""
    lo, hi = lv.zone(i, b.atr, k)
    plo, phi = lv.zone(i - 1, b.atr, k)
    before = slice(max(i - swing, 0), i)
    if side == "support":
        return bool(b.low[i] <= hi and b.close[i] >= lo and b.close[i - 1] >= plo
                    and b.low[i] <= b.low[before].min())
    return bool(b.high[i] >= lo and b.close[i] <= hi and b.close[i - 1] <= phi
                and b.high[i] >= b.high[before].max())


def _held(tech: Tech, i: int, s, which: str) -> list[Firing]:
    """`which`: support / resistance (horizontal levels only) or rising / falling (any sloped level, tested from
    whichever side price was on). Whether later closes kept the level intact is checked in `evaluate_signals`."""
    b, k = tech.bars, s.levels.zone_atr_mult
    if i < 1 or np.isnan(b.atr[i]) or np.isnan(b.atr[i - 1]):
        return []
    out = []
    for lv in tech.levels:
        if not _valid(lv, i, i - 1):
            continue
        slope = lv.slope_dir(i)
        if which in ("support", "resistance"):
            if slope != "flat":
                continue
            side = which
        else:
            if slope != which:
                continue
            side = "support" if b.close[i - 1] >= lv.value(i - 1) else "resistance"
        if not lv.known_before(b.dates[i], s.levels.min_touches_horizontal) or \
                not _held_test(lv, b, i, k, side, s.signals.held_swing_bars):
            continue
        ev = _lvl_ev(lv, i, b, k)
        ev.update({"test_bar": b.ohlc(i), "held_side": side, "slope": slope,
                   "slope_per_bar": _r(lv.value(i) - lv.value(i - 1))})
        out.append((lv, ev))
    return out


def still_held(sig: SignalResult, b: Bars, end: int, k: float) -> bool:
    """Every bar after the test up to bar `end` stayed on the held side: no close through the level's zone, and no
    low below the test bar's low (support) or high above its high (resistance), so the test is still the swing."""
    lv, side, i = sig.level, sig.evidence["held_side"], sig.bar_idx
    for t in range(i + 1, end + 1):
        lo, hi = lv.zone(t, b.atr, k)
        if side == "support" and (b.close[t] < lo or b.low[t] < b.low[i]):
            return False
        if side == "resistance" and (b.close[t] > hi or b.high[t] > b.high[i]):
            return False
    return True


_HELD_PARAMS = ("zone_atr_mult", "held_swing_bars", "min_touches_horizontal")
_HELD_RULE = ("The test bar must be a pullback extreme (its low the lowest of the `held_swing_bars` bars before it; "
              "its high the highest, for resistance), the level must already have existed before the test (its "
              "anchors predate the test bar), and since the test no completed bar may have closed through the zone "
              "or gone beyond the test bar's extreme.")


@register("support_held", "held", "bullish", _HELD_PARAMS,
          "Horizontal support held: a bar's low entered the support zone (low ≤ zone_high) and it closed ≥ zone_low, "
          "coming from above (prior close ≥ zone_low). " + _HELD_RULE)
def support_held(tech: Tech, i: int, s) -> list[Firing]:
    return _held(tech, i, s, "support")


@register("resistance_held", "held", "bearish", _HELD_PARAMS,
          "Horizontal resistance held: a bar's high entered the resistance zone (high ≥ zone_low) and it closed "
          "≤ zone_high, coming from below (prior close ≤ zone_high). " + _HELD_RULE)
def resistance_held(tech: Tech, i: int, s) -> list[Firing]:
    return _held(tech, i, s, "resistance")


@register("rising_line_held", "held", "bullish", _HELD_PARAMS,
          "A rising line (diagonal trendline or SMA, support or resistance) held: a bar tested its zone from the side "
          "price was on and closed without going through. " + _HELD_RULE)
def rising_line_held(tech: Tech, i: int, s) -> list[Firing]:
    return _held(tech, i, s, "rising")


@register("falling_line_held", "held", "bearish", _HELD_PARAMS,
          "A falling line (diagonal trendline or SMA, support or resistance) held: a bar tested its zone from the side "
          "price was on and closed without going through. " + _HELD_RULE)
def falling_line_held(tech: Tech, i: int, s) -> list[Firing]:
    return _held(tech, i, s, "falling")


# ----------------------------------------------------------------------------------------------
# Break signals (2-bar close beyond the zone with strong confirmation; OQ-1, decision B5)
# ----------------------------------------------------------------------------------------------


def _beyond(lv: Level, b: Bars, t: int, k: float, side: str) -> bool:
    """Bar t closed beyond the zone on the breaking side (below zone_low for support, above zone_high otherwise)."""
    lo, hi = lv.zone(t, b.atr, k)
    return bool(b.close[t] < lo) if side == "support" else bool(b.close[t] > hi)


def _prior_side_ok(lv: Level, b: Bars, end: int, k: float, n: int, side: str) -> bool:
    """Each of the `n` closes ending at bar `end` stayed on the level's side (none beyond the zone)."""
    ts = range(end - n + 1, end + 1)
    if end - n + 1 < 0 or not all(_valid(lv, t) and not np.isnan(b.atr[t]) for t in ts):
        return False
    return not any(_beyond(lv, b, t, k, side) for t in ts)


def _break_conditions(tech: Tech, lv: Level, i: int, s, side: str) -> dict | None:
    b, k, sig = tech.bars, s.levels.zone_atr_mult, s.signals
    if i < 1 or not _valid(lv, i, i - 1) or np.isnan(b.atr[i]) or np.isnan(b.atr[i - 1]):
        return None
    lo, hi = lv.zone(i, b.atr, k)
    strong = is_bear if side == "support" else is_bull
    beyond = (lo - b.close[i]) / b.atr[i] if side == "support" else (b.close[i] - hi) / b.atr[i]
    n = sig.break_prior_side_bars
    return {"closes_beyond_zone": _beyond(lv, b, i, k, side),
            "prior_closes_beyond_zone": _beyond(lv, b, i - 1, k, side),
            "full_bodied": bool(is_full_body(b, i, sig.full_body_ratio) and strong(b, i)),
            "first_full_bodied": bool(is_full_body(b, i - 1, sig.full_body_ratio) and strong(b, i - 1)),
            "beyond_prior_extreme": bool(b.close[i] < b.low[i - 1] if side == "support"
                                         else b.close[i] > b.high[i - 1]),
            "engulfs_prior": engulfs(b, i, _eng_tol(b, i, s), sig.break_engulfing_require_opposite_color),
            "prior_side": _prior_side_ok(lv, b, i - 2, k, n, side),          # before the 2-bar break
            "prior_side_next": _prior_side_ok(lv, b, i - 1, k, n, side),     # if bar i is the first close beyond
            "beyond_atr": float(beyond), "in_zone": bool(lo <= b.close[i] <= hi)}


def break_requirements(s) -> tuple[str, ...]:
    """The `_break_conditions` keys a break needs, by `break_confirm`: `prior_extreme` (both bars strong candles in
    the break direction, the second closing beyond the first's high/low) or `engulf` (the second full-bodied and
    engulfing the first)."""
    common = ("closes_beyond_zone", "prior_closes_beyond_zone", "full_bodied", "prior_side")
    if s.signals.break_confirm == "engulf":
        return (*common, "engulfs_prior")
    return (*common, "first_full_bodied", "beyond_prior_extreme")


def _break(tech: Tech, i: int, s, side: str) -> list[Firing]:
    """Breaks of `side` levels on bar i. Memoised on the structure: holds and near-misses re-ask the same question
    (keyed on every setting a break reads, so runs with different settings stay separate)."""
    key = ("break", i, side, s.levels.zone_atr_mult, tuple(s.signals.model_dump().items()))
    if key not in tech.memo:
        tech.memo[key] = _find_breaks(tech, i, s, side)
    return tech.memo[key]


def _find_breaks(tech: Tech, i: int, s, side: str) -> list[Firing]:
    b, k = tech.bars, s.levels.zone_atr_mult
    need = break_requirements(s)
    out = []
    for lv in tech.levels:
        if not (_beyond(lv, b, i, k, side) and _beyond(lv, b, i - 1, k, side)):
            continue  # cheap check first: most levels are not broken
        c = _break_conditions(tech, lv, i, s, side)
        if c is None or not all(c[x] for x in need):
            continue
        ev = _lvl_ev(lv, i, b, k)
        ev.update({"break_bar": b.ohlc(i), "first_close_bar": b.ohlc(i - 1), "body_ratio": _r(body_ratio(b, i), 3),
                   "first_body_ratio": _r(body_ratio(b, i - 1), 3), "confirm": s.signals.break_confirm,
                   "prior_side_bars": s.signals.break_prior_side_bars, "beyond_zone_atr": _r(c["beyond_atr"], 3),
                   "stretched": c["beyond_atr"] > s.signals.chase_atr})
        out.append((lv, ev))
    return out


_BREAK_PARAMS = ("full_body_ratio", "break_confirm", "break_engulfing_require_opposite_color",
                 "engulfing_tolerance_atr", "break_prior_side_bars", "chase_atr")


@register("support_break", "break", "bearish", _BREAK_PARAMS,
          "2-bar bearish close through support: bars N-1 and N both close below zone_low, and the "
          "`break_prior_side_bars` closes before N-1 were all ≥ zone_low (price came from the support side, so a "
          "reclaim of a level price was already beyond is not a break). With `break_confirm` = prior_extreme both "
          "bars are full-bodied bearish and N closes below N-1's low; with engulf, N is full-bodied bearish and its "
          "body engulfs N-1's.")
def support_break(tech: Tech, i: int, s) -> list[Firing]:
    return _break(tech, i, s, "support")


@register("resistance_break", "break", "bullish", _BREAK_PARAMS,
          "2-bar bullish close through resistance, the mirror of support_break: N-1 and N both close above "
          "zone_high after `break_prior_side_bars` closes ≤ zone_high. With `break_confirm` = prior_extreme both "
          "bars are full-bodied bullish and N closes above N-1's high; with engulf, N is full-bodied bullish and "
          "engulfs N-1.")
def resistance_break(tech: Tech, i: int, s) -> list[Firing]:
    return _break(tech, i, s, "resistance")


def _break_near(tech: Tech, i: int, s, side: str) -> list[NearResult]:
    b, k = tech.bars, s.levels.zone_atr_mult
    a = float(b.atr[i])
    out = []
    if np.isnan(a) or a <= 0:
        return out
    fired = {lv.id for lv, _ in _break(tech, i, s, side)}
    word = "below" if side == "support" else "above"
    engulf = s.signals.break_confirm == "engulf"
    extreme = "low" if side == "support" else "high"
    for lv in tech.levels:
        if lv.id in fired:
            continue
        c = _break_conditions(tech, lv, i, s, side)
        if c is None:
            continue
        nxt, zlo, zhi = next_zone(lv, i, b.atr, k)
        edge = zlo if side == "support" else zhi
        edge_name = f"{lv.label} zone {'low' if side == 'support' else 'high'}"
        if c["in_zone"] and c["prior_side_next"]:
            dist = (b.close[i] - zlo) / a if side == "support" else (zhi - b.close[i]) / a
            desc = f"price in {lv.label} zone; no break yet (needs 2 closes {word} the zone)"
            note = (" — first of 2 closes; the second must be full-bodied and engulf the first" if engulf else
                    f" — first of 2 strong closes; the second must close {word} the first's {extreme}")
        elif c["closes_beyond_zone"] and not c["prior_closes_beyond_zone"] and c["prior_side_next"]:
            if not engulf and not c["full_bodied"]:
                continue  # a weak first close cannot start a prior_extreme break
            dist = 0.0
            if engulf:
                desc = f"first close {word} {lv.label} zone; needs a second full-bodied, engulfing close {word} it"
                note = " — second close; must be full-bodied and engulf this bar's body"
            else:
                desc = (f"first strong close {word} {lv.label} zone; needs a second strong close {word} this bar's "
                        f"{extreme}")
                edge = float(b.low[i] if side == "support" else b.high[i])
                edge_name = f"this bar's {extreme}"
                note = " — second close; must be full-bodied"
        else:
            continue
        trig = AlertPrice("Break trigger", edge, f"close {word} ${edge:.2f} ({edge_name}){note}")
        out.append(NearResult(f"{side}_break", lv, desc, float(max(dist, 0.0)),
                              [trig, *_zone_alerts(lv, nxt, zlo, zhi)]))
    return out


@register_near("support_break")
def support_break_near(tech: Tech, i: int, s) -> list[NearResult]:
    return _break_near(tech, i, s, "support")


@register_near("resistance_break")
def resistance_break_near(tech: Tech, i: int, s) -> list[NearResult]:
    return _break_near(tech, i, s, "resistance")


# ----------------------------------------------------------------------------------------------
# Pattern signals
# ----------------------------------------------------------------------------------------------


def _engulfing(tech: Tech, i: int, s, bullish: bool) -> list[Firing]:
    b, k = tech.bars, s.levels.zone_atr_mult
    if i < 1:
        return []
    colour_ok = is_bull(b, i) if bullish else is_bear(b, i)
    if not (colour_ok and engulfs(b, i, _eng_tol(b, i, s), s.signals.pattern_engulfing_require_opposite_color)):
        return []
    touched = []
    for lv in tech.levels:
        if not _valid(lv, i):
            continue
        lo, hi = lv.zone(i, b.atr, k)
        if b.low[i] <= hi and b.high[i] >= lo:
            touched.append(lv)
    best = max(touched, key=lambda lv: (lv.quality, lv.id), default=None)
    ev = {"engulfing_bar": b.ohlc(i), "prior_bar": b.ohlc(i - 1), "body_ratio": _r(body_ratio(b, i), 3),
          "at_level": bool(touched), "levels_touched": [lv.id for lv in touched]}
    if best is not None:
        ev.update(_lvl_ev(best, i, b, k))
    return [(best, ev)]


@register("bullish_engulfing", "pattern", "bullish",
          ("pattern_engulfing_require_opposite_color", "engulfing_tolerance_atr"),
          "Bar N is bullish and its body engulfs bar N-1's body (classic: N-1 bearish when "
          "`pattern_engulfing_require_opposite_color`). `at_level` records whether the bar touched any zone.")
def bullish_engulfing(tech: Tech, i: int, s) -> list[Firing]:
    return _engulfing(tech, i, s, True)


@register("bearish_engulfing", "pattern", "bearish",
          ("pattern_engulfing_require_opposite_color", "engulfing_tolerance_atr"),
          "Bar N is bearish and its body engulfs bar N-1's body. `at_level` records whether the bar touched any "
          "zone.")
def bearish_engulfing(tech: Tech, i: int, s) -> list[Firing]:
    return _engulfing(tech, i, s, False)


# ----------------------------------------------------------------------------------------------
# Context signals
# ----------------------------------------------------------------------------------------------


def _approaching(tech: Tech, i: int, s, side: str) -> list[Firing]:
    b, k, sig = tech.bars, s.levels.zone_atr_mult, s.signals
    lb = sig.approach_lookback_bars
    a = float(b.atr[i])
    if i - lb < 0 or np.isnan(a) or a <= 0:
        return []
    c = float(b.close[i])
    out = []
    for lv in tech.levels:
        if not _valid(lv, i, i - lb):
            continue
        lo, hi = lv.zone(i, b.atr, k)
        if side == "support" and c > hi:
            dist = (c - hi) / a
        elif side == "resistance" and c < lo:
            dist = (lo - c) / a
        else:
            continue
        if dist > sig.approach_atr:
            continue
        now_gap, then_gap = abs(c - lv.value(i)), abs(b.close[i - lb] - lv.value(i - lb))
        if now_gap < then_gap:
            ev = _lvl_ev(lv, i, b, k)
            ev.update({"dist_to_zone_atr": _r(dist, 3), "gap_now": _r(now_gap), f"gap_{lb}_bars_ago": _r(then_gap)})
            out.append((lv, ev))
    return out


@register("approaching_support", "context", "bullish", ("approach_atr", "approach_lookback_bars"),
          "Close is above a support zone, within `approach_atr` ATRs of zone_high, and nearer the level than "
          "`approach_lookback_bars` bars ago.")
def approaching_support(tech: Tech, i: int, s) -> list[Firing]:
    return _approaching(tech, i, s, "support")


@register("approaching_resistance", "context", "bearish", ("approach_atr", "approach_lookback_bars"),
          "Close is below a resistance zone, within `approach_atr` ATRs of zone_low, and nearer the level than "
          "`approach_lookback_bars` bars ago.")
def approaching_resistance(tech: Tech, i: int, s) -> list[Firing]:
    return _approaching(tech, i, s, "resistance")


def _range_ev(tech: Tech) -> dict:
    tr = tech.trend
    return {"range_high": _r(tr.range_high.value(tech.cutoff)), "range_low": _r(tr.range_low.value(tech.cutoff)),
            "range_width_atr": _r(tr.range_width_atr, 2), "position": _r(tr.range_position, 3)}


def _range_level(tech: Tech) -> Level | None:
    tr = tech.trend
    if tr.range_high is None or tr.range_low is None:
        return None
    return min((tr.range_high, tr.range_low), key=lambda lv: (lv.quality, lv.id))


@register("range_bound", "context", "neutral", ("cluster_atr_mult",),
          "Trend state is `range` (equal pivot highs and lows within tolerance, ≥ 2 touches per boundary, price "
          "inside).")
def range_bound(tech: Tech, i: int, s) -> list[Firing]:
    if tech.trend.state != "range":
        return []
    return [(_range_level(tech), _range_ev(tech))]


@register("range_mid", "context", "neutral", ("mid_band",),
          "Range-bound and the close sits inside the middle band of the range (default 0.35–0.65).")
def range_mid(tech: Tech, i: int, s) -> list[Firing]:
    if tech.trend.state != "range" or not tech.trend.range_mid:
        return []
    return [(_range_level(tech), _range_ev(tech))]


@register_near("range_mid")
def range_mid_near(tech: Tech, i: int, s) -> list[NearResult]:
    tr = tech.trend
    if tr.state != "range" or tr.range_mid:
        return []
    lo, hi = tr.range_low.value(tech.cutoff), tr.range_high.value(tech.cutoff)
    band = s.range.mid_band
    edge_pos = band[0] if tr.range_position < band[0] else band[1]
    edge = lo + edge_pos * (hi - lo)
    a = float(tech.bars.atr[i])
    dist = abs(float(tech.bars.close[i]) - edge) / a
    return [NearResult("range_mid", _range_level(tech),
                       f"range-bound but near the {'low' if edge_pos == band[0] else 'high'} edge", dist,
                       [AlertPrice("Mid-band edge", edge, f"price back inside the middle band at ${edge:.2f}"),
                        AlertPrice("Range low", lo, "range low boundary"),
                        AlertPrice("Range high", hi, "range high boundary")])]


@register("extended_up", "context", "neutral", ("rsi_overbought",), "RSI(14) ≥ `rsi_overbought` (default 70).")
def extended_up(tech: Tech, i: int, s) -> list[Firing]:
    r = tech.bars.rsi[i]
    th = s.indicators.rsi_overbought
    if np.isnan(r) or r < th:
        return []
    return [(None, {"rsi": _r(r, 2), "threshold": th, "distance": _r(r - th, 2)})]


@register("extended_down", "context", "neutral", ("rsi_oversold",), "RSI(14) ≤ `rsi_oversold` (default 30).")
def extended_down(tech: Tech, i: int, s) -> list[Firing]:
    r = tech.bars.rsi[i]
    th = s.indicators.rsi_oversold
    if np.isnan(r) or r > th:
        return []
    return [(None, {"rsi": _r(r, 2), "threshold": th, "distance": _r(th - r, 2)})]


def find_channel(tech: Tech, start: int, end: int, s) -> dict | None:
    """A horizontal channel over bars start..end: a flat support and a flat resistance from `tech` (structure known
    before the window) with every close between the support's zone low and the resistance's zone high, each zone
    touched at least `channel_min_touches` times, and the two zones apart. The narrowest such pair, else None."""
    b, k = tech.bars, s.levels.zone_atr_mult
    if start < 1 or end < start:
        return None
    w = slice(start, end + 1)
    atr = b.atr[w]
    if np.isnan(atr).any():
        return None
    tr = tech.trend
    flat = [lv for lv in [*tech.levels, tr.range_high, tr.range_low]
            if lv is not None and lv.type in FLAT_LEVEL_TYPES and _valid(lv, start, end)]
    need = s.range.channel_min_touches
    sups, ress = [], []
    for lv in flat:
        v = lv.value(end)
        if np.all(b.close[w] >= v - k * atr) and (n := int(np.sum(b.low[w] <= v + k * atr))) >= need:
            sups.append((lv, n))
        if np.all(b.close[w] <= v + k * atr) and (n := int(np.sum(b.high[w] >= v - k * atr))) >= need:
            ress.append((lv, n))
    a_end = float(b.atr[end])
    pairs = [(r.value(end) - sp.value(end), sp.id, r.id, sp, ns, r, nr) for sp, ns in sups for r, nr in ress
             if r.value(end) - k * a_end > sp.value(end) + k * a_end]
    if not pairs:
        return None
    width, _, _, sup, ns, res, nr = min(pairs, key=lambda p: p[:3])
    return {"support": sup, "resistance": res, "support_touches": ns, "resistance_touches": nr,
            "width_atr": _r(width / a_end, 2), "bars": end - start + 1, "from": b.dates[start], "to": b.dates[end]}


# ----------------------------------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------------------------------


def evaluate_signals(techs: list[Tech], last_idx: int, s) -> list[SignalResult]:
    """Evaluate every registered signal at ages 0..len(techs)-1 (techs[age] built for bar last_idx − age).

    On a provisional bar (i > tech.cutoff) a signal that would fire is reported as PENDING_CLOSE; one that
    would not fire is simply absent (the explain output notes the provisional bar). A `held` signal is kept only
    while every completed close since its test stayed on the level's side, and only its latest test per level.
    """
    out: list[SignalResult] = []
    for age, tech in enumerate(techs):
        i = last_idx - age
        if i < 1:
            break
        status = Tri.PENDING if i > tech.cutoff else Tri.TRUE
        for name in SIGNAL_NAMES:
            d = REGISTRY[name]
            for lv, ev in d.fn(tech, i, s):
                out.append(SignalResult(name, status, age, i, tech.bars.dates[i], d.kind, d.direction, lv, ev))
    if not techs:
        return out
    end, k = techs[0].cutoff, s.levels.zone_atr_mult
    seen: set[tuple] = set()
    kept = []
    for x in out:  # ages ascending, so the first test of a level seen is its latest
        if x.kind == "held":
            # by value, not id: ids are numbered per structure, so they can differ between bars for the same level
            key = (x.name, x.level.type, round(x.level.value(end), 4))
            if key in seen or not still_held(x, techs[0].bars, end, k):
                continue
            seen.add(key)
        kept.append(x)
    return kept


def near_signals(tech: Tech, i: int, s) -> dict[str, list[NearResult]]:
    out: dict[str, list[NearResult]] = {}
    for name in SIGNAL_NAMES:
        d = REGISTRY[name]
        if d.near is not None:
            res = d.near(tech, i, s)
            if res:
                out[name] = res
    return out
