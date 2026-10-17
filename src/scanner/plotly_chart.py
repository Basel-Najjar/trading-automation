"""Interactive Plotly chart for the Streamlit app (the HTML report keeps the static SVG in charts.py).

Same content as `charts.svg_chart`: candles, SMA50/200, level zones and lines, signal bars, pivots and an RSI pane,
plus volume bars overlaid along the bottom of the price pane. Only the candles have a tooltip (OHLC).
The whole analysis window is plotted; the initial view shows the last `chart_bars` bars, so you can pan back.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from scanner.models import Level, SymbolAnalysis

HEIGHT = 580
UP, DOWN = "#0ca30c", "#d03b3b"
CANDLE = {"increasing": {"line": {"color": UP}, "fillcolor": UP},
          "decreasing": {"line": {"color": DOWN}, "fillcolor": DOWN}}
SMA_COLORS = {"sma50": "#2a78d6", "sma200": "#eb6834"}
RSI_COLOR = "#6250d6"
ZONE_FILL = {"support": "rgba(12,163,12,0.12)", "resistance": "rgba(208,59,59,0.12)"}
SIGNAL_FILL = "rgba(237,161,0,0.18)"
VOL_UP, VOL_DOWN = "rgba(12,163,12,0.30)", "rgba(208,59,59,0.30)"
VOLUME_SHARE = 0.2  # the tallest visible volume bar reaches this fraction of the price pane
PRICE_PANE = {"row": 1, "col": 1, "secondary_y": True}
PIVOT_MUTED, PIVOT_USED = "rgba(128,128,128,0.55)", "#6250d6"
HALF_DAY = timedelta(hours=12)


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def missing_sessions(dates: list[date]) -> list[str]:
    """Weekdays between the first and last bar that have no bar (holidays, halts), for x-axis rangebreaks."""
    if not dates:
        return []
    have = set(dates)
    out, d = [], dates[0]
    while d <= dates[-1]:
        if d.weekday() < 5 and d not in have:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _finite(arr: np.ndarray, ts: range | list[int]) -> list[int]:
    return [t for t in ts if np.isfinite(arr[t])]


def _visible_range(sa: SymbolAnalysis, levels: list[Level], start: int, last: int, k: float) -> tuple[float, float]:
    """Initial y-range for the last `chart_bars` bars, padded like the SVG chart (levels near price widen it)."""
    b = sa.bars
    lo = float(np.nanmin(b.low[start:last + 1]))
    hi = float(np.nanmax(b.high[start:last + 1]))
    span = hi - lo or 1.0
    vis_lo, vis_hi = lo - 0.12 * span, hi + 0.12 * span
    for lv in levels:
        v = lv.value(last)
        if not np.isnan(v) and vis_lo <= v <= vis_hi:
            z = lv.zone(last, b.atr, k)
            lo, hi = min(lo, z[0]), max(hi, z[1])
    for arr in (b.sma50, b.sma200):
        seg = arr[start:last + 1]
        m = seg[np.isfinite(seg)]
        if m.size:
            lo = min(lo, float(np.clip(m.min(), vis_lo, vis_hi)))
            hi = max(hi, float(np.clip(m.max(), vis_lo, vis_hi)))
    pad = (hi - lo) * 0.04 or 1.0
    return lo - pad, hi + pad


def _level_start(sa: SymbolAnalysis, lv: Level) -> int:
    if lv.type == "diagonal" and lv.anchors:
        return next((t for t, d in enumerate(sa.bars.dates) if d >= lv.anchors[0][0]), 0)
    return 0


def _add_level(fig: go.Figure, sa: SymbolAnalysis, lv: Level, k: float, last: int) -> None:
    b = sa.bars
    ts = [t for t in range(_level_start(sa, lv), last + 1) if np.isfinite(lv.values[t]) and np.isfinite(b.atr[t])]
    if len(ts) < 2:
        return
    role = lv.role(last, float(b.close[last]))
    color = UP if role == "support" else DOWN
    xs = [_dt(b.dates[t]) for t in ts]
    top = [float(lv.values[t] + k * b.atr[t]) for t in ts]
    bot = [float(lv.values[t] - k * b.atr[t]) for t in ts]
    name = f"{lv.id} · {role} {lv.value(last):,.2f}"
    fig.add_trace(go.Scatter(x=xs + xs[::-1], y=top + bot[::-1], fill="toself", fillcolor=ZONE_FILL[role],
                             line={"width": 0}, hoverinfo="skip", legendgroup=lv.id, showlegend=False, name=name),
                  **PRICE_PANE)
    vals = [float(lv.values[t]) for t in ts]
    fig.add_trace(go.Scatter(
        x=xs, y=vals, mode="lines+text", name=name, legendgroup=lv.id, hoverinfo="skip",
        line={"color": color, "width": 1.5, "dash": "dash"},
        text=[""] * (len(ts) - 1) + [lv.id], textposition="middle right", textfont={"color": color, "size": 11},
        cliponaxis=False,
    ), **PRICE_PANE)


def price_figure(sa: SymbolAnalysis, levels: list[Level], signal_bars: list[int], chart_bars: int, k: float,
                 rsi_ob: float, rsi_os: float) -> go.Figure:
    b = sa.bars
    last = sa.last_idx
    start = max(0, last - chart_bars + 1)
    idx = range(0, last + 1)
    x = [_dt(d) for d in b.dates[:last + 1]]

    # Row 1 has two y-axes: volume on the base axis (hidden, drawn underneath) and everything priced on the
    # overlaying axis. Only the candles show a tooltip (OHLC); every other trace skips hover.
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03,
                        specs=[[{"secondary_y": True}], [{}]])

    # volume, coloured by the bar's direction
    vol = np.nan_to_num(b.volume[:last + 1].astype(float), nan=0.0)
    fig.add_trace(go.Bar(x=x, y=vol, name="Volume", hoverinfo="skip", marker_line_width=0,
                         marker_color=[VOL_UP if c >= o else VOL_DOWN
                                       for o, c in zip(b.open[:last + 1], b.close[:last + 1], strict=True)]),
                  row=1, col=1, secondary_y=False)

    # candles (a provisional last bar is drawn separately and faded)
    done = last if sa.provisional else last + 1
    fig.add_trace(go.Candlestick(
        x=x[:done], open=b.open[:done], high=b.high[:done], low=b.low[:done], close=b.close[:done], name="Price",
        showlegend=False, **CANDLE,
    ), **PRICE_PANE)
    if sa.provisional:
        fig.add_trace(go.Candlestick(
            x=x[last:], open=b.open[last:last + 1], high=b.high[last:last + 1], low=b.low[last:last + 1],
            close=b.close[last:last + 1], name="Provisional bar", opacity=0.45, **CANDLE,
        ), **PRICE_PANE)

    # level zones + lines (each level toggles as one legend group)
    for lv in levels:
        _add_level(fig, sa, lv, k, last)

    # SMAs
    for name, arr in (("sma50", b.sma50), ("sma200", b.sma200)):
        ts = _finite(arr, idx)
        if len(ts) > 1:
            fig.add_trace(go.Scatter(x=[x[t] for t in ts], y=arr[ts], mode="lines", name=name.upper(),
                                     line={"color": SMA_COLORS[name], "width": 2}, hoverinfo="skip"), **PRICE_PANE)

    # pivots (those used for the trend state are emphasised)
    tech = sa.techs[0]
    used = {(p.kind, p.idx) for p in sa.trend.pivots_used}
    for kind, pivots, symbol in (("high", tech.highs, "triangle-down"), ("low", tech.lows, "triangle-up")):
        ps = [p for p in pivots if p.idx <= last]
        if not ps:
            continue
        fig.add_trace(go.Scatter(
            x=[_dt(p.date) for p in ps], y=[p.price for p in ps], mode="markers", name=f"Pivot {kind}s",
            legendgroup="pivots", hoverinfo="skip",
            marker={"symbol": symbol, "size": [11 if (p.kind, p.idx) in used else 8 for p in ps],
                    "color": [PIVOT_USED if (p.kind, p.idx) in used else PIVOT_MUTED for p in ps]},
        ), **PRICE_PANE)

    # signal bars
    for t in sorted(set(signal_bars)):
        if 0 <= t <= last:
            fig.add_vrect(x0=x[t] - HALF_DAY, x1=x[t] + HALF_DAY, fillcolor=SIGNAL_FILL, line_width=0, layer="below",
                          row=1, col=1)

    # RSI pane
    ts = _finite(b.rsi, idx)
    fig.add_trace(go.Scatter(x=[x[t] for t in ts], y=b.rsi[ts], mode="lines", name="RSI",
                             line={"color": RSI_COLOR, "width": 2}, showlegend=False, hoverinfo="skip"),
                  row=2, col=1)
    for lvl in (rsi_ob, rsi_os):
        fig.add_hline(y=lvl, line={"dash": "dot", "width": 1, "color": "rgba(128,128,128,0.8)"}, row=2, col=1)

    lo, hi = _visible_range(sa, levels, start, last, k)
    fig.update_layout(
        height=HEIGHT, hovermode="x unified", dragmode="zoom", margin={"l": 56, "r": 64, "t": 36, "b": 8},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "xanchor": "left", "x": 0},
        xaxis_rangeslider_visible=False,
    )
    breaks = [{"bounds": ["sat", "mon"]}]
    if missing := missing_sessions(b.dates[:last + 1]):
        breaks.append({"values": missing})
    fig.update_xaxes(rangebreaks=breaks, range=[x[start] - HALF_DAY, x[last] + HALF_DAY])
    # tickmode auto: an overlaying axis otherwise syncs its ticks to the hidden volume axis (odd price ticks)
    fig.update_yaxes(range=[lo, hi], tickformat=",.2f", side="left", showgrid=True, tickmode="auto", **PRICE_PANE)
    vmax = float(vol[start:last + 1].max()) if last >= start else 0.0
    fig.update_yaxes(range=[0, (vmax or 1.0) / VOLUME_SHARE], visible=False, showgrid=False, fixedrange=True,
                     row=1, col=1, secondary_y=False)
    fig.update_yaxes(range=[0, 100], fixedrange=True, tickvals=[rsi_os, rsi_ob], title_text="RSI",
                     row=2, col=1)
    return fig
