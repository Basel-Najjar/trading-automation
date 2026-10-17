"""View model for the Streamlit app: market overview, opportunity list grouped by symbol, run-relative score tiers,
the setup pane (rule one-liners, price ladder, chart), post-scan filtering and ticker descriptions.

Pure functions over a `RunResult` (no Streamlit), so they are unit-tested like the rest of the scanner.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import numpy as np

from scanner.charts import chart_levels
from scanner.config import Config, Strategy
from scanner.data import Profile
from scanner.models import Candidate, RuleResult, SignalResult, SymbolAnalysis, Tri
from scanner.pipeline import RunResult
from scanner.plotly_chart import Overlay, price_figure
from scanner.report import level_dict, market_context
from scanner.strategies import StrategyEval, watch_nears

if TYPE_CHECKING:
    import plotly.graph_objects as go

# ----------------------------------------------------------------------------------------------
# Labels
# ----------------------------------------------------------------------------------------------

LABELS = {
    # biases
    "bullish": "Bullish", "bearish": "Bearish", "non_bearish": "Not bearish", "non_bullish": "Not bullish",
    "neutral": "Neutral",
    # flow
    "with_flow": "With flow", "countertrend": "Against flow", "mixed": "Mixed", "ignored": "Flow ignored",
    # trend
    "uptrend": "Uptrend", "downtrend": "Downtrend", "range": "Range",
    # rules
    "trend": "Trend", "signals_all": "Required", "signals_any": "Trigger", "not_extended": "Not extended",
    "alignment": "Flow",
}
TREND_ICON = {"uptrend": "▲", "downtrend": "▼", "range": "↔", "mixed": "≈"}
BIAS_GROUPS = {"Bullish": ("bullish", "non_bearish"), "Bearish": ("bearish", "non_bullish"), "Neutral": ("neutral",)}
STATUS_ICON = {Tri.TRUE: "✓", Tri.FALSE: "✗", Tri.PENDING: "⏳"}


def humanize(token: str | None) -> str:
    """Readable label for a config/scanner identifier (`non_bearish` → "Not bearish", `support_hold` →
    "Support hold"). Unknown tokens get underscores replaced and the first letter capitalised."""
    if not token:
        return "n/a"
    if token in LABELS:
        return LABELS[token]
    text = token.replace("_", " ")
    return text[:1].upper() + text[1:]


def trend_text(state: str) -> str:
    icon = TREND_ICON.get(state)
    return f"{icon} {humanize(state)}" if icon else humanize(state)


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
    def n_full(self) -> int:
        return sum(1 for r in self.rows if r.total and r.met == r.total)

    @property
    def pending(self) -> bool:
        return any(r.pending for r in self.rows)

    @property
    def matches(self) -> list[StrategyRow]:
        """Strategies with a candidate (a full or pending-close match), best first."""
        return [r for r in self.rows if r.cand]


def _row_key(r: StrategyRow) -> tuple:
    return (-r.fraction, -r.score, r.ev.strategy.name)


def strategy_rows(res: RunResult, symbol: str) -> list[StrategyRow]:
    """Every evaluated strategy for `symbol`, best % of rules met first. PENDING_CLOSE rules do not count as met."""
    cands = {c.strategy_id: c for c in res.candidates if c.symbol == symbol}
    rows = [StrategyRow(ev, sum(r.status is Tri.TRUE for r in ev.rules), len(ev.rules),
                        sum(r.status is Tri.PENDING for r in ev.rules), cands.get(ev.strategy.id))
            for ev in res.evals.get(symbol, [])]
    return sorted(rows, key=_row_key)


def change_pct(sa: SymbolAnalysis) -> float:
    t = sa.last_idx
    close = float(sa.bars.close[t])
    prev = float(sa.bars.close[t - 1]) if t > 0 else close
    return (close / prev - 1) * 100 if prev else 0.0


def symbol_rankings(res: RunResult) -> list[SymbolRow]:
    """One row per analysed symbol with strategies; best fraction desc, then best candidate score, then symbol."""
    out = []
    for sym, sa in res.analyses.items():
        rows = strategy_rows(res, sym)
        if not rows:
            continue
        out.append(SymbolRow(sym, rows, float(sa.bars.close[sa.last_idx]), change_pct(sa), sa.trend.state,
                             sa.sector_etf))
    best_score = {r.symbol: max(x.score for x in r.rows) for r in out}
    return sorted(out, key=lambda r: (-r.best.fraction, -best_score[r.symbol], r.symbol))


def _strategy_ok(r: StrategyRow, sids: set[str] | None, biases: set[str] | None) -> bool:
    return (sids is None or r.ev.strategy.id in sids) and (biases is None or r.ev.strategy.bias in biases)


def _bias_set(groups: list[str] | None) -> set[str] | None:
    return {b for g in groups for b in BIAS_GROUPS.get(g, (g,))} if groups else None


def opportunity_rows(res: RunResult, symbols: list[str] | None = None, strategy_ids: list[str] | None = None,
                     bias_groups: list[str] | None = None, sectors: list[str] | None = None,
                     matches_only: bool = True) -> list[SymbolRow]:
    """One row per symbol, its strategies limited to the filters (empty or None = all). `bias_groups` are keys of
    BIAS_GROUPS. With `matches_only`, symbols without a candidate among the kept strategies are dropped.
    Order: symbols with a candidate first (best score desc), then by share of rules met, then symbol."""
    syms, secs = set(symbols or []) or None, set(sectors or []) or None
    sids, biases = set(strategy_ids or []) or None, _bias_set(bias_groups)
    out = []
    for s in symbol_rankings(res):
        if (syms is not None and s.symbol not in syms) or (secs is not None and s.sector not in secs):
            continue
        rows = [r for r in s.rows if _strategy_ok(r, sids, biases)]
        if not rows or (matches_only and not any(r.cand for r in rows)):
            continue
        out.append(replace(s, rows=rows))
    return sorted(out, key=lambda s: (s.best.cand is None, -s.best.score, -s.best.fraction, s.symbol))


def setup_pairs(rows: list[SymbolRow], matches_only: bool = True) -> list[tuple[SymbolRow, StrategyRow]]:
    """The "by setup" view of `opportunity_rows`: one entry per (symbol, strategy), candidates first by score,
    then by share of rules met. With `matches_only`, only candidates."""
    pairs = [(s, r) for s in rows for r in s.rows if r.cand or not matches_only]
    return sorted(pairs, key=lambda p: (p[1].cand is None, -p[1].score, -p[1].fraction, p[0].symbol,
                                        p[1].ev.strategy.name))


# ----------------------------------------------------------------------------------------------
# Tiers
# ----------------------------------------------------------------------------------------------

TIER_SHARES = (0.2, 0.6)  # A = top 20% of this run's candidates, B = down to 60%, C = the rest


def tier_cutoffs(scores: list[float], shares: tuple[float, float] = TIER_SHARES) -> tuple[float, float]:
    """(lowest A score, lowest B score) for this run's candidate scores. Ties share a tier. No scores → (inf, inf)."""
    s = sorted(scores, reverse=True)
    if not s:
        return math.inf, math.inf
    pick = [s[max(math.ceil(share * len(s)), 1) - 1] for share in shares]
    return pick[0], pick[1]


def tier(score: float | None, cutoffs: tuple[float, float]) -> str | None:
    """"A", "B" or "C" relative to the run (see `tier_cutoffs`); None when there is no candidate score."""
    if score is None:
        return None
    return "A" if score >= cutoffs[0] else "B" if score >= cutoffs[1] else "C"


# ----------------------------------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------------------------------


def overview(res: RunResult, cfg: Config) -> dict:
    """Top-of-page summary: session, benchmark, counts, and one entry per sector ETF with its setup count."""
    market, sectors = market_context(res, cfg)
    with_cand = {c.symbol for c in res.candidates}
    for e in sectors:
        e["setups"] = sum(1 for m in e["members"] if m in with_cand)
    warnings = [d for d in res.diagnostics if d.severity != "info"]
    return {"as_of": res.as_of, "provisional": res.provisional, "notes": list(res.notes), "market": market,
            "sectors": sectors, "n_candidates": len(res.candidates), "n_symbols_with": len(with_cand),
            "n_symbols": len(res.analyses), "watch": res.watchlist, "skipped": res.skipped, "warnings": warnings}


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
    for h in ev.qualifying:  # identity, not ==: dataclass equality would compare numpy arrays
        seen = hits.setdefault(h.name, [])
        if h.status is not Tri.FALSE and not any(x is h for x in seen):
            seen.append(h)
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


APP_CHART_BARS = 60  # the app opens the chart on fewer bars than the report, so the levels near price stay legible


def signal_hits(ev: StrategyEval, cand: Candidate | None) -> list[dict]:
    """One row per signal that fired for this setup (the structured form of the rule evidence)."""
    out, seen = [], []
    for h in (cand.signals if cand else ev.qualifying):
        if h.status is Tri.FALSE or any(x is h for x in seen):
            continue
        seen.append(h)
        out.append({"Signal": humanize(h.name), "Level": h.level.label if h.level else "",
                    "Price": h.level.value(h.bar_idx) if h.level else None, "Date": h.date, "Bars ago": h.age,
                    "Pending": "pending close" if h.status is Tri.PENDING else ""})
    return out


OVERLAY_COLORS = {"etf": "#12a1a1", "benchmark": "#8d8d8d", "ratio": "#c0399b"}


def _series(res: RunResult, symbol: str) -> SymbolAnalysis | None:
    return res.context.get(symbol) or res.analyses.get(symbol)


def overlay_choices(res: RunResult, symbol: str) -> dict[str, str]:
    """Comparison lines available for `symbol` ({kind: label}): its sector ETF, the benchmark and the ETF/benchmark
    ratio (the series the sector flow is read from). A series is offered only when it was loaded and is not the
    symbol itself."""
    sa = res.analyses[symbol]
    etf, bench = sa.sector_etf, res.benchmark
    has_etf = bool(etf) and etf != symbol and _series(res, etf) is not None
    has_bench = bench != symbol and _series(res, bench) is not None
    out = {}
    if has_etf:
        out["etf"] = f"{etf} (sector)"
    if has_bench:
        out["benchmark"] = f"{bench} (benchmark)"
    if has_etf and has_bench and etf != bench:
        out["ratio"] = f"{etf}/{bench}"
    return out


def _aligned_close(res: RunResult, other: str, dates: list) -> np.ndarray:
    o = _series(res, other)
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
    return price_figure(sa, levels, bars, min(s.report.chart_bars, APP_CHART_BARS), s.levels.zone_atr_mult,
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
# Filtering for the HTML report
# ----------------------------------------------------------------------------------------------


def filtered_result(res: RunResult, symbols: list[str] | None, strategy_ids: list[str] | None) -> RunResult:
    """A copy of a full scan limited to the picked symbols/strategies (empty or None = all), for the HTML report."""
    syms = set(symbols) if symbols else None
    sids = set(strategy_ids) if strategy_ids else None

    def keep(sym: str, sid: str) -> bool:
        return (syms is None or sym in syms) and (sids is None or sid in sids)

    evals = {sym: [ev for ev in evs if sids is None or ev.strategy.id in sids] for sym, evs in res.evals.items()}
    watch = []
    for w in res.watchlist:
        strats = [s for s in w.strategies if keep(w.symbol, s[0])]
        if strats:
            watch.append(replace(w, strategies=strats))
    return replace(res, evals=evals, candidates=[c for c in res.candidates if keep(c.symbol, c.strategy_id)],
                   watchlist=watch, strategies_enabled=[s for s in res.strategies_enabled if sids is None or s in sids],
                   focus_symbols=list(symbols) if symbols else None)


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
