"""T-1: horizontal clustering, trendlines, manual levels, confluence, nearest levels."""

from __future__ import annotations

from datetime import date

import numpy as np

from conftest import bars_from_ohlc, hlevel
from scanner.config import ManualLevel, Settings
from scanner.indicators import find_pivots
from scanner.levels import (
    cluster_prices,
    confluence_types,
    confluence_zones,
    horizontal_levels,
    manual_levels,
    nearest_levels,
    project_next,
    trendlines,
)
from scanner.models import Level, Pivot


def _p(kind, idx, price):
    return Pivot(kind, idx, date(2026, 1, 1), price, idx + 5)


def test_cluster_prices_greedy_span_limit():
    piv = [_p("low", i, x) for i, x in enumerate([10.0, 10.3, 10.6, 12.0, 12.1])]
    cl = cluster_prices(piv, 0.5)
    assert [[p.price for p in c] for c in cl] == [[10.0, 10.3], [10.6], [12.0, 12.1]]


def _flat_bars(n=120, price=100.0, atr=2.0):
    return bars_from_ohlc([(price, price + 1, price - 1, price)] * n, atr=atr)


def test_horizontal_levels_and_role_reversal():
    s = Settings()
    b = _flat_bars()
    highs = [_p("high", 10, 105.0), _p("high", 30, 105.4), _p("high", 50, 120.0)]
    lows = [_p("low", 20, 95.0), _p("low", 40, 95.2), _p("low", 60, 80.0)]
    lv = horizontal_levels(b, highs, lows, 100, s)
    by = {(x.origin, round(x.value(0), 2)): x for x in lv}
    assert set(by) == {("resistance", 105.2), ("support", 95.1)}  # singles (120, 80) need 2 touches
    res = by[("resistance", 105.2)]
    assert res.touches == 2 and res.last_touch_idx == 30
    assert res.quality == 2 * 0.5 ** ((100 - 30) / 60)
    # price 100 is below 105.2 → acts as resistance (no reversal); a level below price flips to support
    assert res.role(100, 100.0) == "resistance" and res.role(100, 110.0) == "support"


def test_horizontal_ignores_unconfirmed_pivots():
    s = Settings()
    b = _flat_bars()
    highs = [_p("high", 10, 105.0), Pivot("high", 98, date(2026, 1, 1), 105.1, 103)]
    assert horizontal_levels(b, highs, [], 100, s) == []


def _channel(n=120, period=10, slope=0.3, width=8.0):
    """Zig-zag between a support line 100+slope·t (lows at t≡0) and resistance +width (highs at t≡5)."""
    rows = []
    for t in range(n):
        ph = t % period
        base = 100 + slope * t
        frac = ph / (period / 2) if ph <= period / 2 else (period - ph) / (period / 2)
        c = base + frac * width
        o = c - 0.1 if ph < period / 2 else c + 0.1
        hi, lo = c + 0.3, c - 0.3
        if ph == 0:
            lo = base
        if ph == period // 2:
            hi = base + width
        rows.append((o, hi, lo, c))
    return rows


def test_trendlines_find_channel_lines():
    s = Settings().model_copy(update={"pivots": Settings().pivots.model_copy(update={"pivot_window": 2})})
    b = bars_from_ohlc(_channel(), atr=1.0)
    highs, lows = find_pivots(b.high, b.low, b.dates, 2)
    cutoff = len(b) - 1
    lines = trendlines(b, highs, lows, cutoff, s)
    sup = [x for x in lines if x.origin == "support"]
    res = [x for x in lines if x.origin == "resistance"]
    assert len(sup) == 1 and len(res) == 1  # every pivot pair yields the same line → deduplicated
    assert abs(sup[0].slope - 0.3) < 1e-9 and abs(res[0].slope - 0.3) < 1e-9
    assert abs(sup[0].value(cutoff) - (100 + 0.3 * cutoff)) < 1e-9
    assert sup[0].touches == len([p for p in lows if p.confirm_idx <= cutoff])


def test_trendline_invalidated_by_close_through():
    s = Settings().model_copy(update={"pivots": Settings().pivots.model_copy(update={"pivot_window": 2})})
    rows = _channel()
    o, h, lo, c = rows[100]
    rows[100] = (o, h, 90.0, 95.0)  # close far below the support line (≈130) long before the cutoff
    b = bars_from_ohlc(rows, atr=1.0)
    highs, lows = find_pivots(b.high, b.low, b.dates, 2)
    lows = [p for p in lows if p.idx < 95]
    sup = [x for x in trendlines(b, highs, lows, len(b) - 1, s) if x.origin == "support"]
    assert sup == []


def test_trendline_survives_break_on_signal_bar():
    """A close through the line on the latest bar (the current signal) does not invalidate it."""
    s = Settings().model_copy(update={"pivots": Settings().pivots.model_copy(update={"pivot_window": 2})})
    rows = _channel(n=121)
    rows[-1] = (rows[-1][0], rows[-1][1], 100.0, 101.0)
    b = bars_from_ohlc(rows, atr=1.0)
    highs, lows = find_pivots(b.high, b.low, b.dates, 2)
    sup = [x for x in trendlines(b, highs, lows, len(b) - 1, s) if x.origin == "support"]
    assert len(sup) == 1


def test_manual_levels_and_no_look_ahead():
    s = Settings()
    b = _flat_bars(n=60)
    ml = [ManualLevel(type="horizontal", role="support", price=97.5, note="owner"),
          ManualLevel(type="diagonal", role="support", anchors=[(b.dates[5], 90.0), (b.dates[15], 95.0)]),
          ManualLevel(type="diagonal", role="support", anchors=[(b.dates[5], 90.0), (b.dates[55], 95.0)])]
    levels, notes = manual_levels(b, ml, 40, s)
    assert [x.id for x in levels] == ["m_1", "m_2"]
    assert levels[0].manual and levels[0].value(40) == 97.5
    assert abs(levels[1].value(25) - 100.0) < 1e-9  # 90 + 0.5/bar × 20 bars
    assert any("after" in n for n in notes)


def test_confluence_and_nearest():
    b = _flat_bars(n=30, atr=2.0)
    n = len(b)
    a = hlevel(99.0, n, lid="h1")
    sm = Level(id="sma50", type="sma50", values=np.full(n, 99.6), quality=2.0)
    far = hlevel(90.0, n, lid="h2")
    res = hlevel(104.0, n, origin="resistance", lid="h3")
    k = 0.25
    assert confluence_types(a, [a, sm, far, res], n - 1, b, k) == ["horizontal", "sma50"]
    zones = confluence_zones([a, sm, far, res], n - 1, b, k)
    assert len(zones) == 1 and set(zones[0]["sources"]) == {"h1", "sma50"}
    sup, rs = nearest_levels([a, sm, far, res], n - 1, b, 2)
    assert [r["level"].id for r in sup] == ["sma50", "h1"]
    assert [r["level"].id for r in rs] == ["h3"]
    assert abs(rs[0]["dist_atr"] - 2.0) < 1e-9


def test_project_next():
    n = 10
    sm = Level(id="sma50", type="sma50", values=np.arange(n, dtype=float))
    assert project_next(sm, n - 1) == n
    assert project_next(hlevel(5.0, n), n - 1) == 5.0
