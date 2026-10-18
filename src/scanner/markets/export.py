"""Offline HTML snapshot of the Markets page for one resolution (tables plus interactive Plotly figures).

plotly.js is embedded once, inline, so the file opens without a network connection (about 4–5 MB).
"""

from __future__ import annotations

import math
from datetime import datetime

from scanner.markets.config import LABELS
from scanner.markets.figures import avg_corr_figure, corr_heatmap, explained_figure, loadings_figure, rrg_figure
from scanner.markets.run import MarketsResult
from scanner.markets.structure import detail_figure, level_rows
from scanner.report import _env


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if isinstance(v, bool):
        return "yes" if v else ""
    if isinstance(v, float):
        return f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def _table(rows: list[dict], drop: tuple[str, ...] = ()) -> dict:
    cols = [c for c in (rows[0] if rows else {}) if c not in drop]
    return {"cols": cols, "rows": [[_cell(r[c]) for c in cols] for r in rows]}


def render_markets_html(r: MarketsResult) -> str:
    first = [True]

    def html(fig) -> str:
        out = fig.to_html(full_html=False, include_plotlyjs="inline" if first[0] else False,
                          config={"displaylogo": False})
        first[0] = False
        return out

    k = r.settings.levels.zone_atr_mult
    structure = [{"label": label, "fig": html(detail_figure(sa, r.settings, r.resolution, r.display_bars)),
                  "levels": _table(level_rows(sa, k))} for label, sa in r.analyses.items()]
    p = r.pca
    pca_figs = []
    if not p.explained.empty:
        pca_figs = [html(explained_figure(p)), html(avg_corr_figure(p)), html(loadings_figure(p)),
                    html(corr_heatmap(p.corr_now, f"Correlation · last {p.window} bars"))]
        if p.corr_change is not None:
            pca_figs.append(html(corr_heatmap(p.corr_change, f"Correlation change vs {p.window} bars earlier")))
    return _env().get_template("markets.html.j2").render(
        title=f"Markets · {LABELS[r.resolution]}", as_of=r.as_of.isoformat() if r.as_of else "n/a",
        generated=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        summary=_table(r.summary, drop=("Provisional",)), structure=structure,
        rrg_fig=html(rrg_figure(r.rrg, r.rrg_order)) if r.rrg.tails else "", rrg_table=_table(r.rrg.table),
        benchmark=r.rrg.benchmark, pca_figs=pca_figs, shifts=_table(p.pair_shifts()),
        diagnostics=[f"{d.severity.upper()} {d.symbol}: {d.message}" for d in r.diagnostics],
    )


def export_name(r: MarketsResult) -> str:
    return f"markets_{r.as_of.isoformat() if r.as_of else 'na'}_{r.resolution}.html"
