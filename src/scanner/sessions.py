"""US equity trading calendar (XNYS): completed sessions, provisional bars, replay date snapping."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import pandas as pd

ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def calendar() -> xc.ExchangeCalendar:
    return xc.get_calendar("XNYS", start="2000-01-01")


def _ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d)


def is_session(d: date) -> bool:
    return bool(calendar().is_session(_ts(d)))


def session_on_or_before(d: date) -> date:
    cal = calendar()
    return cal.date_to_session(_ts(d), direction="previous").date()


def session_close(d: date) -> datetime:
    return calendar().session_close(_ts(d)).to_pydatetime().astimezone(ET)


def session_open(d: date) -> datetime:
    return calendar().session_open(_ts(d)).to_pydatetime().astimezone(ET)


def sessions_between(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [t.date() for t in calendar().sessions_in_range(_ts(start), _ts(end))]


def sessions_back(d: date, n: int) -> date:
    """The session `n` sessions before session-on-or-before(d)."""
    cal = calendar()
    s = cal.date_to_session(_ts(d), direction="previous")
    return cal.session_offset(s, -n).date()


def latest_completed_session(now: datetime, buffer_minutes: int = 30) -> date:
    """Most recent session whose close (+ buffer for data to settle) is at or before `now`."""
    now_et = now.astimezone(ET)
    d = session_on_or_before(now_et.date())
    if now_et < session_close(d) + timedelta(minutes=buffer_minutes):
        d = sessions_back(d, 1)
    return d


def is_provisional(bar_date: date, now: datetime, buffer_minutes: int = 30) -> bool:
    """A bar is provisional while its session has not closed (+ buffer) yet."""
    if not is_session(bar_date):
        return False
    return now.astimezone(ET) < session_close(bar_date) + timedelta(minutes=buffer_minutes)


def live_session(now: datetime) -> date | None:
    """Today's session if the market has opened today (a forming bar may exist), else None."""
    now_et = now.astimezone(ET)
    today = now_et.date()
    if is_session(today) and now_et >= session_open(today):
        return today
    return None
