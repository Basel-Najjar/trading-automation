"""Shared fixtures: synthetic OHLC builders, hand-built bars, fake price provider, temp config dirs."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scanner import sessions
from scanner.config import Settings, load_config
from scanner.indicators import compute_bars
from scanner.models import Bars, Level, Tech, TrendResult

ROOT = Path(__file__).resolve().parents[1]


def session_index(n: int, end: date = date(2026, 9, 30)) -> pd.DatetimeIndex:
    start = sessions.sessions_back(end, n - 1)
    days = sessions.sessions_between(start, end)
    assert len(days) == n
    return pd.DatetimeIndex(days).astype("datetime64[ns]")


def random_frame(n: int = 600, seed: int = 0, end: date = date(2026, 9, 30), start_price: float = 100.0,
                 drift: float = 0.0003, vol: float = 0.015) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.r_[start_price, close[:-1]] * np.exp(rng.normal(0, vol / 4, n))
    hi = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": volume},
                        index=session_index(n, end))


def frame_from_ohlc(rows: list[tuple[float, float, float, float]], end: date = date(2026, 9, 30)) -> pd.DataFrame:
    a = np.array(rows, dtype=float)
    return pd.DataFrame({"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3],
                         "volume": np.full(len(a), 1e6)}, index=session_index(len(a), end))


def bars_from_ohlc(rows: list[tuple[float, float, float, float]], atr: float = 2.0, rsi: float = 50.0) -> Bars:
    """Hand-built bars with constant ATR/RSI so zone arithmetic is exact in tests."""
    df = frame_from_ohlc(rows)
    b = compute_bars(df, Settings())
    b.atr = np.full(len(rows), atr)
    b.rsi = np.full(len(rows), rsi)
    return b


def hlevel(price: float, n: int, origin: str = "support", lid: str = "h_test", quality: float = 2.0) -> Level:
    return Level(id=lid, type="horizontal", values=np.full(n, price), origin=origin, touches=2, quality=quality)


def tech_for(bars: Bars, levels: list[Level], cutoff: int | None = None,
             trend: TrendResult | None = None) -> Tech:
    return Tech(bars, len(bars) - 1 if cutoff is None else cutoff, [], [], levels,
                trend or TrendResult("mixed", "test"))


class FakeProvider:
    """PriceProvider over in-memory frames; records calls; can fail selected symbols."""

    def __init__(self, frames: dict[str, pd.DataFrame], fail: set[str] | None = None):
        self.frames = frames
        self.fail = fail or set()
        self.calls = 0
        self.requests: list[tuple[list[str], date, date]] = []

    def fetch(self, symbols, start, end):
        self.calls += 1
        self.requests.append((list(symbols), start, end))
        out = {}
        for s in symbols:
            if s in self.fail or s not in self.frames:
                out[s] = LookupError(f"no data for {s}")
                continue
            df = self.frames[s]
            sub = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
            out[s] = sub.copy() if len(sub) else LookupError("no bars in range")
        return out


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Copy of the sample config with a tiny universe and the cache dir inside tmp_path."""
    d = tmp_path / "config"
    d.mkdir()
    # tests pin the preserved default playbook, not the owner's working copy (which the app edits)
    shutil.copyfile(ROOT / "config" / "strategies.default.yaml", d / "strategies.yaml")
    shutil.copyfile(ROOT / "config" / "strategies.default.yaml", d / "strategies.default.yaml")
    text = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    text = text.replace("cache_dir: cache ", f"cache_dir: {(tmp_path / 'cache').as_posix()} ")
    (d / "settings.yaml").write_text(text, encoding="utf-8")
    (d / "universe.csv").write_text("symbol,type,sector_etf,enabled,notes\nAAA,stock,XLK,true,\nBBB,stock,XLK,true,\n"
                                    "CCC,stock,,true,\n", encoding="utf-8")
    return d


@pytest.fixture
def cfg(config_dir: Path):
    c, issues = load_config(config_dir)
    assert c is not None, [str(i) for i in issues]
    return c


@pytest.fixture
def market_frames() -> dict[str, pd.DataFrame]:
    return {
        "AAA": random_frame(700, seed=1, drift=0.0008),
        "BBB": random_frame(700, seed=2, drift=-0.0004),
        "CCC": random_frame(700, seed=3, drift=0.0),
        "XLK": random_frame(700, seed=4, drift=0.0004),
        "SPY": random_frame(700, seed=5, drift=0.0003, vol=0.01),
    }
