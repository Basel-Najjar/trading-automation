"""Signal vocabulary (§6.4). Each signal is a small function registered in REGISTRY (NF-7).

Signal function contract: fn(tech, i, s) -> list[(level | None, evidence dict)], one entry per firing
(level-based signals can fire on several levels). `i` is the evaluation bar; `tech` holds structure built
from bars ≤ tech.cutoff. If i > tech.cutoff the bar is provisional and results become PENDING_CLOSE.

Near function contract (watch list, WL-1): near(tech, i, s) -> list[NearResult] for a completed bar i.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from scanner.levels import project_next
from scanner.models import SIGNAL_NAMES, AlertPrice, Bars, Level, NearResult, SignalResult, Tech, Tri

Firing = tuple[Level | None, dict]


@dataclass(frozen=True)
class SignalDef:
    name: str
    kind: str  # break | hold | pattern | context
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
    return all(0 <= t < len(lv.values) and not np.isnan(lv.values[t]) for t in ts)


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
    out = []
    for lv in tech.levels:
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
        nxt = project_next(lv, i)
        zlo, zhi = nxt - k * a, nxt + k * a
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
# Break signals (engulfing + full-bodied confirmation, OQ-1)
# ----------------------------------------------------------------------------------------------


def _break_conditions(tech: Tech, lv: Level, i: int, s, side: str) -> dict | None:
    b, k, sig = tech.bars, s.levels.zone_atr_mult, s.signals
    lb = sig.break_prior_side_lookback
    if i < 1 or not _valid(lv, i, i - 1) or np.isnan(b.atr[i]) or np.isnan(b.atr[i - 1]):
        return None
    lo, hi = lv.zone(i, b.atr, k)
    plo, phi = lv.zone(i - 1, b.atr, k)
    prior = range(max(i - lb, 1), i)
    if side == "support":
        c1 = b.close[i] < lo
        c2 = is_full_body(b, i, sig.full_body_ratio) and is_bear(b, i)
        c4 = b.close[i - 1] <= phi
        c5 = any(_valid(lv, t) and b.close[t] > lv.zone(t, b.atr, k)[1] for t in prior)
        beyond = (lo - b.close[i]) / b.atr[i]
    else:
        c1 = b.close[i] > hi
        c2 = is_full_body(b, i, sig.full_body_ratio) and is_bull(b, i)
        c4 = b.close[i - 1] >= plo
        c5 = any(_valid(lv, t) and b.close[t] < lv.zone(t, b.atr, k)[0] for t in prior)
        beyond = (b.close[i] - hi) / b.atr[i]
    c3 = engulfs(b, i, _eng_tol(b, i, s), sig.break_engulfing_require_opposite_color)
    return {"closes_beyond_zone": bool(c1), "full_bodied": bool(c2), "engulfs_prior": bool(c3),
            "prior_close_in_play": bool(c4), "acted_as_level": bool(c5), "beyond_atr": float(beyond),
            "in_zone": bool(lo <= b.close[i] <= hi)}


def _break(tech: Tech, i: int, s, side: str) -> list[Firing]:
    b, k = tech.bars, s.levels.zone_atr_mult
    out = []
    for lv in tech.levels:
        c = _break_conditions(tech, lv, i, s, side)
        if c is None or not all(c[x] for x in ("closes_beyond_zone", "full_bodied", "engulfs_prior",
                                                 "prior_close_in_play", "acted_as_level")):
            continue
        ev = _lvl_ev(lv, i, b, k)
        ev.update({"break_bar": b.ohlc(i), "prior_bar": b.ohlc(i - 1), "body_ratio": _r(body_ratio(b, i), 3),
                   "beyond_zone_atr": _r(c["beyond_atr"], 3),
                   "stretched": c["beyond_atr"] > s.signals.chase_atr})
        out.append((lv, ev))
    return out


@register("support_break", "break", "bearish",
          ("full_body_ratio", "break_engulfing_require_opposite_color", "engulfing_tolerance_atr",
           "break_prior_side_lookback", "chase_atr"),
          "Bar N closes below zone_low; is full-bodied bearish; its body engulfs bar N-1's body; bar N-1 closed "
          "≤ zone_high (level in play); and some close in the prior `break_prior_side_lookback` bars was above "
          "zone_high (level acted as support).")
def support_break(tech: Tech, i: int, s) -> list[Firing]:
    return _break(tech, i, s, "support")


@register("resistance_break", "break", "bullish",
          ("full_body_ratio", "break_engulfing_require_opposite_color", "engulfing_tolerance_atr",
           "break_prior_side_lookback", "chase_atr"),
          "Mirror of support_break: close above zone_high, full-bodied bullish, engulfs N-1, N-1 closed ≥ zone_low, "
          "and a prior close below zone_low within the lookback.")
def resistance_break(tech: Tech, i: int, s) -> list[Firing]:
    return _break(tech, i, s, "resistance")


def _break_near(tech: Tech, i: int, s, side: str) -> list[NearResult]:
    b, k = tech.bars, s.levels.zone_atr_mult
    a = float(b.atr[i])
    out = []
    if np.isnan(a) or a <= 0:
        return out
    fired = {lv.id for lv, _ in _break(tech, i, s, side)}
    for lv in tech.levels:
        if lv.id in fired:
            continue
        c = _break_conditions(tech, lv, i, s, side)
        if c is None or not c["acted_as_level"]:
            continue
        nxt = project_next(lv, i)
        zlo, zhi = nxt - k * a, nxt + k * a
        note = " — the bar must also be full-bodied and engulf the prior body"
        if c["in_zone"]:
            dist = (b.close[i] - zlo) / a if side == "support" else (zhi - b.close[i]) / a
            desc = f"price in {lv.label} zone; no break yet"
        elif c["closes_beyond_zone"] and c["prior_close_in_play"]:
            dist = 0.0
            failed = [n for n in ("full_bodied", "engulfs_prior") if not c[n]]
            desc = f"closed through {lv.label} zone but not confirmed ({', '.join(failed)} failed)"
        else:
            continue
        if side == "support":
            trig = AlertPrice("Break trigger", zlo, f"close below ${zlo:.2f} ({lv.label} zone low){note}")
        else:
            trig = AlertPrice("Break trigger", zhi, f"close above ${zhi:.2f} ({lv.label} zone high){note}")
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
          "`pattern_engulfing_require_opposite_color`). `at_level` (bar touched any zone) is a score bonus.")
def bullish_engulfing(tech: Tech, i: int, s) -> list[Firing]:
    return _engulfing(tech, i, s, True)


@register("bearish_engulfing", "pattern", "bearish",
          ("pattern_engulfing_require_opposite_color", "engulfing_tolerance_atr"),
          "Bar N is bearish and its body engulfs bar N-1's body. `at_level` is a score bonus.")
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


# ----------------------------------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------------------------------


def evaluate_signals(techs: list[Tech], last_idx: int, s) -> list[SignalResult]:
    """Evaluate every registered signal at ages 0..len(techs)-1 (techs[age] built for bar last_idx − age).

    On a provisional bar (i > tech.cutoff) a signal that would fire is reported as PENDING_CLOSE; one that
    would not fire is simply absent (the explain output notes the provisional bar).
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
    return out


def near_signals(tech: Tech, i: int, s) -> dict[str, list[NearResult]]:
    out: dict[str, list[NearResult]] = {}
    for name in SIGNAL_NAMES:
        d = REGISTRY[name]
        if d.near is not None:
            res = d.near(tech, i, s)
            if res:
                out[name] = res
    return out
