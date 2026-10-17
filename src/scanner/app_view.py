"""View model for the Streamlit app: rank symbols by % of strategy rules met, filter a finished scan, detail panel.

Pure functions over a `RunResult` (no Streamlit), so they are unit-tested like the rest of the scanner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import cache
from importlib import resources

from scanner.charts import chart_levels, svg_chart
from scanner.config import Config
from scanner.models import Candidate, Tri
from scanner.pipeline import RunResult
from scanner.report import _plan_items, level_dict
from scanner.strategies import StrategyEval, watch_nears


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

    @property
    def best(self) -> StrategyRow:
        return self.rows[0]

    @property
    def n_full(self) -> int:
        return sum(1 for r in self.rows if r.total and r.met == r.total)

    @property
    def pending(self) -> bool:
        return any(r.pending for r in self.rows)


def _row_key(r: StrategyRow) -> tuple:
    return (-r.fraction, -r.score, r.ev.strategy.name)


def strategy_rows(res: RunResult, symbol: str) -> list[StrategyRow]:
    """Every evaluated strategy for `symbol`, best % of rules met first. PENDING_CLOSE rules do not count as met."""
    cands = {c.strategy_id: c for c in res.candidates if c.symbol == symbol}
    rows = [StrategyRow(ev, sum(r.status is Tri.TRUE for r in ev.rules), len(ev.rules),
                        sum(r.status is Tri.PENDING for r in ev.rules), cands.get(ev.strategy.id))
            for ev in res.evals.get(symbol, [])]
    return sorted(rows, key=_row_key)


def symbol_rankings(res: RunResult) -> list[SymbolRow]:
    """One row per analysed symbol with strategies; best fraction desc, then best candidate score, then symbol."""
    out = []
    for sym, sa in res.analyses.items():
        rows = strategy_rows(res, sym)
        if not rows:
            continue
        t = sa.last_idx
        close = float(sa.bars.close[t])
        prev = float(sa.bars.close[t - 1]) if t > 0 else close
        out.append(SymbolRow(sym, rows, close, (close / prev - 1) * 100 if prev else 0.0, sa.trend.state))
    best_score = {r.symbol: max(x.score for x in r.rows) for r in out}
    return sorted(out, key=lambda r: (-r.best.fraction, -best_score[r.symbol], r.symbol))


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


@cache
def _report_css() -> str:
    text = (resources.files("scanner") / "templates" / "scan.html.j2").read_text(encoding="utf-8")
    m = re.search(r"<style>.*?</style>", text, re.DOTALL)
    return m.group(0) if m else ""


def chart_page(svg: str) -> str:
    """A standalone page for one SVG chart, styled with the report's CSS (light/dark aware)."""
    return (f'<!doctype html><html><head><meta charset="utf-8">{_report_css()}</head>'
            f'<body style="margin:0;background:var(--panel)">{svg}</body></html>')


def detail_view(res: RunResult, cfg: Config, symbol: str, strategy_id: str) -> dict:
    """Everything the detail panel shows for one (symbol, strategy)."""
    s = res.settings
    k = s.levels.zone_atr_mult
    sa = res.analyses[symbol]
    row = next(r for r in strategy_rows(res, symbol) if r.ev.strategy.id == strategy_id)
    ev, cand, st = row.ev, row.cand, row.ev.strategy
    sigs = cand.signals if cand else ev.qualifying
    sig_levels = [x.level for x in sigs if x.level is not None]
    levels = chart_levels(sa, sig_levels)
    chart = svg_chart(sa, levels, [x.bar_idx for x in sigs], s.report.chart_bars, k,
                      s.indicators.rsi_overbought, s.indicators.rsi_oversold)
    lv_rows = [level_dict(r["level"], sa, k) for r in sa.nearest_resistance[::-1] + sa.nearest_support]
    if cand:
        alerts = [(a.label, a.price, a.meaning) for a in cand.alerts]
    else:
        alerts = [(f"{nr.signal}: {a.label}", a.price, a.meaning) for nr in watch_nears(ev, sa.near)
                  for a in nr.triggers]
    t = sa.last_idx
    return {
        "row": row, "strategy": st, "trend_reason": sa.trend.reason,
        "flow": list(sa.flow.components()) if sa.flow else [], "flow_label": ev.flow_label,
        "rules": [(r.rule, r.status, r.detail, list(r.evidence)) for r in ev.rules],
        "chart": chart, "levels": lv_rows, "alerts": alerts,
        "score": [(x.name, x.raw, x.weight, x.points, x.note) for x in cand.components] if cand else [],
        "plan": _plan_items(st, sa, s), "construction": list(st.construction),
        "checklist": list(st.orats_checklist), "manual_checks": list(st.other_manual_checks),
        "close": float(sa.bars.close[t]), "atr": float(sa.bars.atr[t]), "rsi": float(sa.bars.rsi[t]),
    }
