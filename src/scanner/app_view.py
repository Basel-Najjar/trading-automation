"""View model for the Streamlit app: market overview, tickers split into Bullish / Bearish / Neutral candidates by
their matched setups, the setup pane (rule one-liners, price ladder, chart) and ticker descriptions.

Pure functions over a `RunResult` (no Streamlit), so they are unit-tested like the rest of the scanner.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import numpy as np

from scanner.config import Config, Strategy
from scanner.data import Profile
from scanner.labels import LABELS, STATUS_ICON, TREND_ICON, change_pct, humanize, trend_text
from scanner.models import (
    DOWN_BIASES,
    UP_BIASES,
    Candidate,
    RuleResult,
    SignalResult,
    SymbolAnalysis,
    Tri,
    unique_by_id,
)
from scanner.pipeline import RunResult
from scanner.plotly_chart import Overlay, chart_levels, price_figure
from scanner.strategies import StrategyEval, watch_nears
from scanner.views import level_dict, market_context

if TYPE_CHECKING:
    import plotly.graph_objects as go

__all__ = ["LABELS", "STATUS_ICON", "TREND_ICON", "change_pct", "humanize", "trend_text"]  # re-exported from labels

BIAS_GROUPS = {"Bullish": UP_BIASES, "Bearish": DOWN_BIASES, "Neutral": ("neutral",)}


# ----------------------------------------------------------------------------------------------
# Ranking
# ----------------------------------------------------------------------------------------------


@dataclass
class StrategyRow:
    ev: StrategyEval
    met: int
    total: int
    pending: int
    cand: Candidate | None

    @property
    def fraction(self) -> float:
        return self.met / self.total if self.total else 0.0

    @property
    def score(self) -> float:
        return self.cand.score if self.cand else 0.0


@dataclass
class SymbolRow:
    symbol: str
    rows: list[StrategyRow]
    close: float
    change_pct: float
    trend: str
    sector: str | None = None

    @property
    def best(self) -> StrategyRow:
        return self.rows[0]

    @property
    def matches(self) -> list[StrategyRow]:
        """Strategies with a candidate (a full or pending-close match), best first."""
        return [r for r in self.rows if r.cand]


def _row_key(r: StrategyRow) -> tuple:
    return (-r.fraction, -r.score, r.ev.strategy.name)


def _candidates_by_symbol(res: RunResult) -> dict[str, dict[str, Candidate]]:
    out: dict[str, dict[str, Candidate]] = {}
    for c in res.candidates:
        out.setdefault(c.symbol, {})[c.strategy_id] = c
    return out


def strategy_rows(res: RunResult, symbol: str, _cands: dict[str, Candidate] | None = None) -> list[StrategyRow]:
    """Every evaluated strategy for `symbol`, best % of rules met first. PENDING_CLOSE rules do not count as met."""
    cands = _candidates_by_symbol(res).get(symbol, {}) if _cands is None else _cands
    rows = [StrategyRow(ev, sum(r.status is Tri.TRUE for r in ev.rules), len(ev.rules),
                        sum(r.status is Tri.PENDING for r in ev.rules), cands.get(ev.strategy.id))
            for ev in res.evals.get(symbol, [])]
    return sorted(rows, key=_row_key)


def symbol_rankings(res: RunResult) -> list[SymbolRow]:
    """One row per analysed symbol with strategies; best fraction desc, then best candidate score, then symbol."""
    out = []
    by_sym = _candidates_by_symbol(res)
    for sym, sa in res.analyses.items():
        rows = strategy_rows(res, sym, by_sym.get(sym, {}))
        if not rows:
            continue
        out.append(SymbolRow(sym, rows, float(sa.bars.close[sa.last_idx]), change_pct(sa), sa.trend.state,
                             sa.sector_etf))
    best_score = {r.symbol: max(x.score for x in r.rows) for r in out}
    return sorted(out, key=lambda r: (-r.best.fraction, -best_score[r.symbol], r.symbol))


def _strategy_ok(r: StrategyRow, sids: set[str] | None) -> bool:
    return sids is None or r.ev.strategy.id in sids


# ----------------------------------------------------------------------------------------------
# Direction: Bullish / Bearish / Neutral candidates
# ----------------------------------------------------------------------------------------------


def bias_group(bias: str) -> str:
    """The direction group ("Bullish", "Bearish" or "Neutral") of one strategy bias."""
    return next((g for g, biases in BIAS_GROUPS.items() if bias in biases), "Neutral")


def setup_summary(row: SymbolRow) -> dict[str, list[StrategyRow]]:
    """The symbol's matched strategies by direction group (every group present, in BIAS_GROUPS order), best score
    first."""
    out: dict[str, list[StrategyRow]] = {g: [] for g in BIAS_GROUPS}
    for r in sorted(row.matches, key=lambda r: (-r.score, r.ev.strategy.name)):
        out[bias_group(r.ev.strategy.bias)].append(r)
    return out


def direction(row: SymbolRow) -> str | None:
    """A ticker's direction from its matched setups: None without a match; Neutral when it has both bullish and
    bearish matches (two-sided, e.g. bull put + bear call in a range) or only neutral ones; otherwise the one side
    it matched (a neutral match beside it does not change that)."""
    groups = {g for g, rows in setup_summary(row).items() if rows}
    if not groups:
        return None
    bull, bear = "Bullish" in groups, "Bearish" in groups
    if bull == bear:
        return "Neutral"
    return "Bullish" if bull else "Bearish"


def direction_setups(row: SymbolRow, group: str | None = None) -> list[StrategyRow]:
    """The matches that back the ticker's direction (or `group`'s), best score first: those on its side, or every
    match for a Neutral ticker."""
    group = group or direction(row)
    if group is None:
        return []
    out = [r for g, rows in setup_summary(row).items() if group == "Neutral" or g == group for r in rows]
    return sorted(out, key=lambda r: (-r.score, r.ev.strategy.name))


def direction_groups(res: RunResult, symbols: list[str] | None = None, strategy_ids: list[str] | None = None,
                     sectors: list[str] | None = None,
                     rankings: list[SymbolRow] | None = None) -> dict[str, list[SymbolRow]]:
    """Tickers with at least one matched setup, split by `direction` ("Bullish", "Bearish", "Neutral", every key
    present). Each row keeps its strategies within the filters (empty or None = all), and its direction is taken
    from those. Order: best score among the setups in the ticker's direction desc, then symbol. `rankings`: an
    already computed `symbol_rankings(res)`."""
    syms, secs = set(symbols or []) or None, set(sectors or []) or None
    sids = set(strategy_ids or []) or None
    out: dict[str, list[SymbolRow]] = {g: [] for g in BIAS_GROUPS}
    for s in symbol_rankings(res) if rankings is None else rankings:
        if (syms is not None and s.symbol not in syms) or (secs is not None and s.sector not in secs):
            continue
        rows = [r for r in s.rows if _strategy_ok(r, sids)]
        if not rows:
            continue
        kept = replace(s, rows=rows)
        d = direction(kept)
        if d is not None:
            out[d].append(kept)
    for g in out:
        out[g].sort(key=lambda s, g=g: (-direction_setups(s, g)[0].score, s.symbol))
    return out


def pane_rows(row: SymbolRow) -> list[StrategyRow]:
    """Every evaluated strategy for the setup pane: matches in the ticker's direction first (best score first), then
    its other matches, then the rest by share of rules met."""
    d = direction(row)

    def key(r: StrategyRow) -> tuple:
        if r.cand is None:
            return (2, 0.0, -r.fraction, r.ev.strategy.name)
        own = d is not None and (d == "Neutral" or bias_group(r.ev.strategy.bias) == d)
        return (0 if own else 1, -r.score, 0.0, r.ev.strategy.name)

    return sorted(row.rows, key=key)


# ----------------------------------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------------------------------


def overview(res: RunResult, cfg: Config, rankings: list[SymbolRow] | None = None) -> dict:
    """Top-of-page summary: session, benchmark, counts (tickers per direction), and one entry per sector ETF with
    its setup count. `rankings`: an already computed `symbol_rankings(res)`."""
    market, sectors = market_context(res, cfg)
    with_cand = {c.symbol for c in res.candidates}
    for e in sectors:
        e["setups"] = sum(1 for m in e["members"] if m in with_cand)
    warnings = [d for d in res.diagnostics if d.severity != "info"]
    directions = {g: len(rows) for g, rows in direction_groups(res, rankings=rankings).items()}
    return {"as_of": res.as_of, "provisional": res.provisional, "notes": list(res.notes), "market": market,
            "sectors": sectors, "n_candidates": len(res.candidates), "n_symbols_with": len(with_cand),
            "n_symbols": len(res.analyses), "directions": directions, "watch": res.watchlist,
            "skipped": res.skipped, "warnings": warnings}


# ----------------------------------------------------------------------------------------------
# Setup pane
# ----------------------------------------------------------------------------------------------


@dataclass
class RuleLine:
    rule: str
    label: str
    status: Tri
    summary: str
    detail: str = ""  # the scanner's own one-line detail, for the raw evidence drill-down
    evidence: list[str] = field(default_factory=list)


def _signal_line(name: str, hits: list[SignalResult]) -> str:
    if not hits:
        return f"{humanize(name)}: not fired"
    with_lv = [h for h in hits if h.level is not None]
    if with_lv:
        ids = {h.level.id for h in with_lv}
        h = with_lv[0]
        where = f"at {len(ids)} levels, e.g." if len(ids) > 1 else "at"
        text = f"{humanize(name)} {where} {h.level.value(h.bar_idx):,.2f} ({h.level.label})"
    else:
        text = f"{humanize(name)} on {hits[0].date:%b %d}"
    age = min(h.age for h in hits)
    if age:
        text += f", {age} bar{'s' if age > 1 else ''} ago"
    if all(h.status is Tri.PENDING for h in hits):
        text += " · pending close"
    return text


def rule_lines(ev: StrategyEval, sa: SymbolAnalysis, settings) -> list[RuleLine]:
    """One readable line per evaluated rule (the raw evidence is kept for the drill-down)."""
    strat: Strategy = ev.strategy
    tech = strat.technical
    s = settings.with_overrides(tech.overrides)
    hits: dict[str, list[SignalResult]] = {}
    for h in unique_by_id(ev.qualifying):
        hits.setdefault(h.name, [])
        if h.status is not Tri.FALSE:
            hits[h.name].append(h)
    out = []
    for r in ev.rules:
        out.append(RuleLine(r.rule, humanize(r.rule), r.status, _rule_summary(r, ev, sa, s, hits), r.detail,
                            list(r.evidence)))
    return out


def _rule_summary(r: RuleResult, ev: StrategyEval, sa: SymbolAnalysis, s, hits: dict) -> str:
    tech = ev.strategy.technical
    if r.rule == "trend":
        state = trend_text(sa.trend.state)
        if tech.trend == ["any"]:
            return f"{state} (any trend allowed)"
        return f"{state} (allowed: {', '.join(humanize(t) for t in tech.trend)})"
    if r.rule in ("signals_all", "signals_any"):
        names = tech.signals_all if r.rule == "signals_all" else tech.signals_any
        fired = [n for n in names if hits.get(n)]
        if r.rule == "signals_any" and not fired:
            return "none fired (any of: " + ", ".join(humanize(n) for n in names) + ")"
        shown = names if r.rule == "signals_all" else fired
        return "; ".join(_signal_line(n, hits.get(n, [])) for n in shown)
    if r.rule == "not_extended":
        rsi = float(sa.bars.rsi[sa.last_idx])
        ok = r.status is Tri.TRUE
        if ev.strategy.not_extended_resolved == "up":
            th = s.indicators.rsi_overbought
            return f"RSI {rsi:.1f}, " + (f"below overbought {th:g}" if ok else f"overbought (≥ {th:g})")
        th = s.indicators.rsi_oversold
        return f"RSI {rsi:.1f}, " + (f"above oversold {th:g}" if ok else f"oversold (≤ {th:g})")
    if r.rule == "alignment":
        comps = " · ".join(f"{k} {humanize(v).lower()}" for k, v in sa.flow.components()) if sa.flow else "n/a"
        mode = {"soft": "scored only", "required": "required", "ignore": "ignored"}[tech.alignment]
        if tech.alignment == "ignore":
            return f"ignored ({comps})"
        return f"{humanize(ev.flow_label)}, {mode} ({comps})"
    return r.detail


def _alert_kind(label: str) -> str:
    low = label.lower()
    if "invalidation" in low:
        return "invalidation"
    return "level" if low.endswith("level") else "zone"


def price_ladder(alerts: list[tuple[str, float, str]], close: float) -> list[dict]:
    """The alert prices plus the last close, highest first, de-duplicated, each with its distance from the close."""
    rows, seen = [], set()
    for label, price, meaning in [("Last close", close, "latest bar")] + list(alerts):
        key = (label, round(price, 2))
        if key in seen or not math.isfinite(price):
            continue
        seen.add(key)
        kind = "close" if label == "Last close" else _alert_kind(label)
        rows.append({"label": label, "price": price, "meaning": meaning, "kind": kind,
                     "dist_pct": (price / close - 1) * 100 if close else 0.0})
    return sorted(rows, key=lambda r: -r["price"])


APP_CHART_BARS = 60  # the chart opens on fewer bars than `display.chart_bars`, so the levels near price stay legible


def signal_hits(ev: StrategyEval, cand: Candidate | None) -> list[dict]:
    """One row per signal that fired for this setup (the structured form of the rule evidence)."""
    out = []
    for h in unique_by_id([h for h in (cand.signals if cand else ev.qualifying) if h.status is not Tri.FALSE]):
        out.append({"Signal": humanize(h.name), "Level": h.level.label if h.level else "",
                    "Price": h.level.value(h.bar_idx) if h.level else None, "Date": h.date, "Bars ago": h.age,
                    "Pending": "pending close" if h.status is Tri.PENDING else ""})
    return out


OVERLAY_COLORS = {"etf": "#12a1a1", "benchmark": "#8d8d8d", "ratio": "#c0399b"}


def overlay_choices(res: RunResult, symbol: str) -> dict[str, str]:
    """Comparison lines available for `symbol` ({kind: label}): its sector ETF, the benchmark and the ETF/benchmark
    ratio (the series the sector flow is read from). A series is offered only when it was loaded and is not the
    symbol itself."""
    sa = res.analyses[symbol]
    etf, bench = sa.sector_etf, res.benchmark
    has_etf = bool(etf) and etf != symbol and res.series(etf) is not None
    has_bench = bench != symbol and res.series(bench) is not None
    out = {}
    if has_etf:
        out["etf"] = f"{etf} (sector)"
    if has_bench:
        out["benchmark"] = f"{bench} (benchmark)"
    if has_etf and has_bench and etf != bench:
        out["ratio"] = f"{etf}/{bench}"
    return out


def _aligned_close(res: RunResult, other: str, dates: list) -> np.ndarray:
    o = res.series(other)
    by_date = {d: float(c) for d, c in zip(o.bars.dates[:o.last_idx + 1], o.bars.close[:o.last_idx + 1], strict=True)}
    return np.array([by_date.get(d, np.nan) for d in dates], dtype=float)


def chart_overlays(res: RunResult, symbol: str, kinds: list[str] | tuple[str, ...]) -> list[Overlay]:
    """The overlays `kinds` asks for (in the order etf, benchmark, ratio), aligned to `symbol`'s bars."""
    opts = overlay_choices(res, symbol)
    sa = res.analyses[symbol]
    dates = sa.bars.dates
    etf = _aligned_close(res, sa.sector_etf, dates) if "etf" in opts else None
    bench = _aligned_close(res, res.benchmark, dates) if "benchmark" in opts else None
    out = []
    for kind in OVERLAY_COLORS:
        if kind not in kinds or kind not in opts:
            continue
        if kind == "ratio":
            out.append(Overlay(opts[kind], etf / bench, OVERLAY_COLORS[kind], tickformat=".4f", dash="dot"))
        else:
            out.append(Overlay(opts[kind].split(" ")[0], etf if kind == "etf" else bench, OVERLAY_COLORS[kind]))
    return out


def symbol_chart(res: RunResult, symbol: str) -> go.Figure:
    """One chart per symbol, shared by all its strategies: the signal levels and markers of every evaluated
    strategy, nothing strategy-specific, so switching strategy leaves the chart (and its zoom) untouched.
    Every available comparison line (sector ETF, benchmark, ratio) is included, hidden, behind a chart button."""
    s = res.settings
    sa = res.analyses[symbol]
    sigs = [h for r in strategy_rows(res, symbol) for h in (r.cand.signals if r.cand else r.ev.qualifying)]
    levels = chart_levels(sa, [h.level for h in sigs if h.level is not None])
    bars = sorted({h.bar_idx for h in sigs})
    return price_figure(sa, levels, bars, min(s.display.chart_bars, APP_CHART_BARS), s.levels.zone_atr_mult,
                        s.indicators.rsi_overbought, s.indicators.rsi_oversold,
                        overlays=chart_overlays(res, symbol, list(overlay_choices(res, symbol))))


def detail_view(res: RunResult, cfg: Config, symbol: str, strategy_id: str) -> dict:
    """Everything the setup pane shows for one (symbol, strategy)."""
    s = res.settings
    k = s.levels.zone_atr_mult
    sa = res.analyses[symbol]
    row = next(r for r in strategy_rows(res, symbol) if r.ev.strategy.id == strategy_id)
    ev, cand, st = row.ev, row.cand, row.ev.strategy
    if cand:
        alerts = [(a.label, a.price, a.meaning) for a in cand.alerts]
    else:
        alerts = [(f"{humanize(nr.signal)}: {a.label}", a.price, a.meaning) for nr in watch_nears(ev, sa.near)
                  for a in nr.triggers]
        if not alerts:  # nothing close to triggering: show the nearest zones so the ladder still frames the price
            alerts = [(label, lv[edge], f"{lv['type']} zone {edge.split('_')[1]}")
                      for label, rows, edge in (("Nearest resistance", sa.nearest_resistance, "zone_high"),
                                                ("Nearest support", sa.nearest_support, "zone_low"))
                      for lv in [level_dict(r["level"], sa, k) for r in rows[:1]]]
    lv_rows = [level_dict(r["level"], sa, k) for r in sa.nearest_resistance[::-1] + sa.nearest_support]
    t = sa.last_idx
    close = float(sa.bars.close[t])
    return {
        "row": row, "strategy": st, "trend": sa.trend.state, "trend_reason": sa.trend.reason,
        "flow": list(sa.flow.components()) if sa.flow else [], "flow_label": ev.flow_label,
        "rules": rule_lines(ev, sa, s), "hits": signal_hits(ev, cand),
        "levels": lv_rows, "alerts": alerts, "ladder": price_ladder(alerts, close),
        "score": [(x.name, x.raw, x.weight, x.points, x.note) for x in cand.components] if cand else [],
        "close": close, "change_pct": change_pct(sa), "atr": float(sa.bars.atr[t]), "rsi": float(sa.bars.rsi[t]),
        "sector": sa.sector_etf,
    }


def strategy_recipe(stg: Strategy) -> list[tuple[str, str]]:
    """(term, value) pairs that sum up a strategy's technical rules on one line, in rule order."""
    tech = stg.technical
    out = [("Trend", "any" if tech.trend == ["any"] else ", ".join(humanize(t) for t in tech.trend))]
    if tech.signals_all:
        out.append(("Requires", " + ".join(humanize(n) for n in tech.signals_all)))
    if tech.signals_any:
        out.append(("Any of", " / ".join(humanize(n) for n in tech.signals_any)))
    ne = stg.not_extended_resolved
    if ne in ("up", "down"):
        out.append(("Not", "overbought" if ne == "up" else "oversold"))
    out.append(("Flow", {"soft": "scored", "required": "required", "ignore": "ignored"}[tech.alignment]))
    return out


# ----------------------------------------------------------------------------------------------
# Ticker descriptions
# ----------------------------------------------------------------------------------------------


def _clip_sentences(text: str, limit: int) -> str:
    """`text` cut at the last sentence end within `limit` characters (or a word boundary, with an ellipsis)."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if end > limit // 3:
        return head[:end + 1]
    return head.rsplit(" ", 1)[0] + "…"


def ticker_description(cfg: Config, profiles: dict[str, Profile], symbol: str) -> tuple[str, str] | None:
    """(name, summary) for a symbol, or None. The name is your universe.csv note if set, else the yfinance profile
    name; the summary is the yfinance business summary."""
    entry = next((u for u in cfg.universe if u.symbol == symbol), None)
    p = profiles.get(symbol) or Profile()
    name = (entry.notes.strip() if entry is not None else "") or p.name
    if not (name or p.summary):
        return None
    return name, p.summary


def card_tooltip(desc: tuple[str, str] | None, limit: int = 300) -> str | None:
    """Short hover text for a symbol: the name in bold, then the summary up to about `limit` characters."""
    if desc is None:
        return None
    name, summary = desc
    text = _clip_sentences(summary, limit)
    return f"**{name}**\n\n{text}" if name and text else (f"**{name}**" if name else text)


def split_summary(summary: str, sentences: int = 2) -> tuple[str, str]:
    """(first `sentences` sentences, the rest) for the setup header and its "About" tab."""
    parts = re.split(r"(?<=[.!?])\s+", summary.strip())
    return " ".join(parts[:sentences]), " ".join(parts[sentences:])
