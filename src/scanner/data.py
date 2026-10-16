"""Price data: provider interface, yfinance implementation, Parquet cache, data-quality checks."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from scanner import sessions
from scanner.models import Diagnostic

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


class PriceProvider(Protocol):
    """Fetch daily OHLCV. Returns symbol → DataFrame (DatetimeIndex of dates, COLUMNS) or the Exception."""

    def fetch(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame | Exception]: ...


def normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case OHLCV columns, tz-naive midnight DatetimeIndex, sorted, no all-NaN rows, float64."""
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns {missing}")
    df = df[COLUMNS].astype("float64")
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize().astype("datetime64[ns]")
    df.index.name = "date"
    df = df[~df[["open", "high", "low", "close"]].isna().all(axis=1)]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


class YFinanceProvider:
    """Batched yfinance downloads with retry/backoff. One failing ticker never aborts the batch."""

    def __init__(self, price_adjustment: str = "split_only", batch_size: int = 40, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self.auto_adjust = price_adjustment == "total_return"
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.sleep = sleep
        self.calls = 0

    def _download(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        import yfinance as yf

        self.calls += 1
        return yf.download(
            symbols, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=self.auto_adjust, actions=False, group_by="ticker", progress=False,
            threads=True, repair=False, multi_level_index=True,
        )

    def fetch(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame | Exception]:
        out: dict[str, pd.DataFrame | Exception] = {}
        pending = list(dict.fromkeys(symbols))
        for attempt in range(self.max_retries):
            failed: list[str] = []
            for i in range(0, len(pending), self.batch_size):
                batch = pending[i:i + self.batch_size]
                try:
                    raw = self._download(batch, start, end)
                except Exception as e:  # network-level failure: retry the whole batch
                    log.warning("yfinance batch failed (%s): %s", batch, e)
                    for s in batch:
                        out[s] = e
                    failed.extend(batch)
                    continue
                for s in batch:
                    try:
                        sub = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                        df = normalize_frame(sub)
                        if df.empty:
                            raise LookupError("no data returned (unknown ticker or no bars in range)")
                        out[s] = df
                    except Exception as e:
                        out[s] = e if isinstance(e, LookupError) else LookupError(f"no data: {e}")
                        failed.append(s)
            if not failed or attempt == self.max_retries - 1:
                break
            self.sleep(2 ** attempt)
            pending = failed
        return out


class ParquetCache:
    """One Parquet file per symbol under <root>/<price_adjustment>/, plus meta.json."""

    def __init__(self, root: Path, price_adjustment: str):
        self.dir = Path(root) / price_adjustment
        self.meta_path = self.dir / "meta.json"

    def _path(self, symbol: str) -> Path:
        safe = symbol.replace("^", "_IDX_").replace("/", "_")
        return self.dir / f"{safe}.parquet"

    def load(self, symbol: str) -> pd.DataFrame | None:
        p = self._path(symbol)
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        df.index = pd.DatetimeIndex(df.index).astype("datetime64[ns]")
        return df

    def save(self, symbol: str, df: pd.DataFrame, requested_start: date | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self._path(symbol))
        meta = self.meta()
        entry = meta.get(symbol, {})
        entry.update({
            "first": df.index[0].date().isoformat() if len(df) else None,
            "last": df.index[-1].date().isoformat() if len(df) else None,
            "bars": len(df),
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        if requested_start is not None:
            prev = entry.get("requested_start")
            if prev is None or requested_start.isoformat() < prev:
                entry["requested_start"] = requested_start.isoformat()
        meta[symbol] = entry
        self.meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    def meta(self) -> dict:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        return {}

    def requested_start(self, symbol: str) -> date | None:
        v = self.meta().get(symbol, {}).get("requested_start")
        return date.fromisoformat(v) if v else None


@dataclass
class SymbolData:
    symbol: str
    completed: pd.DataFrame | None = None  # completed bars only (what the cache holds)
    provisional: pd.DataFrame | None = None  # at most one forming bar (live runs only; never cached)
    error: str | None = None
    fetched: bool = False


@dataclass
class DataResult:
    data: dict[str, SymbolData]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    network_calls: int = 0


def _overlap_mismatch(old: pd.DataFrame, new: pd.DataFrame, rel_tol: float) -> bool:
    common = old.index.intersection(new.index)
    if len(common) == 0:
        return False
    cols = ["open", "high", "low", "close"]
    a, b = old.loc[common, cols].to_numpy(), new.loc[common, cols].to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        rel = np.abs(a - b) / np.maximum(np.abs(a), 1e-12)
    return bool(np.nanmax(rel) > rel_tol)


def ensure_data(
    symbols: list[str],
    provider: PriceProvider,
    cache: ParquetCache,
    need_start: date,
    need_end: date,
    now: datetime,
    *,
    refresh: bool = False,
    want_provisional: bool = False,
    rel_tol: float = 1e-4,
    close_buffer_minutes: int = 30,
) -> DataResult:
    """Make sure each symbol has completed bars covering [need_start, need_end] (as far as data exists).

    - Missing / too-short / `refresh` → full download from need_start.
    - Stale → incremental download overlapping the last 5 cached sessions; on revision mismatch (e.g. a
      split re-scaled history) the symbol is re-downloaded in full (DS-7).
    - Provisional (still-forming) bars are returned separately and never written to the cache.
    """
    result = DataResult({s: SymbolData(s) for s in symbols})
    last_completed = sessions.latest_completed_session(now, close_buffer_minutes)
    target_end = min(need_end, last_completed)
    live = sessions.live_session(now) if want_provisional else None
    fetch_end = live or target_end

    full: list[str] = []
    incremental: dict[str, pd.DataFrame] = {}
    for s in symbols:
        cached = None if refresh else cache.load(s)
        if cached is None or cached.empty:
            full.append(s)
            continue
        first, last = cached.index[0].date(), cached.index[-1].date()
        req = cache.requested_start(s)
        covers_start = first <= need_start + timedelta(days=7) or (req is not None and req <= need_start)
        if not covers_start:
            full.append(s)
        elif last < target_end or (live is not None and last < live):
            incremental[s] = cached
        else:
            result.data[s].completed = cached

    def _store(sym: str, df: pd.DataFrame, requested_start: date | None) -> None:
        completed = df[df.index.date <= last_completed]
        prov = df[df.index.date > last_completed]
        cache.save(sym, completed, requested_start)
        result.data[sym].completed = completed
        result.data[sym].fetched = True
        if live is not None and len(prov) and prov.index[-1].date() == live:
            result.data[sym].provisional = prov.iloc[[-1]]

    calls_before = getattr(provider, "calls", 0)
    if full:
        fetched = provider.fetch(full, need_start, fetch_end)
        for s in full:
            r = fetched.get(s)
            if isinstance(r, pd.DataFrame) and not r.empty:
                _store(s, r, need_start)
            else:
                result.data[s].error = str(r) if r is not None else "no data returned"
                stale = cache.load(s)
                if stale is not None and not stale.empty:
                    result.data[s].completed = stale
                    result.diagnostics.append(Diagnostic(s, "data", "warning",
                                                         f"download failed, using cached data: {r}"))
                    result.data[s].error = None
                else:
                    result.diagnostics.append(Diagnostic(s, "data", "error", f"download failed: {r}"))

    if incremental:
        start = min(sessions.sessions_back(c.index[-1].date(), 5) for c in incremental.values())
        fetched = provider.fetch(sorted(incremental), start, fetch_end)
        refetch: list[str] = []
        for s, cached in incremental.items():
            r = fetched.get(s)
            if not isinstance(r, pd.DataFrame) or r.empty:
                result.data[s].completed = cached
                result.diagnostics.append(Diagnostic(s, "data", "warning",
                                                     f"incremental update failed, using cache: {r}"))
                continue
            new_completed = r[r.index.date <= last_completed]
            if _overlap_mismatch(cached, new_completed, rel_tol):
                refetch.append(s)
                result.diagnostics.append(Diagnostic(s, "data", "info",
                                                     "cached bars revised (split/adjustment?) — full re-download"))
                continue
            merged = pd.concat([cached[cached.index < r.index[0]], r])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            _store(s, merged, None)
        if refetch:
            fetched = provider.fetch(refetch, need_start, fetch_end)
            for s in refetch:
                r = fetched.get(s)
                if isinstance(r, pd.DataFrame) and not r.empty:
                    _store(s, r, need_start)
                else:
                    result.data[s].completed = incremental[s]
                    result.diagnostics.append(Diagnostic(s, "data", "warning",
                                                         f"full re-download failed, using cache: {r}"))
    result.network_calls = getattr(provider, "calls", 0) - calls_before
    return result


def quality_checks(symbol: str, df: pd.DataFrame, as_of: date, gap_warn_pct: float,
                   stale_bar_sessions: int, recent_bars: int = 150) -> list[Diagnostic]:
    """DS-10 checks on the analysis window (completed bars ≤ as_of).

    Missing bars are checked over the whole window; volume and gap checks over the last `recent_bars`
    (the charted period), so years-old events do not flag every run.
    """
    out: list[Diagnostic] = []
    if df.empty:
        return [Diagnostic(symbol, "quality", "error", "no bars")]
    first, last = df.index[0].date(), df.index[-1].date()
    expected = sessions.sessions_between(first, last)
    have = set(df.index.date)
    missing = [d for d in expected if d not in have]
    if missing:
        sample = ", ".join(d.isoformat() for d in missing[:5]) + (" …" if len(missing) > 5 else "")
        out.append(Diagnostic(symbol, "quality", "warning", f"{len(missing)} missing bar(s) vs calendar: {sample}"))
    behind = len(sessions.sessions_between(last, as_of)) - 1
    if behind > stale_bar_sessions - 1 and last < as_of:
        out.append(Diagnostic(symbol, "quality", "warning",
                              f"stale data: last bar {last.isoformat()} is {behind} session(s) before {as_of}"))
    recent = df.iloc[-recent_bars - 1:]
    vol = recent["volume"].iloc[1:]
    bad_vol = int((vol.isna() | (vol <= 0)).sum())
    if bad_vol:
        out.append(Diagnostic(symbol, "quality", "warning", f"{bad_vol} bar(s) with zero/NaN volume"))
    gap = ((recent["open"] / recent["close"].shift(1) - 1).abs() * 100).iloc[1:]
    big = gap[gap > gap_warn_pct]
    if len(big):
        sample = ", ".join(f"{d.date().isoformat()} ({g:.1f}%)" for d, g in big.tail(5).items())
        out.append(Diagnostic(symbol, "quality", "warning", f"{len(big)} gap(s) > {gap_warn_pct:g}%: {sample}"))
    return out
