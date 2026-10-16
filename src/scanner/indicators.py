"""Indicators (SMA, Wilder ATR/RSI, relative volume) and pivot detection. All causal: value at t uses bars ≤ t."""

from __future__ import annotations

from datetime import date

import numpy as np

from scanner.models import Bars, Pivot


def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rma(x: np.ndarray, n: int, start: int = 0) -> np.ndarray:
    """Wilder's moving average, seeded with the simple mean of the first n values from `start`."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) - start < n:
        return out
    seed = start + n - 1
    out[seed] = np.mean(x[start:seed + 1])
    for t in range(seed + 1, len(x)):
        out[t] = (out[t - 1] * (n - 1) + x[t]) / n
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high, low, close = (np.asarray(a, dtype=float) for a in (high, low, close))
    prev = np.roll(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return tr


def atr_wilder(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """ATR as TradingView's ta.atr: RMA of true range (first TR = high − low)."""
    return rma(true_range(high, low, close), n)


def rsi_wilder(close: np.ndarray, n: int = 14) -> np.ndarray:
    """RSI as TradingView's ta.rsi: RMA of gains/losses, seeded with the mean of the first n changes."""
    close = np.asarray(close, dtype=float)
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    d = np.diff(close, prepend=np.nan)
    gain, loss = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    ag, al = rma(gain, n, start=1), rma(loss, n, start=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        out = 100 - 100 / (1 + rs)
    out = np.where((al == 0) & (ag > 0), 100.0, out)
    out = np.where((al == 0) & (ag == 0), 50.0, out)
    out[: n] = np.nan
    return out


def rel_volume(volume: np.ndarray, n: int = 20) -> np.ndarray:
    """Volume ÷ average volume of the prior n bars (informational only)."""
    v = np.asarray(volume, dtype=float)
    avg = sma(v, n)
    out = np.full(len(v), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[1:] = v[1:] / avg[:-1]
    return out


def find_pivots(high: np.ndarray, low: np.ndarray, dates: list[date], window: int,
                last_usable: int | None = None) -> tuple[list[Pivot], list[Pivot]]:
    """Pivot highs/lows with `window` bars on each side.

    A pivot high at i: high[i] > every high in the `window` bars before it and ≥ every high in the
    `window` bars after it (first bar of a flat top wins). Mirror for lows. A pivot is confirmed at
    i + window and only bars ≤ `last_usable` may confirm (no look-ahead; excludes provisional bars).
    Windows containing NaN produce no pivot.
    """
    high, low = np.asarray(high, dtype=float), np.asarray(low, dtype=float)
    n = len(high)
    last = n - 1 if last_usable is None else min(last_usable, n - 1)
    highs: list[Pivot] = []
    lows: list[Pivot] = []
    w = window
    for i in range(w, last - w + 1):
        lh, rh = high[i - w:i], high[i + 1:i + w + 1]
        if not (np.isnan(high[i]) or np.isnan(lh).any() or np.isnan(rh).any()):
            if high[i] > lh.max() and high[i] >= rh.max():
                highs.append(Pivot("high", i, dates[i], float(high[i]), i + w))
        ll, rl = low[i - w:i], low[i + 1:i + w + 1]
        if not (np.isnan(low[i]) or np.isnan(ll).any() or np.isnan(rl).any()):
            if low[i] < ll.min() and low[i] <= rl.min():
                lows.append(Pivot("low", i, dates[i], float(low[i]), i + w))
    return highs, lows


def compute_bars(df, settings) -> Bars:
    """Arrays for one symbol's analysis window (df: normalized OHLCV frame, oldest first)."""
    o, h, lo, c, v = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    ind = settings.indicators
    return Bars(
        dates=[d.date() for d in df.index], open=o, high=h, low=lo, close=c, volume=v,
        atr=atr_wilder(h, lo, c, ind.atr_period), rsi=rsi_wilder(c, ind.rsi_period),
        sma50=sma(c, 50), sma200=sma(c, 200), relvol=rel_volume(v, ind.relvol_period),
    )
