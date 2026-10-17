"""Scoring (§6.8), ranking, watch list (§6.7) and alert helper.

Each score component is normalised to 0–1 and multiplied by its weight (weights sum to 100). A score belongs to
one (symbol, strategy) pair: the signal is scored against the best signal that strategy allows, trend_fit
measures the strength of the trend state it asked for, room and RSI headroom follow its bias, and the strategy's
own `scoring.weights` override the global ones. Defaults for the normalisations live in settings.scoring; the
component table is stored on every candidate so the ranking can be challenged.
"""

from __future__ import annotations

import numpy as np

from scanner.levels import confluence_types, project_next
from scanner.models import (
    AlertPrice,
    Bars,
    Candidate,
    Level,
    ScoreComponent,
    SignalResult,
    SymbolAnalysis,
    TrendResult,
    Tri,
    WatchItem,
)
from scanner.signals import REGISTRY
from scanner.strategies import StrategyEval, watch_nears

KIND_ORDER = {"break": 0, "hold": 1, "pattern": 2, "context": 3}
UP_BIASES = ("bullish", "non_bearish")
DOWN_BIASES = ("bearish", "non_bullish")


def _clamp(x: float) -> float:
    return float(min(max(x, 0.0), 1.0)) if not np.isnan(x) else 0.0


def _signal_fit(sig: SignalResult | None, strat, sc) -> tuple[float, str]:
    """Signal kind score relative to the best kind this strategy's own signal rules can produce, so a strategy that
    only ever fires holds is not ranked below a break strategy for firing its ideal signal."""
    if sig is None:
        return 0.0, "no signal rule"
    kinds = {REGISTRY[n].kind for n in strat.technical.signals_all + strat.technical.signals_any}
    best = max((sc.signal_kind_scores.get(kd, 0.0) for kd in kinds), default=0.0)
    got = sc.signal_kind_scores.get(sig.kind, 0.0)
    raw = got / best if best > 0 else 0.0
    return _clamp(raw), f"{sig.kind} ({sig.name}); best this strategy allows: {_best_kind(kinds, sc)}"


def _best_kind(kinds: set[str], sc) -> str:
    return max(sorted(kinds), key=lambda kd: sc.signal_kind_scores.get(kd, 0.0), default="—")


def _trend_fit(wanted: list[str], trend: TrendResult, b: Bars, t: int, s) -> tuple[float, str]:
    """How strong the trend state the strategy asked for is: pivot step + SMA stack for up/downtrends, boundary
    touches + width for ranges. `trend_fit_any` when the strategy accepts any state (or the state is mixed)."""
    sc = s.scoring
    state = trend.state
    if wanted == ["any"] or state not in ("uptrend", "downtrend", "range"):
        return sc.trend_fit_any, f"{state} vs {wanted}; no strength measure"
    a = float(b.atr[t])
    if state == "range":
        hi, lo = trend.range_high, trend.range_low
        touches = (hi.touches if hi else 0) + (lo.touches if lo else 0)
        w0, w1 = sc.range_width_atr
        width = trend.range_width_atr or 0.0
        t_part = _clamp(touches / sc.range_touch_cap)
        w_part = _clamp((width - w0) / (w1 - w0))
        return (t_part + w_part) / 2, (f"range: {touches} boundary touches ({t_part:.2f}), "
                                       f"width {width:.1f} ATR ({w_part:.2f})")
    up = state == "uptrend"
    sgn = 1.0 if up else -1.0
    h1, h2, l1, l2 = trend.pivots_used[:4]
    step = min(sgn * (h2.price - h1.price), sgn * (l2.price - l1.price)) / a if a > 0 else 0.0
    p_part = _clamp(step / sc.trend_step_atr)
    j = t - sc.trend_sma_slope_bars
    m50_then = float(b.sma50[j]) if j >= 0 else float("nan")
    pairs = ((float(b.close[t]), float(b.sma50[t])), (float(b.sma50[t]), float(b.sma200[t])),
             (float(b.sma50[t]), m50_then))
    checks = [sgn * (x - y) > 0 for x, y in pairs if not (np.isnan(x) or np.isnan(y))]  # n/a with short history
    m_part = sum(checks) / len(checks) if checks else 0.5
    word = "above" if up else "below"
    return (p_part + m_part) / 2, (f"{state}: pivot step {step:.2f} ATR ({p_part:.2f}); SMA stack "
                                   f"{sum(checks)}/{len(checks)} (close {word} SMA50 {word} SMA200, SMA50 "
                                   f"{'rising' if up else 'falling'}) ({m_part:.2f})")


def _room(bias: str, trend: TrendResult, supports: list[dict], resistances: list[dict], k: float,
          room_atr: float, own: Level | None = None) -> tuple[float, str]:
    """Space to the next opposing level in the strategy's direction, in ATRs beyond its zone. Neutral strategies
    want price centred: range position near the middle, or equal distance to support and resistance. The signal's
    own level is skipped: price inside its zone can sit on either side of the line."""
    if own is not None:
        supports = [r for r in supports if r["level"].id != own.id]
        resistances = [r for r in resistances if r["level"].id != own.id]

    def gap(rows: list[dict]) -> float | None:
        return max(abs(rows[0]["dist_atr"]) - k, 0.0) if rows and not np.isnan(rows[0]["dist_atr"]) else None

    up, down = gap(resistances), gap(supports)
    if bias in UP_BIASES or bias in DOWN_BIASES:
        d, rows, side = (up, resistances, "resistance") if bias in UP_BIASES else (down, supports, "support")
        if d is None:
            return 1.0, f"no {side} in the way"
        return _clamp(d / room_atr), f"{d:.2f} ATR to {rows[0]['level'].label} {side} zone"
    if trend.state == "range" and trend.range_position is not None:
        pos = trend.range_position
        return _clamp(1 - abs(pos - 0.5) * 2), f"range position {pos:.2f} (0.5 = centred)"
    if up is None or down is None:
        return 0.5, "only one side has a level"
    hi = max(up, down)
    raw = min(up, down) / hi if hi > 0 else 1.0
    return raw, f"{down:.2f} ATR to support, {up:.2f} ATR to resistance"


def _components(sig: SignalResult | None, ev: StrategyEval, sa: SymbolAnalysis, s) -> tuple[list[ScoreComponent], bool]:
    sc, w = s.scoring, s.scoring.weights_for(ev.strategy)
    b, t = sa.bars, sa.last_idx
    k = s.levels.zone_atr_mult
    comps: list[tuple[str, float, str]] = []
    stretched = False
    lv: Level | None = sig.level if sig else None

    comps.append(("signal", *_signal_fit(sig, ev.strategy, sc)))
    if lv is not None:
        rr = _role_reversal(lv, sa)
        lq = lv.quality / sc.level_quality_cap + (sc.role_reversal_bonus if rr else 0.0)
        comps.append(("level_quality", _clamp(lq),
                      f"{lv.label} quality {lv.quality:.2f} (touches {lv.touches})"
                      + (" + role-reversal bonus" if rr else "")))
        tech = sa.techs[min(sig.age, len(sa.techs) - 1)]
        types = confluence_types(lv, tech.levels, sig.bar_idx, b, k)
        comps.append(("confluence", _clamp((len(types) - 1) / 2), ", ".join(types)))
    else:
        comps.append(("level_quality", 0.0, "no level"))
        comps.append(("confluence", 0.0, "no level"))
    bias = ev.strategy.bias
    comps.append(("trend_fit", *_trend_fit(ev.strategy.technical.trend, sa.trend, b, t, s)))
    comps.append(("flow", sc.flow_scores.get(ev.flow_label, 0.5), ev.flow_label))
    comps.append(("room", *_room(bias, sa.trend, sa.nearest_support, sa.nearest_resistance, k, sc.room_atr, lv)))

    rsi = float(b.rsi[t])
    if bias in UP_BIASES:
        comps.append(("rsi_headroom", _clamp((s.indicators.rsi_overbought - rsi) / sc.rsi_headroom_scale),
                      f"RSI {rsi:.1f} vs {s.indicators.rsi_overbought:g}"))
    elif bias in DOWN_BIASES:
        comps.append(("rsi_headroom", _clamp((rsi - s.indicators.rsi_oversold) / sc.rsi_headroom_scale),
                      f"RSI {rsi:.1f} vs {s.indicators.rsi_oversold:g}"))
    else:
        comps.append(("rsi_headroom", _clamp(1 - abs(rsi - 50) / sc.rsi_neutral_scale),
                      f"RSI {rsi:.1f} vs 50 (neutral: closer is better)"))

    a = float(b.atr[t])
    c = float(b.close[t])
    if sig is None:
        comps.append(("entry_quality", 0.5, "no signal"))
    elif sig.kind == "hold" and lv is not None:
        d = abs(c - lv.value(t)) / a
        comps.append(("entry_quality", _clamp(1 - d / s.signals.approach_atr), f"{d:.2f} ATR from level"))
    elif sig.kind == "break" and lv is not None:
        lo, hi = lv.zone(t, b.atr, k)
        beyond = (lo - c) / a if sig.name == "support_break" else (c - hi) / a
        stretched = beyond > s.signals.chase_atr
        comps.append(("entry_quality", _clamp(1 - max(beyond, 0.0) / s.signals.chase_atr),
                      f"{beyond:.2f} ATR beyond zone" + (" — STRETCHED" if stretched else "")))
    elif sig.kind == "pattern":
        at = bool(sig.evidence.get("at_level"))
        comps.append(("entry_quality", 1.0 if at else 0.5, "at a level" if at else "not at a level"))
    else:
        comps.append(("entry_quality", 0.5, "context signal"))

    ages = sc.age_scores
    age_raw = ages[min(sig.age, len(ages) - 1)] if sig else ages[0]
    comps.append(("age", age_raw, f"age {sig.age if sig else 0}"))

    out = [ScoreComponent(n, raw, float(w.get(n, 0.0)), raw * float(w.get(n, 0.0)), note) for n, raw, note in comps]
    return out, stretched


def _role_reversal(lv: Level, sa: SymbolAnalysis) -> bool:
    if lv.origin is None:
        return False
    return lv.role(sa.last_idx, float(sa.bars.close[sa.last_idx])) != lv.origin


def score_candidate(ev: StrategyEval, sa: SymbolAnalysis, s) -> Candidate:
    scored: list[tuple[float, SignalResult | None, list[ScoreComponent], bool]] = []
    for sig in ev.qualifying or [None]:
        comps, stretched = _components(sig, ev, sa, s)
        scored.append((sum(c.points for c in comps), sig, comps, stretched))

    def _key(o):
        sig = o[1]
        return (-round(o[0], 9), KIND_ORDER.get(sig.kind, 9) if sig else 9, sig.age if sig else 0,
                -(sig.level.quality if sig and sig.level else 0.0), sig.name if sig else "",
                sig.level.id if sig and sig.level else "")

    total, primary, comps, stretched = sorted(scored, key=_key)[0]
    strat = ev.strategy
    cand = Candidate(
        symbol=sa.symbol, strategy_id=strat.id, strategy_name=strat.name, bias=strat.bias, status=ev.status,
        rules=ev.rules, flow_label=ev.flow_label, primary=primary, signals=ev.qualifying,
        score=round(total, 2), components=comps, stretched=stretched,
        level_quality=primary.level.quality if primary and primary.level else 0.0,
    )
    cand.alerts = build_alerts(primary, sa, s)
    return cand


def rank(cands: list[Candidate]) -> list[Candidate]:
    """SC-3: confirmed before pending; score desc; ties by level quality desc, then symbol, then strategy."""
    def key(c: Candidate) -> tuple:
        return (c.pending, -c.score, -round(c.level_quality, 9), c.symbol, c.strategy_id)

    ordered = sorted(cands, key=key)
    for n, c in enumerate(ordered, start=1):
        c.rank = n
    by_strat: dict[str, int] = {}
    for c in ordered:
        by_strat[c.strategy_id] = by_strat.get(c.strategy_id, 0) + 1
        c.strategy_rank = by_strat[c.strategy_id]
    return ordered


# ----------------------------------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------------------------------


def _zone_next(lv: Level, sa: SymbolAnalysis, s) -> tuple[float, float, float]:
    t = sa.last_idx
    nxt = project_next(lv, t)
    a = float(sa.bars.atr[t])
    k = s.levels.zone_atr_mult
    return nxt, nxt - k * a, nxt + k * a


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


# ----------------------------------------------------------------------------------------------
# Watch list
# ----------------------------------------------------------------------------------------------


def build_watchlist(evals: dict[str, list[StrategyEval]], analyses: dict[str, SymbolAnalysis], s) -> list[WatchItem]:
    items: dict[tuple, WatchItem] = {}
    for sym in sorted(evals):
        sa = analyses[sym]
        matched = {ev.strategy.id for ev in evals[sym] if ev.status is not Tri.FALSE}
        for ev in evals[sym]:
            if ev.strategy.id in matched:
                continue
            for nr in watch_nears(ev, sa.near):
                key = (sym, nr.signal, nr.level.id if nr.level is not None else "")
                if key not in items:
                    q = nr.level.quality if nr.level is not None else 0.0
                    items[key] = WatchItem(sym, [], nr, q)
                st = (ev.strategy.id, ev.strategy.name)
                if st not in items[key].strategies:
                    items[key].strategies.append(st)
    ordered = sorted(items.values(), key=lambda w: w.sort_key)
    per_sym: dict[str, int] = {}
    out = []
    for w in ordered:
        if per_sym.get(w.symbol, 0) >= s.report.max_watch_items_per_symbol:
            continue
        per_sym[w.symbol] = per_sym.get(w.symbol, 0) + 1
        out.append(w)
        if len(out) >= s.report.max_watch_items:
            break
    return out
