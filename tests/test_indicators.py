"""T-1: indicator and pivot primitives against independent reference implementations."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from conftest import random_frame
from scanner.indicators import atr_wilder, find_pivots, rel_volume, rsi_wilder, sma


def _ref_rma(x, n):
    out = [np.nan] * len(x)
    acc = None
    for t in range(len(x)):
        if t == n - 1:
            acc = sum(x[:n]) / n
        elif t >= n:
            acc = (acc * (n - 1) + x[t]) / n
        if acc is not None:
            out[t] = acc
    return np.array(out)


def _ref_atr(h, low, c, n):
    tr = [h[0] - low[0]] + [max(h[t] - low[t], abs(h[t] - c[t - 1]), abs(low[t] - c[t - 1])) for t in range(1, len(h))]
    return _ref_rma(tr, n)


def _ref_rsi(c, n):
    out = [np.nan] * len(c)
    gains = [max(c[t] - c[t - 1], 0) for t in range(1, len(c))]
    losses = [max(c[t - 1] - c[t], 0) for t in range(1, len(c))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al)
    for t in range(n + 1, len(c)):
        ag = (ag * (n - 1) + gains[t - 1]) / n
        al = (al * (n - 1) + losses[t - 1]) / n
        out[t] = 100 - 100 / (1 + ag / al)
    return np.array(out)


def test_sma_hand_values():
    x = np.array([1, 2, 3, 4, 5, 6], dtype=float)
    out = sma(x, 3)
    assert np.isnan(out[:2]).all()
    np.testing.assert_allclose(out[2:], [2, 3, 4, 5])
    assert np.isnan(sma(x, 10)).all()


def test_atr_matches_reference():
    df = random_frame(300, seed=11)
    h, lo, c = (df[k].to_numpy() for k in ("high", "low", "close"))
    np.testing.assert_allclose(atr_wilder(h, lo, c, 14), _ref_atr(h, lo, c, 14), rtol=1e-12, equal_nan=True)


def test_atr_hand_value():
    # constant range 2, no gaps -> ATR = 2
    h = np.array([11.0] * 20)
    lo = np.array([9.0] * 20)
    c = np.array([10.0] * 20)
    assert atr_wilder(h, lo, c, 14)[-1] == 2.0
    assert np.isnan(atr_wilder(h, lo, c, 14)[12])


def test_rsi_matches_reference():
    df = random_frame(300, seed=12)
    c = df["close"].to_numpy()
    np.testing.assert_allclose(rsi_wilder(c, 14), _ref_rsi(c, 14), rtol=1e-10, equal_nan=True)


def test_rsi_edges():
    up = np.arange(1, 40, dtype=float)
    assert rsi_wilder(up, 14)[-1] == 100.0
    flat = np.full(40, 5.0)
    assert rsi_wilder(flat, 14)[-1] == 50.0
    assert np.isnan(rsi_wilder(up, 14)[13]) and not np.isnan(rsi_wilder(up, 14)[14])


def test_rel_volume_uses_prior_bars():
    v = np.array([10.0] * 20 + [30.0])
    assert rel_volume(v, 20)[-1] == 3.0


def _dates(n):
    return [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]


def test_pivots_basic_and_confirmation():
    high = np.array([1, 2, 3, 5, 3, 2, 1, 2, 3, 4, 3], dtype=float)
    low = high - 0.5
    hs, ls = find_pivots(high, low, _dates(len(high)), 2)
    assert [(p.idx, p.price, p.confirm_idx) for p in hs] == [(3, 5.0, 5)]
    assert [p.idx for p in ls] == [6]


def test_pivot_ties_first_bar_of_flat_top_wins():
    high = np.array([1, 2, 5, 5, 2, 1, 0], dtype=float)
    hs, _ = find_pivots(high, high - 1, _dates(7), 2)
    assert [p.idx for p in hs] == [2]


def test_pivot_nan_window_produces_no_pivot():
    high = np.array([1, 2, 5, np.nan, 2, 1, 0], dtype=float)
    hs, _ = find_pivots(high, high - 1, _dates(7), 2)
    assert hs == []


def test_pivot_needs_window_after_and_last_usable():
    high = np.array([1, 2, 3, 2, 1, 0, 1, 2, 9, 2, 1], dtype=float)
    hs, _ = find_pivots(high, high - 1, _dates(11), 2)
    assert [p.idx for p in hs] == [2, 8]
    hs, _ = find_pivots(high, high - 1, _dates(11), 2, last_usable=9)
    assert [p.idx for p in hs] == [2]  # pivot at 8 needs bar 10 to confirm


def test_pivots_no_look_ahead_perturbation():
    """Randomising bars after t never changes pivots confirmed by t."""
    df = random_frame(400, seed=21)
    h, lo = df["high"].to_numpy(), df["low"].to_numpy()
    d = [x.date() for x in df.index]
    rng = np.random.default_rng(0)
    for t in (100, 250, 333):
        base_h, base_l = find_pivots(h, lo, d, 5)
        h2, l2 = h.copy(), lo.copy()
        h2[t + 1:] = rng.uniform(1, 500, len(h) - t - 1)
        l2[t + 1:] = h2[t + 1:] - rng.uniform(0, 5, len(h) - t - 1)
        new_h, new_l = find_pivots(h2, l2, d, 5)
        assert [p for p in base_h if p.confirm_idx <= t] == [p for p in new_h if p.confirm_idx <= t]
        assert [p for p in base_l if p.confirm_idx <= t] == [p for p in new_l if p.confirm_idx <= t]


def _pivots_reference(high, low, dates, w, last_usable=None):
    """The original loop implementation, kept as the oracle for the vectorised one."""
    n = len(high)
    last = n - 1 if last_usable is None else min(last_usable, n - 1)
    highs, lows = [], []
    for i in range(w, last - w + 1):
        lh, rh = high[i - w:i], high[i + 1:i + w + 1]
        if not (np.isnan(high[i]) or np.isnan(lh).any() or np.isnan(rh).any()):
            if high[i] > lh.max() and high[i] >= rh.max():
                highs.append((i, float(high[i]), i + w))
        ll, rl = low[i - w:i], low[i + 1:i + w + 1]
        if not (np.isnan(low[i]) or np.isnan(ll).any() or np.isnan(rl).any()):
            if low[i] < ll.min() and low[i] <= rl.min():
                lows.append((i, float(low[i]), i + w))
    return highs, lows


def test_find_pivots_matches_reference_loop():
    rng = np.random.default_rng(3)
    for trial in range(60):
        n = int(rng.integers(1, 120))
        high = np.round(rng.normal(100, 2, n), 0)  # rounded: many flat tops and bottoms
        low = high - np.round(rng.uniform(0, 3, n), 0)
        if trial % 3 == 0 and n > 4:
            high[rng.integers(0, n, 3)] = np.nan
            low[rng.integers(0, n, 3)] = np.nan
        dates = [date(2026, 1, 1)] * n
        w = int(rng.integers(1, 7))
        last = None if trial % 2 else int(rng.integers(0, n))
        h, lo = find_pivots(high, low, dates, w, last_usable=last)
        exp_h, exp_l = _pivots_reference(high, low, dates, w, last)
        assert [(p.idx, p.price, p.confirm_idx) for p in h] == exp_h
        assert [(p.idx, p.price, p.confirm_idx) for p in lo] == exp_l
