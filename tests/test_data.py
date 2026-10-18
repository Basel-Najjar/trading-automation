"""T-6: data layer — fake provider, cache incremental updates, split revisions, failure isolation, quality."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from conftest import FakeProvider, random_frame
from scanner import sessions
from scanner.data import (
    ParquetCache,
    Profile,
    ensure_data,
    fetch_profiles,
    normalize_frame,
    quality_checks,
    recent_gaps,
)

ET = sessions.ET
AFTER_CLOSE = datetime(2026, 9, 30, 17, 0, tzinfo=ET)
START = date(2024, 1, 2)


@pytest.fixture
def frames():
    return {"AAA": random_frame(700, seed=1), "BBB": random_frame(700, seed=2)}


def test_sessions_basics():
    assert sessions.latest_completed_session(datetime(2026, 9, 30, 16, 10, tzinfo=ET)) == date(2026, 9, 29)
    assert sessions.latest_completed_session(AFTER_CLOSE) == date(2026, 9, 30)
    assert sessions.latest_completed_session(datetime(2026, 10, 4, 12, 0, tzinfo=ET)) == date(2026, 10, 2)  # Sunday
    assert sessions.session_on_or_before(date(2026, 7, 4)) == date(2026, 7, 2)  # holiday (observed 7/3) + weekend
    assert sessions.live_session(datetime(2026, 9, 30, 8, 0, tzinfo=ET)) is None
    # early close (day after Thanksgiving): 13:00 close
    assert sessions.session_close(date(2026, 11, 27)).hour == 13


def test_full_fetch_then_cache_hit(tmp_path, frames):
    prov = FakeProvider(frames)
    cache = ParquetCache(tmp_path, "split_only")
    r = ensure_data(["AAA", "BBB"], prov, cache, START, date(2026, 9, 30), AFTER_CLOSE)
    assert prov.calls == 1 and r.network_calls == 1
    pd.testing.assert_frame_equal(r.data["AAA"].completed, frames["AAA"][frames["AAA"].index >= pd.Timestamp(START)],
                                  check_freq=False)
    r2 = ensure_data(["AAA", "BBB"], prov, cache, START, date(2026, 9, 30), AFTER_CLOSE)
    assert prov.calls == 1 and r2.network_calls == 0  # warm cache: no network
    pd.testing.assert_frame_equal(r2.data["AAA"].completed, r.data["AAA"].completed, check_freq=False)


def test_incremental_update(tmp_path, frames):
    cache = ParquetCache(tmp_path, "split_only")
    early = {k: v[v.index <= pd.Timestamp("2026-09-24")] for k, v in frames.items()}
    ensure_data(["AAA"], FakeProvider(early), cache, START, date(2026, 9, 24),
                datetime(2026, 9, 24, 18, 0, tzinfo=ET))
    prov = FakeProvider(frames)
    r = ensure_data(["AAA"], prov, cache, START, date(2026, 9, 30), AFTER_CLOSE)
    assert prov.calls == 1
    syms, start, end = prov.requests[0]
    assert start == sessions.sessions_back(date(2026, 9, 24), 5) and end == date(2026, 9, 30)
    assert r.data["AAA"].completed.index[-1] == pd.Timestamp("2026-09-30")
    full = frames["AAA"][frames["AAA"].index >= pd.Timestamp(START)]
    pd.testing.assert_frame_equal(cache.load("AAA"), full, check_freq=False)


def test_split_revision_triggers_full_refetch(tmp_path, frames):
    cache = ParquetCache(tmp_path, "split_only")
    early = {k: v[v.index <= pd.Timestamp("2026-09-24")] for k, v in frames.items()}
    ensure_data(["AAA"], FakeProvider(early), cache, START, date(2026, 9, 24),
                datetime(2026, 9, 24, 18, 0, tzinfo=ET))
    split = {"AAA": frames["AAA"].assign(**{c: frames["AAA"][c] / 2 for c in ("open", "high", "low", "close")})}
    prov = FakeProvider(split)
    r = ensure_data(["AAA"], prov, cache, START, date(2026, 9, 30), AFTER_CLOSE)
    assert prov.calls == 2  # incremental, then full re-download
    assert any("revised" in d.message for d in r.diagnostics)
    assert abs(cache.load("AAA")["close"].iloc[0] - split["AAA"]["close"][split["AAA"].index >= pd.Timestamp(START)]
               .iloc[0]) < 1e-12


def test_failure_isolation_and_stale_cache_fallback(tmp_path, frames):
    cache = ParquetCache(tmp_path, "split_only")
    r = ensure_data(["AAA", "BBB", "ZZZ"], FakeProvider(frames, fail={"BBB"}), cache, START, date(2026, 9, 30),
                    AFTER_CLOSE)
    assert r.data["AAA"].completed is not None
    assert r.data["BBB"].completed is None and r.data["BBB"].error
    assert {d.symbol for d in r.diagnostics if d.severity == "error"} == {"BBB", "ZZZ"}


def test_provisional_bar_returned_but_not_cached(tmp_path, frames):
    cache = ParquetCache(tmp_path, "split_only")
    midday = datetime(2026, 9, 30, 12, 0, tzinfo=ET)
    r = ensure_data(["AAA"], FakeProvider(frames), cache, START, date(2026, 9, 30), midday, want_provisional=True)
    d = r.data["AAA"]
    assert d.completed.index[-1] == pd.Timestamp("2026-09-29")
    assert d.provisional is not None and d.provisional.index[-1] == pd.Timestamp("2026-09-30")
    assert cache.load("AAA").index[-1] == pd.Timestamp("2026-09-29")


def test_normalize_frame_handles_tz_and_case():
    idx = pd.DatetimeIndex(["2026-09-29 00:00", "2026-09-30 00:00"]).tz_localize("America/New_York")
    df = pd.DataFrame({"Open": [1, 2], "High": [2, 3], "Low": [0.5, 1], "Close": [1.5, 2.5], "Volume": [10, 20],
                       "Adj Close": [1.4, 2.4]}, index=idx)
    out = normalize_frame(df)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.tz is None and out.index[0] == pd.Timestamp("2026-09-29")


def test_quality_checks():
    df = random_frame(300, seed=3)
    df = df.drop(df.index[150])  # missing bar
    df.iloc[-5, df.columns.get_loc("volume")] = 0
    df.iloc[-3, df.columns.get_loc("open")] = df["close"].iloc[-4] * 1.2  # 20% gap: not a quality warning any more
    msgs = [d.message for d in quality_checks("AAA", df, date(2026, 10, 2), 1)]
    assert any("1 missing bar" in m for m in msgs)
    assert any("zero/NaN volume" in m for m in msgs)
    assert not any("gap" in m for m in msgs)
    assert any("stale data" in m for m in msgs)  # last bar 9/30, as-of 10/2
    assert quality_checks("AAA", random_frame(300, seed=4), date(2026, 9, 30), 1) == []


def test_recent_gaps_atr_multiple_and_lookback():
    df = random_frame(300, seed=3)
    assert recent_gaps(df, 14, 3.0, 15) == []
    old, new = df.copy(), df.copy()
    old.iloc[-20, old.columns.get_loc("open")] = old["close"].iloc[-21] * 1.2  # 20 bars ago: outside 15
    assert recent_gaps(old, 14, 3.0, 15) == []
    assert len(recent_gaps(old, 14, 3.0, 25)) == 1
    new.iloc[-5, new.columns.get_loc("open")] = new["close"].iloc[-6] * 1.08
    (d, g_atr, g_pct), = recent_gaps(new, 14, 3.0, 15)
    assert d == new.index[-5].date() and abs(g_pct - 8.0) < 1e-9 and g_atr > 3.0
    assert recent_gaps(new, 14, g_atr + 0.1, 15) == []  # threshold is in ATRs, not percent


def test_fetch_profiles_caches_and_refetches_stale(tmp_path):
    calls: list[str] = []

    def fake(sym: str) -> Profile:
        calls.append(sym)
        if sym == "BAD":
            raise RuntimeError("boom")
        return Profile(f"{sym} Inc", f"{sym} does things.")

    t0 = datetime(2026, 10, 1)
    out = fetch_profiles(["AAA", "BAD"], tmp_path, now=t0, fetch=fake)
    assert out["AAA"] == Profile("AAA Inc", "AAA does things.") and out["BAD"] == Profile()
    assert sorted(calls) == ["AAA", "BAD"]

    calls.clear()  # within the TTL nothing is refetched, and the failure is not retried
    fetch_profiles(["AAA", "BAD"], tmp_path, now=t0 + timedelta(days=5), fetch=fake)
    assert calls == []

    fetch_profiles(["AAA"], tmp_path, now=t0 + timedelta(days=5), fetch=fake, refresh=True)
    assert calls == ["AAA"]

    calls.clear()  # stale after the TTL
    fetch_profiles(["AAA", "BAD"], tmp_path, now=t0 + timedelta(days=40), fetch=fake)
    assert sorted(calls) == ["AAA", "BAD"]
