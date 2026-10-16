"""Historical replay (§8): signal log over a date range, forward outcomes, expected-reads validation.

Every replay day calls pipeline.scan() with the same preloaded frames; scan() cuts them at that day, so no
day can see later bars. Forward outcomes (RP-4) are computed afterwards from the full series and only
written to the log — they never feed back into signals or scores. They are NOT a backtest.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from scanner import sessions
from scanner.config import Config, ExpectedRead
from scanner.models import Diagnostic, SignalResult, Tri
from scanner.pipeline import RunResult, all_symbols, load_data, scan

LOG_COLUMNS = ["date", "symbol", "signal", "kind", "direction", "level_id", "level_type", "level", "zone_low",
               "zone_high", "trend", "close", "atr", "rsi", "strategies", "score", "triggers"]


@dataclass
class ReplayResult:
    start: date
    end: date
    days: list[date]
    rows: list[dict]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    runtime_s: float = 0.0


def _same(a: SignalResult, b: SignalResult) -> bool:
    return (a.name == b.name and a.bar_idx == b.bar_idx
            and (a.level.id if a.level else None) == (b.level.id if b.level else None))


def _r(x: float, nd: int = 4):
    return "" if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def log_rows(res: RunResult) -> list[dict]:
    """Age-0 signals of one replay day, with the strategies they triggered."""
    rows = []
    k = res.settings.levels.zone_atr_mult
    for sym in sorted(res.analyses):
        sa = res.analyses[sym]
        cands = [c for c in res.candidates if c.symbol == sym]
        t = sa.last_idx
        for sig in sa.signals:
            if sig.age != 0 or sig.status is not Tri.TRUE:
                continue
            using = [c for c in cands if any(_same(sig, q) for q in c.signals)]
            best = max(using, key=lambda c: c.score, default=None)
            lv = sig.level
            lo, hi = lv.zone(t, sa.bars.atr, k) if lv else (np.nan, np.nan)
            rows.append({
                "date": sa.as_of.isoformat(), "symbol": sym, "signal": sig.name, "kind": sig.kind,
                "direction": sig.direction, "level_id": lv.id if lv else "", "level_type": lv.label if lv else "",
                "level": _r(lv.value(t)) if lv else "", "zone_low": _r(lo), "zone_high": _r(hi),
                "trend": sa.trend.state, "close": _r(sa.bars.close[t]), "atr": _r(sa.bars.atr[t]),
                "rsi": _r(sa.bars.rsi[t], 2), "strategies": ";".join(sorted(c.strategy_id for c in using)),
                "score": best.score if best else "",
                "triggers": "; ".join(f"{a.label} {a.price:.2f}" for a in (best.alerts if best else [])),
            })
    return rows


def forward_outcomes(rows: list[dict], frames: dict[str, pd.DataFrame], horizons: list[int]) -> None:
    """Add fwd_ret_N, mfe_atr_N, mae_atr_N (direction-aware; neutral: mfe = up move, mae = down move)."""
    for row in rows:
        df = frames.get(row["symbol"])
        if df is None:
            continue
        pos = df.index.get_indexer([pd.Timestamp(row["date"])])[0]
        c0, atr = float(row["close"]), row["atr"]
        for n in horizons:
            if pos < 0 or pos + n >= len(df) or atr in ("", 0):
                row[f"fwd_ret_{n}"] = row[f"mfe_atr_{n}"] = row[f"mae_atr_{n}"] = ""
                continue
            fut = df.iloc[pos + 1:pos + n + 1]
            up, down = (fut["high"].max() - c0) / atr, (c0 - fut["low"].min()) / atr
            mfe, mae = (down, up) if row["direction"] == "bearish" else (up, down)
            row[f"fwd_ret_{n}"] = _r(df["close"].iloc[pos + n] / c0 - 1, 5)
            row[f"mfe_atr_{n}"] = _r(mfe, 3)
            row[f"mae_atr_{n}"] = _r(mae, 3)


def run_replay(cfg: Config, start: date, end: date, *, now: datetime | None = None, provider=None,
               refresh: bool = False, symbols: list[str] | None = None) -> ReplayResult:
    import time

    t0 = time.perf_counter()
    now = now or datetime.now(sessions.ET)
    s = cfg.settings
    last_done = sessions.latest_completed_session(now, s.data.close_buffer_minutes)
    days = sessions.sessions_between(start, min(end, last_done))
    if not days:
        return ReplayResult(start, end, [], [], [Diagnostic(None, "replay", "error", "no trading days in range")])
    need_start = sessions.sessions_back(days[0], s.window_bars + 10)
    dr = load_data(cfg, all_symbols(cfg, symbols), need_start, last_done, now, refresh=refresh, provider=provider)
    frames = {k: v.completed for k, v in dr.data.items() if v.completed is not None}
    rows: list[dict] = []
    diags = list(dr.diagnostics)
    for d in days:
        res = scan(cfg, frames, d, symbols=symbols, quality=False)
        rows += log_rows(res)
        diags += [x for x in res.diagnostics if x.severity == "error"]
    forward_outcomes(rows, frames, s.replay.forward_horizons)
    rows.sort(key=lambda r: (r["date"], r["symbol"], r["signal"], r["level_id"]))
    return ReplayResult(start, end, days, rows, diags, time.perf_counter() - t0)


def write_signal_log(rr: ReplayResult, out_dir: Path, horizons: list[int]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "signals_log.csv"
    cols = LOG_COLUMNS + [f"{m}_{n}" for n in horizons for m in ("fwd_ret", "mfe_atr", "mae_atr")]
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write("# Forward outcomes are informational only — NOT a backtest (no options P&L, sizing or costs).\n")
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rr.rows)
    return path


# ----------------------------------------------------------------------------------------------
# Expected reads (§8.2)
# ----------------------------------------------------------------------------------------------


@dataclass
class ReadCheck:
    read: ExpectedRead
    as_of: date | None
    actual: list[str]
    matches: list[str]
    misses: list[str]
    extras: list[str]
    evidence: list[str]


def check_reads(cfg: Config, *, now: datetime | None = None, provider=None) -> list[ReadCheck]:
    now = now or datetime.now(sessions.ET)
    s = cfg.settings
    reads = cfg.expected_reads
    if not reads:
        return []
    last_done = sessions.latest_completed_session(now, s.data.close_buffer_minutes)
    syms = sorted({r.symbol for r in reads})
    first = min(sessions.session_on_or_before(r.date) for r in reads)
    dr = load_data(cfg, all_symbols(cfg, syms), sessions.sessions_back(first, s.window_bars + 10), last_done, now,
                   provider=provider)
    frames = {k: v.completed for k, v in dr.data.items() if v.completed is not None}
    out: list[ReadCheck] = []
    by_day: dict[date, list[ExpectedRead]] = {}
    for r in reads:
        by_day.setdefault(sessions.session_on_or_before(r.date), []).append(r)
    for d in sorted(by_day):
        day_syms = sorted({r.symbol for r in by_day[d]})
        res = scan(cfg, frames, d, symbols=day_syms, quality=False)
        for r in by_day[d]:
            sa = res.analyses.get(r.symbol)
            if sa is None:
                why = dict(res.skipped).get(r.symbol, "not analysed")
                out.append(ReadCheck(r, d, [], [], list(r.expected), [], [f"skipped: {why}"]))
                continue
            fired = [sg for sg in sa.signals if sg.age == 0 and sg.status is Tri.TRUE]
            actual = sorted({sg.name for sg in fired} | {sa.trend.state})
            exp = set(r.expected)
            ev = [f"trend {sa.trend.state}: {sa.trend.reason}"]
            ev += [f"{sg.summary} on {sg.date}" for sg in fired]
            for name in sorted(exp - set(actual)):
                for nr in sa.near.get(name, []):
                    ev.append(f"near {name}: {nr.description}")
            out.append(ReadCheck(r, d, actual, sorted(exp & set(actual)), sorted(exp - set(actual)),
                                 sorted(set(actual) - exp), ev))
    return out


def write_read_checks(checks: list[ReadCheck], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "as_of", "symbol", "expected", "matches", "misses", "extras", "note", "evidence"])
        for c in checks:
            w.writerow([c.read.date.isoformat(), c.as_of.isoformat() if c.as_of else "", c.read.symbol,
                        ";".join(c.read.expected), ";".join(c.matches), ";".join(c.misses), ";".join(c.extras),
                        c.read.note, " | ".join(c.evidence)])
    return path
