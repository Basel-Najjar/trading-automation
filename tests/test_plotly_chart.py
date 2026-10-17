"""Interactive app chart: same content as the SVG chart, full history with the initial view on `chart_bars`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from scanner.charts import chart_levels
from scanner.pipeline import scan
from scanner.plotly_chart import missing_sessions, price_figure

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
