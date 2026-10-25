"""View model for the Streamlit app: market overview, the Bullish / Bearish / Neutral scan lists, the setup pane
(criterion checklist, price ladder, chart) and ticker descriptions.

Pure functions over a `RunResult` (no Streamlit), so they are unit-tested like the rest of the scanner.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

from scanner.config import Config, Scan
from scanner.data import Profile
from scanner.labels import DIRECTION_ICON, LABELS, STATUS_ICON, TREND_ICON, change_pct, humanize, trend_text
from scanner.models import CriterionResult, ScanResult, SignalResult, SymbolAnalysis, Tri, unique_by_id
from scanner.pipeline import RunResult
from scanner.plotly_chart import Overlay, chart_levels, price_figure
from scanner.scans import criterion_label, watch_nears
from scanner.views import level_dict, market_context

if TYPE_CHECKING:
    import plotly.graph_objects as go

__all__ = ["DIRECTION_ICON", "LABELS", "STATUS_ICON", "TREND_ICON", "change_pct", "humanize",
           "trend_text"]  # re-exported from labels


# ----------------------------------------------------------------------------------------------
# Scan lists
# ----------------------------------------------------------------------------------------------


def scan_lists(res: RunResult, symbols: list[str] | None = None,
               sectors: list[str] | None = None) -> dict[str, list[ScanResult]]:
    """Each active scan's listed tickers (already ranked), narrowed to `symbols` and `sectors` (empty or None =
    all). Every active scan is a key, in display order."""
    syms, secs = set(symbols or []) or None, set(sectors or []) or None

    def keep(r: ScanResult) -> bool:
        return (syms is None or r.symbol in syms) and (secs is None or res.analyses[r.symbol].sector_etf in secs)

    return {name: [r for r in res.lists.get(name, []) if keep(r)] for name in res.scans}


def ticker_scans(res: RunResult, symbol: str) -> list[ScanResult]:
    """The symbol's evaluation in every active scan, in display order."""
    by_scan = res.results.get(symbol, {})
    return [by_scan[n] for n in res.scans if n in by_scan]


def listed_scans(res: RunResult, symbol: str) -> list[str]:
    """The scans that list `symbol` (reach their min_pct)."""
    return [r.scan for r in ticker_scans(res, symbol) if r.passed]


def best_scan(res: RunResult, symbol: str) -> str | None:
    """The scan to open for a symbol: its highest match among the scans that list it, else its highest match."""
    rows = ticker_scans(res, symbol)
    if not rows:
        return None
    return max(rows, key=lambda r: (r.passed, round(r.pct, 9), -res.scans.index(r.scan))).scan


# ----------------------------------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------------------------------


def overview(res: RunResult, cfg: Config) -> dict:
    """Top-of-page summary: session, benchmark, tickers listed per scan, and one entry per sector ETF with the
    number of its members listed in any scan."""
    market, sectors = market_context(res, cfg)
    listed = {r.symbol for rows in res.lists.values() for r in rows}
    for e in sectors:
        e["setups"] = sum(1 for m in e["members"] if m in listed)
    warnings = [d for d in res.diagnostics if d.severity != "info"]
    return {"as_of": res.as_of, "provisional": res.provisional, "notes": list(res.notes), "market": market,
            "sectors": sectors, "n_listed": len(listed), "n_symbols": len(res.analyses),
            "counts": {n: len(res.lists.get(n, [])) for n in res.scans}, "watch": res.watchlist,
            "skipped": res.skipped, "warnings": warnings}


# ----------------------------------------------------------------------------------------------
# Setup pane
# ----------------------------------------------------------------------------------------------


@dataclass
class RuleLine:
    check: str
    label: str
    status: Tri
    weight: float
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


def _summary(c: CriterionResult, sa: SymbolAnalysis, flow_label: str) -> str:
    if c.check == "trend":
        return f"{trend_text(sa.trend.state)} (wanted: {', '.join(humanize(t) for t in c.spec.value)})"
    if c.check in ("signal_all", "signal_any"):
        hits: dict[str, list[SignalResult]] = {}
        for h in c.signals:
            hits.setdefault(h.name, []).append(h)
        names = c.spec.value
        fired = [n for n in names if hits.get(n)]
        if c.check == "signal_any" and not fired:
            return "none fired (any of: " + ", ".join(humanize(n) for n in names) + ")"
        shown = names if c.check == "signal_all" else fired
        return "; ".join(_signal_line(n, hits.get(n, [])) for n in shown)
    if c.check.startswith("rsi_") or c.check == "channel":
        return c.detail
    comps = " · ".join(f"{k} {humanize(v).lower()}" for k, v in sa.flow.components()) if sa.flow else "n/a"
    return f"{humanize(flow_label)} ({comps})"


def rule_lines(r: ScanResult, sa: SymbolAnalysis) -> list[RuleLine]:
    """One readable line per criterion (the raw evidence is kept for the drill-down)."""
    return [RuleLine(c.check, c.label, c.status, c.weight, _summary(c, sa, r.flow_label), c.detail,
                     list(c.evidence)) for c in r.criteria]


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


def signal_hits(r: ScanResult) -> list[dict]:
    """One row per signal that fired for this scan (the structured form of the criterion evidence)."""
    out = []
    for h in r.signals:
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
    """One chart per symbol, shared by all its scans: the signal levels and markers of every scan, nothing
    scan-specific, so switching scan leaves the chart (and its zoom) untouched. Every available comparison line
    (sector ETF, benchmark, ratio) is included, hidden, behind a chart button."""
    s = res.settings
    sa = res.analyses[symbol]
    sigs = unique_by_id([h for r in ticker_scans(res, symbol) for h in r.signals])
    levels = chart_levels(sa, [h.level for h in sigs if h.level is not None])
    bars = sorted({h.bar_idx for h in sigs})
    return price_figure(sa, levels, bars, min(s.display.chart_bars, APP_CHART_BARS), s.levels.zone_atr_mult,
                        s.indicators.rsi_overbought, s.indicators.rsi_oversold,
                        overlays=chart_overlays(res, symbol, list(overlay_choices(res, symbol))))


def detail_view(res: RunResult, symbol: str, scan: str) -> dict:
    """Everything the setup pane shows for one (symbol, scan)."""
    s = res.settings
    k = s.levels.zone_atr_mult
    sa = res.analyses[symbol]
    r = res.results[symbol][scan]
    if r.primary is not None:
        alerts = [(a.label, a.price, a.meaning) for a in r.alerts]
    else:
        alerts = [(f"{humanize(nr.signal)}: {a.label}", a.price, a.meaning) for nr in watch_nears(r, sa.near)
                  for a in nr.triggers]
        if not alerts:  # nothing close to triggering: show the nearest zones so the ladder still frames the price
            alerts = [(label, lv[edge], f"{lv['type']} zone {edge.split('_')[1]}")
                      for label, rows, edge in (("Nearest resistance", sa.nearest_resistance, "zone_high"),
                                                ("Nearest support", sa.nearest_support, "zone_low"))
                      for lv in [level_dict(row["level"], sa, k) for row in rows[:1]]]
    lv_rows = [level_dict(row["level"], sa, k) for row in sa.nearest_resistance[::-1] + sa.nearest_support]
    t = sa.last_idx
    close = float(sa.bars.close[t])
    return {
        "result": r, "trend": sa.trend.state, "trend_reason": sa.trend.reason,
        "flow": list(sa.flow.components()) if sa.flow else [], "flow_label": r.flow_label,
        "rules": rule_lines(r, sa), "hits": signal_hits(r), "levels": lv_rows, "alerts": alerts,
        "ladder": price_ladder(alerts, close), "close": close, "change_pct": change_pct(sa),
        "atr": float(sa.bars.atr[t]), "rsi": float(sa.bars.rsi[t]), "sector": sa.sector_etf,
    }


def scan_recipe(scan: Scan) -> list[str]:
    """One short label per criterion, with its weight when it is not 1."""
    return [criterion_label(c) + (f" ×{c.weight:g}" if c.weight != 1 else "") for c in scan.criteria]


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
