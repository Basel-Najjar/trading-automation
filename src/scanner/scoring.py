"""Scoring (§6.8), ranking, watch list (§6.7), alert helper and plan placeholders (§7.4).

Each score component is normalised to 0–1 and multiplied by its weight (weights sum to 100). Defaults
for the normalisations live in settings.scoring (decision 7); the component table is stored on every
candidate so the ranking can be challenged.
"""

from __future__ import annotations

import re

import numpy as np

from scanner.levels import confluence_types, project_next
from scanner.models import (
    AlertPrice,
    Candidate,
    Level,
    ScoreComponent,
    SignalResult,
    SymbolAnalysis,
    Tri,
    WatchItem,
)
from scanner.strategies import StrategyEval, watch_nears

KIND_ORDER = {"break": 0, "hold": 1, "pattern": 2, "context": 3}
UP_BIASES = ("bullish", "non_bearish")
DOWN_BIASES = ("bearish", "non_bullish")


def _clamp(x: float) -> float:
    return float(min(max(x, 0.0), 1.0)) if not np.isnan(x) else 0.0


def _components(sig: SignalResult | None, ev: StrategyEval, sa: SymbolAnalysis, s) -> tuple[list[ScoreComponent], bool]:
    sc, w = s.scoring, s.scoring.weights
    b, t = sa.bars, sa.last_idx
    k = s.levels.zone_atr_mult
    comps: list[tuple[str, float, str]] = []
    stretched = False
    lv: Level | None = sig.level if sig else None

    comps.append(("signal", sc.signal_kind_scores.get(sig.kind, 0.0) if sig else 0.0,
                  f"{sig.kind} ({sig.name})" if sig else "no signal rule"))
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
    trend = ev.strategy.technical.trend
    comps.append(("trend_fit", sc.trend_fit_any if trend == ["any"] else 1.0,
                  f"{sa.trend.state} vs {trend}"))
    comps.append(("flow", sc.flow_scores.get(ev.flow_label, 0.5), ev.flow_label))

    rsi = float(b.rsi[t])
    bias = ev.strategy.bias
    if bias in UP_BIASES:
        comps.append(("rsi_headroom", _clamp((s.indicators.rsi_overbought - rsi) / sc.rsi_headroom_scale),
                      f"RSI {rsi:.1f} vs {s.indicators.rsi_overbought:g}"))
    elif bias in DOWN_BIASES:
        comps.append(("rsi_headroom", _clamp((rsi - s.indicators.rsi_oversold) / sc.rsi_headroom_scale),
                      f"RSI {rsi:.1f} vs {s.indicators.rsi_oversold:g}"))
    else:
        comps.append(("rsi_headroom", 0.5, f"RSI {rsi:.1f}; non-directional"))

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
# Alerts and placeholders
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


_PLACEHOLDER = re.compile(r"\{(nearest_support|nearest_resistance)\}")


def fill_placeholders(text: str, sa: SymbolAnalysis, s) -> str:
    def _sub(m: re.Match) -> str:
        rows = sa.nearest_support if m.group(1) == "nearest_support" else sa.nearest_resistance
        if not rows:
            return "n/a (no level found)"
        lv = rows[0]["level"]
        _, zlo, zhi = _zone_next(lv, sa, s)
        if m.group(1) == "nearest_support":
            return f"${zlo:.2f} ({lv.label} zone low; level ${lv.value(sa.last_idx):.2f})"
        return f"${zhi:.2f} ({lv.label} zone high; level ${lv.value(sa.last_idx):.2f})"

    return _PLACEHOLDER.sub(_sub, text)


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
