"""T-1: trend-state classification (TE-16, decision B1) and range geometry (TE-18)."""

from __future__ import annotations

from datetime import date

import numpy as np

from scanner.config import Settings
from scanner.models import Pivot
from scanner.trend import classify_trend, compare

N = 100
ATR = np.full(N, 2.0)  # tol = 0.5 × 2 = 1.0


def _piv(kind, idx, price):
    return Pivot(kind, idx, date(2026, 1, 1), price, idx + 5)


def _run(highs, lows, close=100.0, s=None):
    return classify_trend(np.full(N, close), ATR, highs, lows, N - 1, s or Settings())


def test_compare_tolerance():
    assert compare(100, 101.5, 1.0) == "higher"
    assert compare(100, 100.9, 1.0) == "equal"
    assert compare(100, 98.9, 1.0) == "lower"


def test_uptrend_and_downtrend():
    up = _run([_piv("high", 20, 105), _piv("high", 60, 110)], [_piv("low", 40, 95), _piv("low", 80, 99)])
    assert up.state == "uptrend" and len(up.pivots_used) == 4
    dn = _run([_piv("high", 20, 110), _piv("high", 60, 105)], [_piv("low", 40, 99), _piv("low", 80, 95)])
    assert dn.state == "downtrend"


def test_mixed_cases():
    hh_ll = _run([_piv("high", 20, 105), _piv("high", 60, 110)], [_piv("low", 40, 99), _piv("low", 80, 95)])
    assert hh_ll.state == "mixed"
    few = _run([_piv("high", 20, 105)], [_piv("low", 40, 95), _piv("low", 80, 99)])
    assert few.state == "mixed" and "too few" in few.reason


def test_b1_small_rise_is_equal_not_higher():
    """+0.6 on highs and lows is within tol (1.0): range, not uptrend (decision B1)."""
    r = _run([_piv("high", 20, 105.0), _piv("high", 60, 105.6)], [_piv("low", 40, 95.0), _piv("low", 80, 95.6)])
    assert r.state == "range"
    assert abs(r.range_high.value(0) - 105.3) < 1e-9 and abs(r.range_low.value(0) - 95.3) < 1e-9
    assert abs(r.range_width_atr - 5.0) < 1e-9
    assert abs(r.range_position - (100 - 95.3) / 10) < 1e-9 and r.range_mid


def test_range_requires_price_inside():
    r = _run([_piv("high", 20, 105.0), _piv("high", 60, 105.2)], [_piv("low", 40, 95.0), _piv("low", 80, 95.2)],
             close=107.0)
    assert r.state == "mixed" and "outside" in r.reason


def test_range_position_not_mid():
    r = _run([_piv("high", 20, 105.0), _piv("high", 60, 105.0)], [_piv("low", 40, 95.0), _piv("low", 80, 95.0)],
             close=96.0)
    assert r.state == "range" and not r.range_mid and abs(r.range_position - 0.1) < 1e-9


def test_unconfirmed_pivots_ignored():
    highs = [_piv("high", 20, 105), _piv("high", 60, 110), Pivot("high", 97, date(2026, 1, 1), 90, 102)]
    r = _run(highs, [_piv("low", 40, 95), _piv("low", 80, 99)])
    assert r.state == "uptrend"
