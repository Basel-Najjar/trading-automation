"""Pipeline orchestration (§5). `scan()` is pure (no network) and is the single place data is sliced at as-of.

No look-ahead by construction: each symbol's frame is cut to bars ≤ as_of before any computation, and
structure for bar i is built only from bars ≤ min(i, last completed bar).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from scanner import sessions
from scanner.config import Config, Settings
from scanner.data import ParquetCache, PriceProvider, YFinanceProvider, ensure_data, quality_checks
from scanner.flow import period_returns, ratio_trend, state_of
from scanner.indicators import compute_bars, find_pivots
from scanner.levels import build_levels, confluence_zones, nearest_levels
from scanner.models import NA, Bars, Candidate, Diagnostic, FlowResult, SymbolAnalysis, Tech, Tri, WatchItem
from scanner.scoring import build_watchlist, rank, score_candidate
from scanner.signals import evaluate_signals, near_signals
from scanner.strategies import StrategyEval, evaluate_strategy
from scanner.trend import classify_trend

PARTIAL_MIN_BARS = 60


@dataclass
class RunResult:
    as_of: date
    requested_as_of: date | None
    provisional: bool
    analyses: dict[str, SymbolAnalysis]
    context: dict[str, SymbolAnalysis]
    evals: dict[str, list[StrategyEval]]
    candidates: list[Candidate]
    watchlist: list[WatchItem]
    skipped: list[tuple[str, str]]
    diagnostics: list[Diagnostic]
    strategies_enabled: list[str]
    strategies_skipped: list[tuple[str, str]]
    settings: Settings
    hashes: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    runtime_s: float = 0.0
    started: datetime | None = None
    benchmark: str = "SPY"
    sector_etfs: list[str] = field(default_factory=list)
    focus_symbols: list[str] | None = None

    @property
    def exit_code(self) -> int:
        if not self.analyses:
            return 1
        warn = any(d.severity in ("warning", "error") for d in self.diagnostics)
        return 2 if (warn or self.skipped) else 0


# ----------------------------------------------------------------------------------------------
# Per-symbol analysis
# ----------------------------------------------------------------------------------------------


def build_tech(bars: Bars, cutoff: int, s: Settings, manual: list | None = None) -> Tech:
    highs, lows = find_pivots(bars.high, bars.low, bars.dates, s.pivots.pivot_window, last_usable=cutoff)
    levels, notes = build_levels(bars, highs, lows, cutoff, s, manual)
    trend = classify_trend(bars.close, bars.atr, highs, lows, cutoff, s)
    return Tech(bars, cutoff, highs, lows, levels, trend, notes)


def overrides_key(overrides: dict) -> str:
    return json.dumps(overrides, sort_keys=True) if overrides else ""


def analyze_symbol(symbol: str, completed: pd.DataFrame, s: Settings, provisional: pd.DataFrame | None = None,
                   manual: list | None = None, variants: dict[str, Settings] | None = None) -> SymbolAnalysis:
    """Analyse one symbol. `completed` must already be cut at as-of (completed bars only)."""
    frame = completed if provisional is None else pd.concat([completed, provisional])
    bars = compute_bars(frame, s)
    last = len(bars) - 1
    done = last - 1 if provisional is not None else last
    by_cutoff: dict[int, Tech] = {}
    techs = []
    for age in range(s.signals.max_signal_age_bars + 1):
        cutoff = min(last - age, done)
        if cutoff < 1:
            break
        if cutoff not in by_cutoff:
            by_cutoff[cutoff] = build_tech(bars, cutoff, s, manual)
        techs.append(by_cutoff[cutoff])
    signals = evaluate_signals(techs, last, s)
    sv = {"": signals}
    for key, vs in (variants or {}).items():
        sv[key] = evaluate_signals(techs, last, vs)
    k = s.levels.zone_atr_mult
    sup, res = nearest_levels(techs[0].levels, last, bars, s.levels.nearest_levels_per_side)
    sa = SymbolAnalysis(
        symbol=symbol, bars=bars, last_idx=last, provisional=provisional is not None, techs=techs,
        signals=signals, confluence=confluence_zones(techs[0].levels, last, bars, k),
        nearest_support=sup, nearest_resistance=res, near=near_signals(techs[0], done, s),
        returns=period_returns(completed["close"]), signal_variants=sv,
    )
    for note in techs[0].notes:
        sa.diagnostics.append(Diagnostic(symbol, "levels", "info", note))
    return sa


def _fingerprint(df: pd.DataFrame) -> str:
    h = hashlib.sha256(np.ascontiguousarray(df[["open", "high", "low", "close", "volume"]].to_numpy()).tobytes())
    h.update(",".join(d.isoformat() for d in df.index.date).encode())
    return f"{df.index[-1].date().isoformat()}:{h.hexdigest()[:16]}"


# ----------------------------------------------------------------------------------------------
# Scan (pure)
# ----------------------------------------------------------------------------------------------


def scan(
    cfg: Config,
    frames: dict[str, pd.DataFrame],
    as_of: date,
    *,
    provisional: dict[str, pd.DataFrame] | None = None,
    symbols: list[str] | None = None,
    strategy_ids: list[str] | None = None,
    diagnostics: list[Diagnostic] | None = None,
    requested_as_of: date | None = None,
    quality: bool = True,
) -> RunResult:
    """Run stages [2]–[8] on preloaded completed frames, using only bars ≤ as_of."""
    t0 = time.perf_counter()
    s = cfg.settings
    provisional = provisional or {}
    diags = list(diagnostics or [])
    notes: list[str] = []
    bench = s.data.benchmark
    universe = symbols or cfg.symbols
    sector_etfs = sorted({e for sym in universe if (e := cfg.sector_for(sym))})

    strategies = [st for st in cfg.strategies if strategy_ids is None or st.id in strategy_ids]
    active = [st for st in strategies if st.active]
    skipped_strats = [(st.id, "stub" if st.status == "stub" else "disabled") for st in strategies if not st.active]
    variants = {overrides_key(st.technical.overrides): s.with_overrides(st.technical.overrides)
                for st in active if st.technical.overrides}

    analyses: dict[str, SymbolAnalysis] = {}
    context: dict[str, SymbolAnalysis] = {}
    skipped: list[tuple[str, str]] = []
    fingerprints: dict[str, str] = {}
    windows: dict[str, pd.DataFrame] = {}
    is_prov = False

    for sym in list(dict.fromkeys([*universe, bench, *sector_etfs])):
        in_universe = sym in universe
        df = frames.get(sym)
        if df is None or df.empty:
            if in_universe:
                skipped.append((sym, "no price data"))
            else:
                diags.append(Diagnostic(sym, "data", "warning", "no price data for benchmark/sector ETF"))
            continue
        win = df[df.index <= pd.Timestamp(as_of)].tail(s.window_bars)
        if win.empty:
            if in_universe:
                skipped.append((sym, f"no bars on or before {as_of}"))
            continue
        if len(win) < s.data.min_bars and not (s.data.allow_partial_history and len(win) >= PARTIAL_MIN_BARS):
            if in_universe:
                skipped.append((sym, f"insufficient history: {len(win)} bars < {s.data.min_bars}"))
            else:
                diags.append(Diagnostic(sym, "data", "warning", f"insufficient history ({len(win)} bars)"))
            continue
        if len(win) < s.data.min_bars:
            diags.append(Diagnostic(sym, "data", "warning",
                                    f"partial history ({len(win)} bars): SMA200-dependent logic is n/a"))
        prov = provisional.get(sym)
        if prov is not None and (len(prov) != 1 or prov.index[-1] <= win.index[-1]
                                 or prov.index[-1] > pd.Timestamp(as_of)):
            prov = None
        if quality:
            # with a forming bar for as_of, completed data is current if it reaches the prior session
            ref = sessions.sessions_back(as_of, 1) if prov is not None else as_of
            diags += quality_checks(sym, win, ref, s.data.gap_warn_pct, s.data.stale_bar_sessions,
                                    s.report.chart_bars)
        windows[sym] = win
        fingerprints[sym] = _fingerprint(win)
        try:
            sa = analyze_symbol(sym, win, s, prov, cfg.manual_levels.get(sym), variants)
        except Exception as e:  # per-symbol isolation (NF-5)
            diags.append(Diagnostic(sym, "analysis", "error", f"{type(e).__name__}: {e}"))
            if in_universe:
                skipped.append((sym, f"analysis error: {type(e).__name__}: {e}"))
            continue
        sa.sector_etf = cfg.sector_for(sym)
        diags += sa.diagnostics
        is_prov = is_prov or sa.provisional
        (analyses if in_universe else context)[sym] = sa
        if in_universe and sym in (bench, *sector_etfs):
            context[sym] = sa

    # [6] relative strength / flow
    bench_sa = context.get(bench) or analyses.get(bench)
    if bench_sa is None:
        diags.append(Diagnostic(bench, "flow", "warning",
                                f"benchmark {bench} unavailable — all flow components are n/a"))
    market = bench_sa.trend.state if bench_sa else NA
    sector_cache: dict[str, str] = {}

    def _close(sym: str) -> pd.Series | None:
        w = windows.get(sym)
        return w["close"] if w is not None else None

    for sym, sa in analyses.items():
        etf = cfg.sector_for(sym)
        sector = name = NA
        if etf and sym != bench:
            if etf not in sector_cache:
                ec, bc = _close(etf), _close(bench)
                sector_cache[etf] = state_of(ratio_trend(ec, bc, s)) if ec is not None and bc is not None else NA
            sector = sector_cache[etf]
            sc, ec = _close(sym), _close(etf)
            name = state_of(ratio_trend(sc, ec, s)) if sc is not None and ec is not None else NA
            if ec is None:
                diags.append(Diagnostic(sym, "flow", "warning", f"sector ETF {etf} unavailable — name-vs-sector n/a"))
        sa.flow = FlowResult(market, sector, name, etf, bench)

    # [7] strategy matching, [8] scoring + watch list
    evals: dict[str, list[StrategyEval]] = {}
    cands: list[Candidate] = []
    for sym in sorted(analyses):
        sa = analyses[sym]
        evals[sym] = []
        for st in active:
            sigs = sa.signal_variants.get(overrides_key(st.technical.overrides), sa.signals)
            ev = evaluate_strategy(st, sa.trend.state, sigs, sa.flow, s, sa.provisional)
            evals[sym].append(ev)
            if ev.status is not Tri.FALSE:
                cands.append(score_candidate(ev, sa, s))
    cands = rank(cands)
    watch = build_watchlist(evals, analyses, s)

    if requested_as_of and requested_as_of != as_of:
        notes.append(f"Requested {requested_as_of} is not a trading day; using prior session {as_of}.")
    return RunResult(
        as_of=as_of, requested_as_of=requested_as_of, provisional=is_prov, analyses=analyses, context=context,
        evals=evals, candidates=cands, watchlist=watch, skipped=skipped, diagnostics=diags,
        strategies_enabled=[st.id for st in active], strategies_skipped=skipped_strats, settings=s,
        hashes=dict(cfg.hashes), fingerprints=fingerprints, notes=notes, runtime_s=time.perf_counter() - t0,
        benchmark=bench, sector_etfs=sector_etfs, focus_symbols=symbols,
    )


# ----------------------------------------------------------------------------------------------
# Data loading + scan (network)
# ----------------------------------------------------------------------------------------------


def resolve_as_of(requested: date | None, now: datetime, s: Settings) -> tuple[date, bool]:
    """(as_of session, live?). Live runs may include a provisional bar for today's session."""
    last_done = sessions.latest_completed_session(now, s.data.close_buffer_minutes)
    if requested is None or requested > last_done:
        live = sessions.live_session(now)
        if live is not None and live > last_done:
            return live, True
        return last_done, requested is None
    return sessions.session_on_or_before(requested), False


def load_data(cfg: Config, symbols: list[str], start: date, end: date, now: datetime, *, refresh: bool = False,
              live: bool = False, provider: PriceProvider | None = None):
    s = cfg.settings
    provider = provider or YFinanceProvider(s.data.price_adjustment, s.data.batch_size, s.data.max_retries)
    cache = ParquetCache(Path(s.data.cache_dir), s.data.price_adjustment)
    return ensure_data(symbols, provider, cache, start, end, now, refresh=refresh, want_provisional=live,
                       rel_tol=s.data.cache_revision_rel_tol, close_buffer_minutes=s.data.close_buffer_minutes)


def all_symbols(cfg: Config, symbols: list[str] | None = None) -> list[str]:
    universe = symbols or cfg.symbols
    etfs = sorted({e for sym in universe if (e := cfg.sector_for(sym))})
    return list(dict.fromkeys([*universe, cfg.settings.data.benchmark, *etfs]))


def run_scan(cfg: Config, *, as_of: date | None = None, now: datetime | None = None,
             provider: PriceProvider | None = None, refresh: bool = False, symbols: list[str] | None = None,
             strategy_ids: list[str] | None = None) -> RunResult:
    started = now or datetime.now(sessions.ET)
    t0 = time.perf_counter()
    s = cfg.settings
    session, live = resolve_as_of(as_of, started, s)
    start = sessions.sessions_back(session, s.window_bars + 10)
    syms = all_symbols(cfg, symbols)
    dr = load_data(cfg, syms, start, session, started, refresh=refresh, live=live, provider=provider)
    frames = {k: v.completed for k, v in dr.data.items() if v.completed is not None}
    prov = {k: v.provisional for k, v in dr.data.items() if v.provisional is not None}
    res = scan(cfg, frames, session, provisional=prov, symbols=symbols, strategy_ids=strategy_ids,
               diagnostics=dr.diagnostics, requested_as_of=as_of)
    res.started = started
    res.runtime_s = time.perf_counter() - t0
    return res
