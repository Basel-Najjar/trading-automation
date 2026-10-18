"""Relative Rotation Graph: RS-Ratio (x) and RS-Momentum (y) of each member against a benchmark.

The published JdK formulas are proprietary, so this is the standard z-score approximation, centred on 100:

    rs       = 100 · member / benchmark
    rs_ratio = 100 + (rs − SMA(rs, N)) / STD(rs, N)
    roc      = 100 · Δrs_ratio / rs_ratio[t−1]
    rs_mom   = 100 + (roc − SMA(roc, N)) / STD(roc, N)

Quadrants: Leading (x ≥ 100, y ≥ 100), Weakening (x ≥ 100, y < 100), Lagging (both < 100), Improving (x < 100,
y ≥ 100). Rotation normally runs clockwise: Leading → Weakening → Lagging → Improving.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

QUADRANTS = ("Leading", "Weakening", "Lagging", "Improving")


def _z(x: pd.Series, n: int) -> pd.Series:
    sd = x.rolling(n).std(ddof=0)
    return (x - x.rolling(n).mean()) / sd.where(sd > 0)


def rrg_series(member: pd.Series, bench: pd.Series, window: int) -> pd.DataFrame:
    """Columns rs_ratio, rs_mom on the common dates (rows before both are defined are dropped)."""
    j = pd.concat([member.rename("m"), bench.rename("b")], axis=1, join="inner").dropna()
    rs = 100 * j["m"] / j["b"]
    ratio = 100 + _z(rs, window)
    roc = 100 * ratio.pct_change(fill_method=None)
    mom = 100 + _z(roc, window)
    return pd.DataFrame({"rs_ratio": ratio, "rs_mom": mom}).dropna()


def quadrant(x: float, y: float) -> str:
    if x >= 100:
        return "Leading" if y >= 100 else "Weakening"
    return "Improving" if y >= 100 else "Lagging"


@dataclass
class RrgResult:
    benchmark: str
    tails: dict[str, pd.DataFrame]  # member → last `tail`+1 rows of rs_ratio/rs_mom
    table: list[dict]
    skipped: list[tuple[str, str]]


def compute_rrg(closes: pd.DataFrame, benchmark: str, members: list[str], window: int, tail: int) -> RrgResult:
    """RRG of `members` (columns of `closes`) against `closes[benchmark]`."""
    tails: dict[str, pd.DataFrame] = {}
    rows: list[dict] = []
    skipped: list[tuple[str, str]] = []
    if benchmark not in closes:
        return RrgResult(benchmark, {}, [], [(m, f"benchmark {benchmark} has no prices") for m in members])
    for m in members:
        if m not in closes:
            skipped.append((m, "no prices"))
            continue
        df = rrg_series(closes[m], closes[benchmark], window)
        if len(df) < 2:
            skipped.append((m, f"too little overlap with {benchmark} for a {window}-bar window"))
            continue
        t = df.tail(tail + 1)
        tails[m] = t
        x, y = float(t["rs_ratio"].iloc[-1]), float(t["rs_mom"].iloc[-1])
        x0, y0 = float(t["rs_ratio"].iloc[-2]), float(t["rs_mom"].iloc[-2])
        first = t.iloc[0]
        dx, dy = x - x0, y - y0
        rows.append({
            "Series": m, "Quadrant": quadrant(x, y),
            "Was": quadrant(float(first["rs_ratio"]), float(first["rs_mom"])),
            "RS-Ratio": x, "RS-Momentum": y,
            # 0° = straight right (gaining relative strength), 90° = straight up (gaining momentum)
            "Heading °": (math.degrees(math.atan2(dy, dx)) + 360) % 360,
            "Speed": math.hypot(dx, dy), "Distance": math.hypot(x - 100, y - 100),
        })
    order = {q: i for i, q in enumerate(QUADRANTS)}
    rows.sort(key=lambda r: (order[r["Quadrant"]], -r["Distance"]))
    return RrgResult(benchmark, tails, rows, skipped)


def axis_range(tails: dict[str, pd.DataFrame], pad: float = 0.5) -> tuple[list[float], list[float]]:
    """Symmetric ranges around 100 that hold every tail (so the quadrants stay square)."""
    if not tails:
        return [98, 102], [98, 102]
    xs = np.concatenate([t["rs_ratio"].to_numpy() for t in tails.values()])
    ys = np.concatenate([t["rs_mom"].to_numpy() for t in tails.values()])
    dx = max(float(np.nanmax(np.abs(xs - 100))), 1.0) + pad
    dy = max(float(np.nanmax(np.abs(ys - 100))), 1.0) + pad
    return [100 - dx, 100 + dx], [100 - dy, 100 + dy]
