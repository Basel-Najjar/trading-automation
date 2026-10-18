"""Trend, key levels and breakouts per series: the scanner's own engine (`analyze_symbol`) on Markets frames.

Same definitions as the scanner (decision B1 trend state, horizontal/diagonal/SMA levels, B5 2-bar breaks, holds,
engulfing), with the resolution's bar-count settings. Ratios are analysed on their body-only ratio candles.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from scanner.config import Settings
from scanner.labels import change_pct, humanize, trend_text
from scanner.markets.series import SeriesFrame
from scanner.models import Diagnostic, NearResult, SignalResult, SymbolAnalysis, Tri
from scanner.pipeline import analyze_symbol
from scanner.plotly_chart import HALF_DAY, chart_levels, price_figure
from scanner.views import level_dict

if TYPE_CHECKING:
    import plotly.graph_objects as go

RETURN_BARS = {"daily": {"1m": 21, "3m": 63, "6m": 126}, "weekly": {"3m": 13, "6m": 26, "1y": 52}}
HALF_BAR = {"daily": HALF_DAY, "weekly": timedelta(days=3, hours=12)}
KIND_RANK = {"break": 0, "hold": 1, "pattern": 2}  # what the "Signal" column reports first


def analyze_all(frames: dict[str, SeriesFrame], s: Settings) -> tuple[dict[str, SymbolAnalysis], list[Diagnostic]]:
    out: dict[str, SymbolAnalysis] = {}
    diags: list[Diagnostic] = []
    for label, f in frames.items():
        try:
            sa = analyze_symbol(label, f.completed, s, f.provisional)
        except Exception as e:  # one series never breaks the page
            diags.append(Diagnostic(label, "analysis", "error", f"{type(e).__name__}: {e}"))
            continue
        out[label] = sa
        diags += sa.diagnostics
    return out, diags


def fired(sa: SymbolAnalysis) -> list[SignalResult]:
    """Breaks, holds and patterns that fired (or are pending on a forming bar), most important first."""
    sigs = [x for x in sa.signals if x.kind in KIND_RANK and x.status in (Tri.TRUE, Tri.PENDING)]
    return sorted(sigs, key=lambda x: (x.age, KIND_RANK[x.kind], x.name))


def _bars_return(sa: SymbolAnalysis, n: int) -> float | None:
    t = sa.last_idx
    if t < n:
        return None
    prev = float(sa.bars.close[t - n])
    return (float(sa.bars.close[t]) / prev - 1) * 100 if prev else None


def _signal_text(x: SignalResult) -> str:
    if x.status is Tri.PENDING:
        when = "pending close"
    else:
        when = "this bar" if x.age == 0 else f"{x.age} bar{'s' if x.age > 1 else ''} ago"
    at = f" @ {x.level.value(x.bar_idx):,.4g}" if x.level is not None else ""
    return f"{humanize(x.name)}{at} ({when})"


def _near_text(n: NearResult, t: int) -> str:
    at = f" @ {n.level.value(t):,.4g}" if n.level is not None else ""
    if n.description.startswith("first close"):  # B5: one close beyond the zone, the confirming close still due
        return f"{humanize(n.signal)}{at}: 1 of 2 closes"
    return f"{humanize(n.signal)}{at} ({n.distance_atr:.2f} ATR)"


def summary_rows(analyses: dict[str, SymbolAnalysis], res: str) -> list[dict]:
    """One row per series for the Structure table."""
    rows = []
    for label, sa in analyses.items():
        t = sa.last_idx
        sup = sa.nearest_support[0] if sa.nearest_support else None
        rst = sa.nearest_resistance[0] if sa.nearest_resistance else None
        sigs = fired(sa)
        nears = sorted((n for ns in sa.near.values() for n in ns), key=lambda n: n.distance_atr)
        row = {"Series": label, "Last": float(sa.bars.close[t]), "Chg %": change_pct(sa)}
        for name, n in RETURN_BARS[res].items():
            row[name] = _bars_return(sa, n)
        row |= {
            "Trend": trend_text(sa.trend.state),
            "Support": sup["value"] if sup else None, "Sup ATR": sup["dist_atr"] if sup else None,
            "Resistance": rst["value"] if rst else None, "Res ATR": rst["dist_atr"] if rst else None,
            "Signal": _signal_text(sigs[0]) if sigs else "",
            "Near": _near_text(nears[0], t) if nears else "",
            "Provisional": sa.provisional,
        }
        rows.append(row)
    return rows


def level_rows(sa: SymbolAnalysis, k: float) -> list[dict]:
    """The nearest levels above and below the close, highest first."""
    out = []
    for r in sa.nearest_resistance[::-1] + sa.nearest_support:
        lv = level_dict(r["level"], sa, k)
        out.append({"Level": humanize(lv["type"]), "Role": humanize(lv["role"]), "Price": lv["level"],
                    "Zone": f"{lv['zone_low']:,.4g} – {lv['zone_high']:,.4g}", "ATR away": lv["dist_atr"],
                    "Touches": lv["touches"]})
    return out


def signal_rows(sa: SymbolAnalysis) -> list[dict]:
    return [{"Signal": humanize(x.name), "Kind": x.kind, "Direction": x.direction, "Date": x.date.isoformat(),
             "Status": "pending" if x.status is Tri.PENDING else "fired",
             "Level": x.level.value(x.bar_idx) if x.level is not None else None} for x in fired(sa)]


def detail_figure(sa: SymbolAnalysis, s: Settings, res: str, display_bars: int) -> go.Figure:
    """The scanner's interactive price chart for one series: levels, SMAs, pivots, fired-signal bars and RSI."""
    sigs = fired(sa)
    levels = chart_levels(sa, [x.level for x in sigs if x.level is not None])
    return price_figure(sa, levels, sorted({x.bar_idx for x in sigs}), display_bars, s.levels.zone_atr_mult,
                        s.indicators.rsi_overbought, s.indicators.rsi_oversold, half_bar=HALF_BAR[res],
                        session_breaks=res == "daily", uirevision=f"{sa.symbol}:{res}")
