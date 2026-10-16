"""Static inline-SVG charts (no plotting dependency). Colours come from CSS classes so light/dark themes work."""

from __future__ import annotations

from html import escape

import numpy as np

from scanner.models import Level, SymbolAnalysis

W, PRICE_H, RSI_H, GAP, PAD_L, PAD_R, PAD_T, AXIS_H = 900, 330, 90, 14, 6, 64, 8, 18


def _fmt(x: float) -> str:
    return f"{x:.1f}"


def _price_label(p: float) -> str:
    return f"{p:,.2f}" if p < 1000 else f"{p:,.0f}"


def chart_levels(sa: SymbolAnalysis, extra: list[Level] | None = None) -> list[Level]:
    """Levels worth drawing: signal levels, nearest per side, range boundaries (deduplicated)."""
    out: dict[str, Level] = {}
    for lv in extra or []:
        out.setdefault(lv.id, lv)
    for r in sa.nearest_support[:2] + sa.nearest_resistance[:2]:
        out.setdefault(r["level"].id, r["level"])
    for lv in (sa.trend.range_high, sa.trend.range_low):
        if lv is not None:
            out.setdefault(lv.id, lv)
    return list(out.values())


def svg_chart(sa: SymbolAnalysis, levels: list[Level], signal_bars: list[int], chart_bars: int, k: float,
              rsi_ob: float, rsi_os: float) -> str:
    b = sa.bars
    last = sa.last_idx
    start = max(0, last - chart_bars + 1)
    idx = np.arange(start, last + 1)
    n = len(idx)
    plot_w = W - PAD_L - PAD_R
    step = plot_w / max(n, 1)
    cw = max(step * 0.62, 1.0)

    lo = float(np.nanmin(b.low[start:last + 1]))
    hi = float(np.nanmax(b.high[start:last + 1]))
    span = hi - lo or 1.0
    vis_lo, vis_hi = lo - 0.12 * span, hi + 0.12 * span
    drawn = []
    for lv in levels:
        v = lv.value(last)
        if not np.isnan(v) and vis_lo <= v <= vis_hi:
            drawn.append(lv)
            z = lv.zone(last, b.atr, k)
            lo, hi = min(lo, z[0]), max(hi, z[1])
    for arr in (b.sma50, b.sma200):
        seg = arr[start:last + 1]
        if np.isfinite(seg).any():
            m = seg[np.isfinite(seg)]
            lo, hi = min(lo, float(np.clip(m.min(), vis_lo, vis_hi))), max(hi, float(np.clip(m.max(), vis_lo, vis_hi)))
    pad = (hi - lo) * 0.04 or 1.0
    lo, hi = lo - pad, hi + pad

    def x(t: int) -> float:
        return PAD_L + (t - start + 0.5) * step

    def y(p: float) -> float:
        return PAD_T + (hi - p) / (hi - lo) * PRICE_H

    rsi_top = PAD_T + PRICE_H + GAP

    def yr(r: float) -> float:
        return rsi_top + (100 - r) / 100 * RSI_H

    total_h = rsi_top + RSI_H + AXIS_H
    parts = [f'<svg class="chart" viewBox="0 0 {W} {total_h}" role="img" preserveAspectRatio="xMidYMid meet" '
             f'xmlns="http://www.w3.org/2000/svg"><title>{escape(sa.symbol)} daily chart</title>',
             f'<rect class="pane" x="{PAD_L}" y="{PAD_T}" width="{plot_w}" height="{PRICE_H}"/>',
             f'<rect class="pane" x="{PAD_L}" y="{rsi_top}" width="{plot_w}" height="{RSI_H}"/>']

    # grid + y labels
    for p in np.linspace(lo, hi, 6)[1:-1]:
        parts.append(f'<line class="grid" x1="{PAD_L}" x2="{PAD_L + plot_w}" y1="{_fmt(y(p))}" y2="{_fmt(y(p))}"/>')
        parts.append(f'<text class="axis" x="{PAD_L + plot_w + 4}" y="{_fmt(y(p) + 4)}">{_price_label(p)}</text>')
    # x labels
    every = max(n // 6, 1)
    for j in range(0, n, every):
        t = int(idx[j])
        parts.append(f'<text class="axis" x="{_fmt(x(t))}" y="{total_h - 4}" text-anchor="middle">'
                     f'{b.dates[t].strftime("%b %d")}</text>')

    # signal bar highlights
    for t in sorted(set(signal_bars)):
        if start <= t <= last:
            parts.append(f'<rect class="sigbar" x="{_fmt(x(t) - step / 2)}" y="{PAD_T}" width="{_fmt(step)}" '
                         f'height="{PRICE_H}"><title>signal bar {b.dates[t]}</title></rect>')

    # zones + level lines
    for lv in drawn:
        first = start
        if lv.type == "diagonal" and lv.anchors:
            a0 = next((t for t in range(len(b.dates)) if b.dates[t] >= lv.anchors[0][0]), start)
            first = max(start, a0)
        ts = [t for t in range(first, last + 1) if np.isfinite(lv.values[t]) and np.isfinite(b.atr[t])]
        if len(ts) < 2:
            continue
        top = [f"{_fmt(x(t))},{_fmt(y(lv.values[t] + k * b.atr[t]))}" for t in ts]
        bot = [f"{_fmt(x(t))},{_fmt(y(lv.values[t] - k * b.atr[t]))}" for t in reversed(ts)]
        role = lv.role(last, float(b.close[last]))
        cls = f"zone {role} {lv.type}"
        parts.append(f'<polygon class="{cls}" points="{" ".join(top + bot)}"><title>{escape(lv.label)} '
                     f'{escape(role)} {lv.value(last):.2f}</title></polygon>')
        line = " ".join(f"{_fmt(x(t))},{_fmt(y(lv.values[t]))}" for t in ts)
        parts.append(f'<polyline class="lvl {role} {lv.type}" points="{line}"/>')
        parts.append(f'<text class="lvltag {role}" x="{PAD_L + plot_w + 4}" y="{_fmt(y(lv.value(last)) + 4)}">'
                     f'{escape(lv.id)}</text>')

    # SMAs
    for name, arr in (("sma50", b.sma50), ("sma200", b.sma200)):
        pts = [f"{_fmt(x(t))},{_fmt(y(arr[t]))}" for t in idx if np.isfinite(arr[t]) and lo <= arr[t] <= hi]
        if len(pts) > 1:
            parts.append(f'<polyline class="{name}" points="{" ".join(pts)}"><title>{name.upper()}</title></polyline>')

    # candles
    for t in idx:
        o, h, l_, c = b.open[t], b.high[t], b.low[t], b.close[t]
        if not np.isfinite([o, h, l_, c]).all():
            continue
        cls = "up" if c >= o else "down"
        if sa.provisional and t == last:
            cls += " prov"
        xs = _fmt(x(t))
        parts.append(f'<line class="wick {cls}" x1="{xs}" x2="{xs}" y1="{_fmt(y(h))}" y2="{_fmt(y(l_))}"/>')
        top_, bh = y(max(o, c)), max(abs(y(o) - y(c)), 0.8)
        parts.append(f'<rect class="body {cls}" x="{_fmt(x(t) - cw / 2)}" y="{_fmt(top_)}" width="{_fmt(cw)}" '
                     f'height="{_fmt(bh)}"/>')

    # pivots used for structure
    tech = sa.techs[0]
    used = {(p.kind, p.idx) for p in sa.trend.pivots_used}
    for p in tech.highs + tech.lows:
        if start <= p.idx <= last:
            cx = x(p.idx)
            if p.kind == "high":
                cy = y(p.price) - 6
                pts = f"{_fmt(cx - 4)},{_fmt(cy - 5)} {_fmt(cx + 4)},{_fmt(cy - 5)} {_fmt(cx)},{_fmt(cy)}"
            else:
                cy = y(p.price) + 6
                pts = f"{_fmt(cx - 4)},{_fmt(cy + 5)} {_fmt(cx + 4)},{_fmt(cy + 5)} {_fmt(cx)},{_fmt(cy)}"
            cls = "pivot used" if (p.kind, p.idx) in used else "pivot"
            parts.append(f'<polygon class="{cls}" points="{pts}"><title>pivot {p.kind} {p.date} '
                         f'{p.price:.2f}</title></polygon>')

    # RSI pane
    for lvl, cls in ((rsi_ob, "ob"), (rsi_os, "os")):
        parts.append(f'<line class="rsiline {cls}" x1="{PAD_L}" x2="{PAD_L + plot_w}" y1="{_fmt(yr(lvl))}" '
                     f'y2="{_fmt(yr(lvl))}"/>')
        parts.append(f'<text class="axis" x="{PAD_L + plot_w + 4}" y="{_fmt(yr(lvl) + 4)}">{lvl:g}</text>')
    pts = [f"{_fmt(x(t))},{_fmt(yr(b.rsi[t]))}" for t in idx if np.isfinite(b.rsi[t])]
    if len(pts) > 1:
        parts.append(f'<polyline class="rsi" points="{" ".join(pts)}"/>')
    parts.append(f'<text class="axis" x="{PAD_L + 4}" y="{rsi_top + 12}">RSI {b.rsi[last]:.1f}</text>')
    parts.append("</svg>")
    return "".join(parts)
