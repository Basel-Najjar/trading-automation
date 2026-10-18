"""Plotly figures for the Markets page: the RRG and the PCA views. Price charts reuse `plotly_chart.price_figure`.

Colours follow one palette: categorical slots in fixed order (an entity keeps its slot, whatever filters remain),
a blue↔red diverging scale with a gray midpoint for correlations, and recessive gray for reference marks.
Every chart has one y-axis; identity is never colour alone (legends plus direct labels at the RRG tail heads).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from scanner.markets.pca import PcaResult
from scanner.markets.rrg import RrgResult, axis_range

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OTHER = "#8d8d8d"  # members past the eighth slot: gray, identified by their direct label
MUTED = "rgba(128,128,128,0.55)"
DIVERGING = [[0.0, "#2a78d6"], [0.5, "#f0efec"], [1.0, "#e34948"]]  # −1 blue · 0 gray · +1 red
QUADRANT_FILL = {"Leading": "rgba(12,163,12,0.07)", "Weakening": "rgba(237,161,0,0.08)",
                 "Lagging": "rgba(208,59,59,0.07)", "Improving": "rgba(42,120,214,0.07)"}
FONT = {"size": 12}


def series_color(i: int) -> str:
    return CATEGORICAL[i] if i < len(CATEGORICAL) else OTHER


def _base(fig: go.Figure, height: int, margin: dict | None = None, **layout) -> go.Figure:
    fig.update_layout(height=height, margin=margin or {"l": 56, "r": 16, "t": 36, "b": 40}, font=FONT,
                      legend={"orientation": "h", "yanchor": "top", "y": -0.12, "x": 0}, **layout)
    return fig


# ----------------------------------------------------------------------------------------------
# RRG
# ----------------------------------------------------------------------------------------------


def rrg_figure(r: RrgResult, order: list[str], tail: int | None = None, height: int = 620) -> go.Figure:
    """Quadrant scatter with one tail per member. `order` is the configured member order (colour slots follow it,
    so hiding a member never repaints the others). `tail` shortens the drawn trail."""
    tails = {m: (t if tail is None else t.tail(tail + 1)) for m, t in r.tails.items()}
    xr, yr = axis_range(tails)
    fig = go.Figure()
    for name, x0, x1, y0, y1, pos in (("Leading", 100, xr[1], 100, yr[1], ("right", "top")),
                                      ("Weakening", 100, xr[1], yr[0], 100, ("right", "bottom")),
                                      ("Lagging", xr[0], 100, yr[0], 100, ("left", "bottom")),
                                      ("Improving", xr[0], 100, 100, yr[1], ("left", "top"))):
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, fillcolor=QUADRANT_FILL[name], line_width=0,
                      layer="below")
        fig.add_annotation(x=x1 if pos[0] == "right" else x0, y=y1 if pos[1] == "top" else y0, text=name,
                           showarrow=False, xanchor=pos[0], yanchor=pos[1], font={"size": 12, "color": MUTED})
    fig.add_hline(y=100, line={"color": MUTED, "width": 1})
    fig.add_vline(x=100, line={"color": MUTED, "width": 1})
    for m, t in tails.items():
        i = order.index(m) if m in order else len(CATEGORICAL)
        color = series_color(i)
        dates = [d.date().isoformat() for d in t.index]
        n = len(t)
        fig.add_trace(go.Scatter(
            x=t["rs_ratio"], y=t["rs_mom"], mode="lines+markers", name=m, legendgroup=m,
            line={"color": color, "width": 2}, opacity=1.0,
            marker={"size": [6] * (n - 1) + [11], "color": color, "line": {"width": 2, "color": "white"}},
            customdata=dates,
            hovertemplate=f"<b>{m}</b> %{{customdata}}<br>RS-Ratio %{{x:.2f}}<br>RS-Momentum %{{y:.2f}}<extra></extra>",
        ))
        fig.add_annotation(x=float(t["rs_ratio"].iloc[-1]), y=float(t["rs_mom"].iloc[-1]), text=m, showarrow=False,
                           xanchor="left", xshift=8, font={"size": 11})
    fig.update_xaxes(range=xr, title_text="RS-Ratio (relative strength vs " + r.benchmark + ")", zeroline=False,
                     showgrid=False)
    fig.update_yaxes(range=yr, title_text="RS-Momentum", zeroline=False, showgrid=False)
    return _base(fig, height, hovermode="closest", showlegend=True, uirevision="rrg")


# ----------------------------------------------------------------------------------------------
# PCA
# ----------------------------------------------------------------------------------------------


def explained_figure(p: PcaResult, height: int = 340) -> go.Figure:
    """Share of variance per component over time, stacked: the top of the stack is the absorption ratio."""
    fig = go.Figure()
    for i, col in enumerate(p.explained.columns):
        fig.add_trace(go.Scatter(
            x=p.explained.index, y=p.explained[col] * 100, name=col, mode="lines", stackgroup="pc",
            line={"color": series_color(i), "width": 2},
            hovertemplate=f"{col} %{{y:.1f}}%<extra></extra>",
        ))
    fig.update_yaxes(title_text="% of variance", range=[0, 100], ticksuffix="%")
    return _base(fig, height, hovermode="x unified", title={"text": "Variance explained (rolling)", "x": 0})


def avg_corr_figure(p: PcaResult, height: int = 260) -> go.Figure:
    fig = go.Figure(go.Scatter(x=p.avg_corr.index, y=p.avg_corr, mode="lines", name="Average correlation",
                               line={"color": CATEGORICAL[0], "width": 2},
                               hovertemplate="%{y:.2f}<extra>Average correlation</extra>"))
    fig.add_hline(y=0, line={"color": MUTED, "width": 1})
    fig.update_yaxes(title_text="mean pairwise ρ", range=[-1, 1])
    return _base(fig, height, hovermode="x unified", showlegend=False,
                 title={"text": "Average pairwise correlation (rolling)", "x": 0})


def loadings_figure(p: PcaResult, component: str = "PC1", height: int = 340) -> go.Figure:
    """One component's loadings now and one window earlier (signs aligned through time)."""
    fig = go.Figure()
    if p.loadings_prev is not None:
        fig.add_trace(go.Bar(x=p.members, y=p.loadings_prev[component], name="One window earlier",
                             marker={"color": MUTED, "line": {"width": 0}},
                             hovertemplate="%{x}: %{y:.2f}<extra>earlier</extra>"))
    fig.add_trace(go.Bar(x=p.members, y=p.loadings_now[component], name="Now",
                         marker={"color": CATEGORICAL[0], "line": {"width": 0}},
                         hovertemplate="%{x}: %{y:.2f}<extra>now</extra>"))
    fig.add_hline(y=0, line={"color": MUTED, "width": 1})
    fig.update_yaxes(title_text="loading", range=[-1, 1])
    return _base(fig, height, barmode="group", bargap=0.25, bargroupgap=0.08, hovermode="closest",
                 title={"text": f"{component} loadings", "x": 0})


def corr_heatmap(c: pd.DataFrame, title: str, zmax: float = 1.0, height: int = 420) -> go.Figure:
    z = c.to_numpy()
    text = np.vectorize(lambda v: f"{v:+.2f}")(z)
    fig = go.Figure(go.Heatmap(z=z, x=list(c.columns), y=list(c.index), zmin=-zmax, zmax=zmax, colorscale=DIVERGING,
                               text=text, texttemplate="%{text}", textfont={"size": 10}, xgap=2, ygap=2,
                               hovertemplate="%{y} · %{x}: %{z:+.2f}<extra></extra>",
                               colorbar={"thickness": 10, "len": 0.8}))
    fig.update_yaxes(autorange="reversed")
    return _base(fig, height, title={"text": title, "x": 0}, margin={"l": 80, "r": 16, "t": 36, "b": 60})
