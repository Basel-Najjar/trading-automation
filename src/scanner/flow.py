"""Relative strength and flow alignment (§6.5).

Ratio series (sector/SPY, name/sector) use the ratio of closes for highs, lows and closes (RS-4 / OQ-8);
ATR on a ratio is therefore Wilder's average of |Δratio|. Flow agreement per scan direction comes from
settings.flow (decision B2): `mixed`/`n/a` components are neutral; countertrend if opposing components
outnumber agreeing ones; with_flow if none oppose and at least one agrees; otherwise mixed.
"""

from __future__ import annotations

import pandas as pd

from scanner.indicators import atr_wilder, find_pivots
from scanner.models import NA, TrendResult
from scanner.trend import classify_trend

RETURN_PERIODS = {"1m": 21, "3m": 63, "6m": 126}


def ratio_trend(num: pd.Series, den: pd.Series, settings) -> TrendResult | None:
    """Trend state of num/den on their common dates. None if too little overlap."""
    joined = pd.concat([num.rename("n"), den.rename("d")], axis=1, join="inner").dropna()
    if len(joined) < 4 * settings.pivots.pivot_window + settings.indicators.atr_period:
        return None
    r = (joined["n"] / joined["d"]).to_numpy(dtype=float)
    dates = [d.date() for d in joined.index]
    atr = atr_wilder(r, r, r, settings.indicators.atr_period)
    highs, lows = find_pivots(r, r, dates, settings.pivots.pivot_window)
    return classify_trend(r, atr, highs, lows, len(r) - 1, settings)


def period_returns(close: pd.Series) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    c = close.dropna()
    for k, n in RETURN_PERIODS.items():
        out[k] = float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None
    return out


def classify_flow(direction: str, components: list[str], settings) -> str:
    agree = set(settings.flow.agree.get(direction, []))
    oppose = set(settings.flow.oppose.get(direction, []))
    a = sum(1 for c in components if c in agree)
    o = sum(1 for c in components if c in oppose)
    if o > a:
        return "countertrend"
    if o == 0 and a >= 1:
        return "with_flow"
    return "mixed"


def state_of(tr: TrendResult | None) -> str:
    return tr.state if tr is not None else NA
