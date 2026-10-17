"""Interactive app chart: same content as the SVG chart, full history with the initial view on `chart_bars`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from scanner.charts import chart_levels
from scanner.pipeline import scan
from scanner.plotly_chart import Overlay, missing_sessions, price_figure

D = date(2026, 9, 30)


@pytest.fixture
def sa(cfg, market_frames):
    res = scan(cfg, market_frames, D)
    return res.analyses[sorted(res.analyses)[0]]


def _fig(sa, levels=None, signal_bars=()):
    return price_figure(sa, chart_levels(sa) if levels is None else levels, list(signal_bars), 150, 0.25, 70, 30)


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def test_initial_view_is_last_chart_bars(sa):
    fig = _fig(sa)
    x0, x1 = fig.layout.xaxis.range
    b, last = sa.bars, sa.last_idx
    assert _midnight(b.dates[last - 149]) - timedelta(hours=12) == x0
    assert x1 > _midnight(b.dates[last])
    candles = [t for t in fig.data if t.type == "candlestick"]
    assert len(candles) == 1 and len(candles[0].x) == last + 1  # the whole window is plotted, so you can pan back


def test_provisional_bar_is_separate_trace(sa):
    fig = _fig(replace(sa, provisional=True))
    candles = [t for t in fig.data if t.type == "candlestick"]
    assert [c.name for c in candles] == ["Price", "Provisional bar"]
    assert len(candles[0].x) == sa.last_idx and len(candles[1].x) == 1 and candles[1].opacity < 1


def test_each_level_is_one_legend_group(sa):
    levels = chart_levels(sa)
    assert levels
    fig = _fig(sa, levels=levels)
    groups = {t.legendgroup for t in fig.data if t.legendgroup and t.legendgroup != "pivots"}
    assert groups == {lv.id for lv in levels}
    for lv in levels:
        traces = [t for t in fig.data if t.legendgroup == lv.id]
        assert [t.fill for t in traces] == ["toself", None]  # zone polygon + level line


def test_signal_bars_shaded(sa):
    fig = _fig(sa, signal_bars=[sa.last_idx, sa.last_idx - 1, sa.last_idx])
    assert sum(1 for s in fig.layout.shapes if s.type == "rect") == 2


def test_nan_sma_is_skipped(sa):
    b = replace(sa.bars, sma200=np.full(len(sa.bars), np.nan))
    names = {t.name for t in _fig(replace(sa, bars=b)).data}
    assert "SMA200" not in names and "SMA50" in names


def test_only_candles_have_a_tooltip(sa):
    fig = _fig(replace(sa, provisional=True), signal_bars=[sa.last_idx])
    for t in fig.data:
        if t.type == "candlestick":
            assert t.hoverinfo in (None, "all")
        else:
            assert t.hoverinfo == "skip", t.name


def test_volume_overlay_under_price(sa):
    fig = _fig(sa)
    vol = next(t for t in fig.data if t.name == "Volume")
    price = next(t for t in fig.data if t.type == "candlestick")
    assert vol.type == "bar" and len(vol.y) == sa.last_idx + 1
    assert vol.yaxis == "y" and price.yaxis == "y2"  # volume on the base axis is drawn beneath the price axis
    assert fig.layout.yaxis2.overlaying == "y" and fig.layout.yaxis.visible is False
    visible_max = float(np.nanmax(sa.bars.volume[sa.last_idx - 149:sa.last_idx + 1]))
    assert fig.layout.yaxis.range[1] == pytest.approx(visible_max / 0.2)
    up = sa.bars.close[sa.last_idx] >= sa.bars.open[sa.last_idx]
    assert ("12,163,12" in vol.marker.color[-1]) == up


def test_missing_sessions_are_weekdays_without_bars():
    dates = [date(2026, 11, 25), date(2026, 11, 27), date(2026, 11, 30)]  # Thanksgiving (Thu 26th) has no bar
    assert missing_sessions(dates) == ["2026-11-26"]
    assert missing_sessions([]) == []


def test_legend_starts_collapsed_and_lists_level_values(sa):
    levels = chart_levels(sa)
    fig = _fig(sa, levels=levels)
    assert fig.layout.showlegend is False
    button = fig.layout.updatemenus[0].buttons[0]
    assert button.args == ({"showlegend": True},) and button.args2 == ({"showlegend": False},)
    for lv in levels:
        line = next(t for t in fig.data if t.legendgroup == lv.id and t.fill is None)
        assert line.mode == "lines" and not line.text  # no id label at the line's end
        assert f"{lv.value(sa.last_idx):,.2f}" in line.name


def test_highlight_emphasises_one_level_and_fades_the_rest(sa):
    levels = chart_levels(sa)
    assert len(levels) >= 2
    fig = price_figure(sa, levels, [], 150, 0.25, 70, 30, highlight={levels[0].id})
    lines = {t.legendgroup: t for t in fig.data if t.type == "scatter" and t.mode == "lines" and t.legendgroup}
    hi = lines[levels[0].id]
    assert hi.line.dash == "solid" and hi.opacity == 1.0
    assert all(lines[lv.id].opacity < 1.0 for lv in levels[1:] if lv.id in lines)
    tags = [a for a in fig.layout.annotations if a.yref == "y2"]
    assert len(tags) == 1 and f"{levels[0].value(sa.last_idx):,.2f}" in tags[0].text


def test_no_highlight_draws_levels_alike(sa):
    fig = _fig(sa)
    lines = [t for t in fig.data if t.type == "scatter" and t.mode == "lines" and t.legendgroup]
    assert lines and all(t.opacity in (None, 1.0) and t.line.dash == "dash" for t in lines)
    assert not [a for a in fig.layout.annotations if a.yref == "y2"]


def test_invalidation_line_on_the_price_axis(sa):
    fig = price_figure(sa, chart_levels(sa), [], 150, 0.25, 70, 30, invalidation=123.45)
    shapes = [s for s in fig.layout.shapes if s.yref == "y2"]
    assert len(shapes) == 1 and shapes[0].y0 == shapes[0].y1 == 123.45
    assert any("Invalidation 123.45" in a.text for a in fig.layout.annotations)


def test_overlays_get_their_own_right_axes(sa):
    n = len(sa.bars.dates)
    a = np.linspace(100, 110, n)
    r = np.full(n, 0.5)
    r[:5] = np.nan  # gaps in the other series are skipped, not drawn as zeros
    fig = price_figure(sa, chart_levels(sa), [], 150, 0.25, 70, 30,
                       overlays=[Overlay("XLK", a, "#111111"), Overlay("XLK/SPY", r, "#222222", ".4f", "dot")])
    traces = {t.name: t for t in fig.data if t.name in ("XLK", "XLK/SPY")}
    assert traces["XLK"].yaxis == "y4" and traces["XLK/SPY"].yaxis == "y5"
    assert len(traces["XLK/SPY"].y) == sa.last_idx + 1 - 5
    y4, y5 = fig.layout.yaxis4, fig.layout.yaxis5
    assert y4.overlaying == y5.overlaying == "y" and y4.side == y5.side == "right"
    assert y4.autoshift and y5.autoshift  # visible overlay axes stack outward instead of overlapping
    lo, hi = y4.range
    assert lo < a[sa.last_idx - 149] and hi > a[sa.last_idx]  # fits the visible window
    # hidden until toggled: one button per overlay, each its own menu so several can be on at once
    assert traces["XLK"].visible is False and y4.visible is False
    menus = {m.buttons[0].label: m.buttons[0] for m in fig.layout.updatemenus}
    assert set(menus) == {"Legend", "XLK", "XLK/SPY"}
    on, off = menus["XLK/SPY"].args, menus["XLK/SPY"].args2
    idx = list(fig.data).index(traces["XLK/SPY"])
    assert on == ({"visible": True}, {"yaxis5.visible": True}, (idx,))
    assert off == ({"visible": False}, {"yaxis5.visible": False}, (idx,))
    pads = [m.pad.l for m in fig.layout.updatemenus]
    assert pads == sorted(pads) and len(set(pads)) == len(pads)  # side by side, not stacked


def test_no_overlays_adds_no_axes(sa):
    fig = _fig(sa)
    assert "yaxis4" not in fig.layout.to_plotly_json()
