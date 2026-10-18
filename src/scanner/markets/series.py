"""Prices for the Markets page: the shared Parquet cache, ratio candles and weekly bars.

Page loads read the cache only; `refresh_prices` is the one network path and goes through the scanner's
`ensure_data` (incremental updates, split-revision detection, completed bars only). A refresh covers the longest
history any resolution needs, so the daily/weekly toggle never downloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from scanner import sessions
from scanner.config import Settings
from scanner.data import COLUMNS, DataResult, PriceProvider, cache_for, ensure_data, provider_for
from scanner.markets.config import BARS_PER_YEAR, RESOLUTIONS, MarketsConfig, SeriesSpec
from scanner.models import Diagnostic

MIN_BARS = 40  # fewer completed bars than this and a series is not analysed


def history_sessions(mc: MarketsConfig) -> int:
    """Daily sessions the longest resolution needs (weekly bars are built from daily)."""
    return max(int(np.ceil(mc.params(r).history_years * BARS_PER_YEAR["daily"])) for r in RESOLUTIONS) + 10


def refresh_prices(mc: MarketsConfig, s: Settings, now: datetime | None = None, *, refresh: bool = False,
                   provider: PriceProvider | None = None) -> DataResult:
    """Bring every Markets symbol up to the last completed session (network)."""
    now = now or datetime.now(sessions.ET)
    end = sessions.latest_completed_session(now, s.data.close_buffer_minutes)
    start = sessions.sessions_back(end, history_sessions(mc))
    return ensure_data(mc.symbols, provider or provider_for(s), cache_for(s), start, end, now, refresh=refresh,
                       rel_tol=s.data.cache_revision_rel_tol, close_buffer_minutes=s.data.close_buffer_minutes)


@dataclass
class Prices:
    frames: dict[str, pd.DataFrame]  # symbol → completed daily bars
    diagnostics: list[Diagnostic] = field(default_factory=list)
    fetched_at: str | None = None  # oldest download time among the loaded symbols (cache meta)

    @property
    def as_of(self) -> date | None:
        last = [f.index[-1].date() for f in self.frames.values() if len(f)]
        return max(last) if last else None


def load_cached(mc: MarketsConfig, s: Settings) -> Prices:
    """Every Markets symbol from the cache (no network). Missing symbols are diagnostics."""
    cache = cache_for(s)
    meta = cache.meta()
    frames, diags, fetched = {}, [], []
    for sym in mc.symbols:
        df = cache.load(sym)
        if df is None or df.empty:
            diags.append(Diagnostic(sym, "data", "warning", "not in the price cache yet: press Refresh prices"))
            continue
        frames[sym] = df
        if f := meta.get(sym, {}).get("fetched_at"):
            fetched.append(f)
    return Prices(frames, diags, min(fetched) if fetched else None)


def prices_from(result: DataResult) -> Prices:
    return Prices(result.completed_frames(non_empty=True), list(result.diagnostics),
                  datetime.now().astimezone().isoformat(timespec="seconds"))


# ----------------------------------------------------------------------------------------------
# Series frames
# ----------------------------------------------------------------------------------------------


def ratio_frame(num: pd.DataFrame, den: pd.DataFrame) -> pd.DataFrame:
    """Candles for num/den on common dates. Open and close are exact ratios; the intrabar path is unknown, so the
    high/low are the larger/smaller of the two (a body-only candle). Volume is NaN."""
    j = num[["open", "close"]].join(den[["open", "close"]], how="inner", lsuffix="_n", rsuffix="_d").dropna()
    o = j["open_n"] / j["open_d"]
    c = j["close_n"] / j["close_d"]
    out = pd.DataFrame({"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c,
                        "volume": np.nan}, index=j.index)
    return out[COLUMNS].astype("float64")


def series_daily(spec: SeriesSpec, frames: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    if any(sym not in frames for sym in spec.symbols):
        return None
    if spec.den is None:
        return frames[spec.num]
    return ratio_frame(frames[spec.num], frames[spec.den])


def _week_complete(last_day: date) -> bool:
    """True when no XNYS session remains in `last_day`'s Monday–Friday week."""
    friday = last_day + timedelta(days=4 - last_day.weekday())
    return not sessions.sessions_between(last_day + timedelta(days=1), friday)


def to_weekly(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """(completed weeks, the in-progress week or None). Weeks end on Friday (W-FRI). Each bar is dated by its
    last actual session, so a holiday Friday labels the week by Thursday."""
    if df.empty:
        return df, None
    key = df.index.to_period("W-FRI")
    g = df.groupby(key)
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(min_count=1)})
    out.index = pd.DatetimeIndex(df.index.to_series().groupby(key).max().to_numpy()).astype("datetime64[ns]")
    out.index.name = "date"
    out = out[COLUMNS].astype("float64")
    if _week_complete(out.index[-1].date()):
        return out, None
    return out.iloc[:-1], out.iloc[[-1]]


@dataclass
class SeriesFrame:
    spec: SeriesSpec
    completed: pd.DataFrame
    provisional: pd.DataFrame | None = None  # the in-progress week (weekly only)

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def full(self) -> pd.DataFrame:
        return self.completed if self.provisional is None else pd.concat([self.completed, self.provisional])


def build_series(specs: list[SeriesSpec], prices: Prices, res: str, history_bars: int,
                 min_bars: int = MIN_BARS) -> tuple[dict[str, SeriesFrame], list[Diagnostic]]:
    """Each spec's frame at resolution `res`, cut to its last `history_bars` completed bars."""
    out: dict[str, SeriesFrame] = {}
    diags: list[Diagnostic] = []
    for spec in specs:
        daily = series_daily(spec, prices.frames)
        if daily is None or daily.empty:
            missing = [s for s in spec.symbols if s not in prices.frames]
            diags.append(Diagnostic(spec.label, "data", "warning",
                                    f"no prices for {', '.join(missing) or spec.label}"))
            continue
        if res == "weekly":
            completed, prov = to_weekly(daily)
        else:
            completed, prov = daily, None
        completed = completed.tail(history_bars)
        if len(completed) < min_bars:
            diags.append(Diagnostic(spec.label, "data", "warning",
                                    f"only {len(completed)} {res} bars (needs {min_bars}); skipped"))
            continue
        if len(completed) < history_bars * 0.9:
            diags.append(Diagnostic(spec.label, "data", "info",
                                    f"short history: {len(completed)} of {history_bars} {res} bars"))
        out[spec.label] = SeriesFrame(spec, completed, prov)
    return out, diags


def closes(frames: dict[str, SeriesFrame], weekly: bool = False) -> pd.DataFrame:
    """Completed close prices of every series on one date index (outer join; NaN where a series has no bar).
    Weekly bars are keyed by their week's Friday, so a series missing a Friday still lines up with the others."""
    cols = {}
    for k, f in frames.items():
        c = f.completed["close"]
        if weekly:
            c = c.set_axis(c.index.to_period("W-FRI").end_time.normalize().astype("datetime64[ns]"))
        cols[k] = c
    return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()
