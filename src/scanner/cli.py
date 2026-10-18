"""Command-line interface (§10.3). Non-interactive; exit codes: 0 ok, 1 hard failure, 2 completed with warnings."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer

from scanner import sessions
from scanner.config import Config, load_config

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Technical Opportunity Scanner — daily-bar technical setups mapped to your strategy playbook.")
cache_app = typer.Typer(no_args_is_help=True, help="Inspect or refresh the local price cache.")
app.add_typer(cache_app, name="cache")

ConfigDir = Annotated[Path, typer.Option("--config-dir", "-c", help="Directory with settings/strategies/universe.")]
ReportsDir = Annotated[
    Path | None, typer.Option("--reports-dir", help="Output root (default: settings.report.reports_dir).")
]
AsOf = Annotated[
    str | None, typer.Option("--as-of", help="Replay as of YYYY-MM-DD (prior session if not a trading day).")
]
Now = Annotated[str | None, typer.Option("--now", hidden=True, help="Override the clock (ISO datetime, testing).")]


def _date(v: str | None, name: str) -> date | None:
    if v is None:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        typer.echo(f"error: {name} must be YYYY-MM-DD, got '{v}'", err=True)
        raise typer.Exit(1) from None


def _now(v: str | None) -> datetime | None:
    if v is None:
        return None
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=sessions.ET)


def _load(config_dir: Path, quiet: bool = False) -> Config:
    cfg, issues = load_config(config_dir)
    errors = [i for i in issues if i.severity == "error"]
    if errors or cfg is None:
        for i in issues:
            typer.echo(str(i), err=True)
        typer.echo(f"Configuration invalid ({len(errors)} error(s)). Run `scanner validate` for details.", err=True)
        raise typer.Exit(1)
    if not quiet:
        for i in issues:
            if i.severity == "warning":
                typer.echo(str(i), err=True)
    return cfg


def _symbols(values: list[str] | None) -> list[str] | None:
    return [v.strip().upper() for v in values] if values else None


@app.command()
def run(
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", "-s", help="Deep dive on symbol(s): all strategies, incl. non-matching rules."),
    ] = None,
    strategy: Annotated[
        list[str] | None, typer.Option("--strategy", help="Only evaluate these strategy id(s).")
    ] = None,
    as_of: AsOf = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Bypass the cache and re-download everything.")] = False,
    config_dir: ConfigDir = Path("config"),
    reports_dir: ReportsDir = None,
    now: Now = None,
) -> None:
    """Full scan as of the latest completed bar (or --as-of). Writes reports/<date>/scan.html and friends."""
    from scanner.pipeline import run_scan
    from scanner.report import write_outputs

    cfg = _load(config_dir)
    syms = _symbols(symbol)
    if strategy:
        unknown = [x for x in strategy if cfg.strategy(x) is None]
        if unknown:
            typer.echo(f"error: unknown strategy id(s): {', '.join(unknown)}", err=True)
            raise typer.Exit(1)
    res = run_scan(cfg, as_of=_date(as_of, "--as-of"), now=_now(now), refresh=refresh, symbols=syms,
                   strategy_ids=strategy)
    if not res.analyses:
        typer.echo("error: no symbols could be analysed:", err=True)
        for sym, why in res.skipped:
            typer.echo(f"  {sym}: {why}", err=True)
        for d in res.diagnostics:
            if d.severity == "error":
                typer.echo(f"  {d.symbol}: {d.message}", err=True)
        raise typer.Exit(1)
    paths = write_outputs(res, cfg, reports_dir=reports_dir, symbols=syms, strategy_ids=strategy)
    prov = " (PROVISIONAL bar — signals on it are PENDING_CLOSE)" if res.provisional else ""
    typer.echo(f"As of {res.as_of}{prov}: {len(res.analyses)} symbols, {len(res.candidates)} candidates, "
               f"{len(res.watchlist)} watch items, {len(res.skipped)} skipped, "
               f"{sum(d.severity != 'info' for d in res.diagnostics)} warnings · {res.runtime_s:.1f}s")
    for note in res.notes:
        typer.echo(note)
    for c in res.candidates[:10]:
        flag = " [pending]" if c.pending else ""
        typer.echo(f"  #{c.rank:<3} {c.symbol:<6} {c.strategy_name:<32} {c.score:5.1f}  "
                   f"{c.primary.summary if c.primary else '-'}{flag}")
    typer.echo(f"Report: {paths['html'].resolve()}")
    raise typer.Exit(res.exit_code)


@app.command()
def explain(
    symbol: Annotated[str, typer.Option("--symbol", "-s", help="Symbol to explain.")],
    strategy: Annotated[str, typer.Option("--strategy", help="Strategy id.")],
    as_of: AsOf = None,
    config_dir: ConfigDir = Path("config"),
    now: Now = None,
) -> None:
    """Print every rule evaluated for (symbol, strategy) with evidence and why it matched or not."""
    from scanner.pipeline import run_scan
    from scanner.scoring import score_candidate
    from scanner.strategies import evaluate_strategy, watch_nears

    cfg = _load(config_dir, quiet=True)
    st = cfg.strategy(strategy)
    if st is None:
        typer.echo(f"error: unknown strategy '{strategy}'", err=True)
        raise typer.Exit(1)
    sym = symbol.strip().upper()
    res = run_scan(cfg, as_of=_date(as_of, "--as-of"), now=_now(now), symbols=[sym], strategy_ids=[strategy])
    sa = res.analyses.get(sym)
    if sa is None:
        typer.echo(f"error: {sym} could not be analysed: {dict(res.skipped).get(sym, 'unknown reason')}", err=True)
        raise typer.Exit(1)
    s = cfg.settings
    from scanner.pipeline import overrides_key
    sigs = sa.signal_variants.get(overrides_key(st.technical.overrides), sa.signals)
    ev = evaluate_strategy(st, sa.trend.state, sigs, sa.flow, s, sa.provisional)
    t = sa.last_idx
    out = typer.echo
    verdict = {"true": "MATCH", "false": "NO MATCH", "PENDING_CLOSE": "PENDING_CLOSE (depends on the forming bar)"}
    out(f"{sym} × {st.name} ({st.id}) — as of {sa.as_of}{' (provisional bar)' if sa.provisional else ''}")
    if not st.active:
        out("NOTE: strategy is disabled; evaluated anyway for explanation.")
    out(f"Result: {verdict[ev.status.value]}")
    out(f"Close {sa.bars.close[t]:.2f} · ATR {sa.bars.atr[t]:.2f} · RSI {sa.bars.rsi[t]:.1f}")
    out("")
    out(f"Trend: {sa.trend.state} — {sa.trend.reason}")
    for p in sa.trend.pivots_used:
        out(f"  pivot {p.kind:<4} {p.date} {p.price:.2f} (confirmed {sa.bars.dates[p.confirm_idx]})")
    if sa.flow:
        out("Flow: " + "; ".join(f"{k}: {v}" for k, v in sa.flow.components()) + f" → {ev.flow_label} for {st.bias}")
    out("")
    out("Rules:")
    for r in ev.rules:
        out(f"  [{r.status.value:^13}] {r.rule}: {r.detail}")
        for e in r.evidence:
            out(f"                  {e}")
    out("")
    out(f"Signals on the evaluated bars (age 0..{s.signals.max_signal_age_bars}):")
    if not sigs:
        out("  none")
    for sg in sigs:
        out(f"  {sg.status.value:<13} {sg.summary} on {sg.date}")
        out(f"      {json.dumps(_jsonable(sg.evidence), sort_keys=True)}")
    out("")
    out("Nearest levels:")
    for row in sa.nearest_resistance[::-1] + sa.nearest_support:
        lv = row["level"]
        lo, hi = lv.zone(t, sa.bars.atr, s.levels.zone_atr_mult)
        out(f"  {lv.id:<10} {lv.label:<18} {row['value']:>10.2f}  zone {lo:.2f}–{hi:.2f}  {row['dist_atr']:+.2f} ATR"
            f"  touches {lv.touches}")
    nears = watch_nears(ev, sa.near)
    if nears:
        out("")
        out("Near-misses (watch triggers):")
        for nr in nears:
            out(f"  {nr.signal}: {nr.description} ({nr.distance_atr:.2f} ATR)")
            for a in nr.triggers:
                out(f"      {a.label}: {a.price:.2f} — {a.meaning}")
    if ev.status.value != "false":
        c = score_candidate(ev, sa, s)
        out("")
        out(f"Score {c.score:.1f}:")
        for x in c.components:
            out(f"  {x.name:<14} raw {x.raw:.2f} × {x.weight:g} = {x.points:5.1f}   {x.note}")
        out("Alert helper:")
        for a in c.alerts:
            out(f"  {a.label:<20} {a.price:>10.2f}  {a.meaning}")
    raise typer.Exit(0)


def _jsonable(x):
    from scanner.report import _clean
    return _clean(x)


@app.command()
def replay(
    start: Annotated[str, typer.Option("--start", help="First date (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--end", help="Last date (YYYY-MM-DD).")],
    config_dir: ConfigDir = Path("config"),
    reports_dir: ReportsDir = None,
    now: Now = None,
) -> None:
    """Run every trading day in [start, end] and write signals_log.csv (with forward outcomes)."""
    from scanner.replay import run_replay, write_signal_log

    cfg = _load(config_dir)
    d0, d1 = _date(start, "--start"), _date(end, "--end")
    if d1 < d0:
        typer.echo("error: --end is before --start", err=True)
        raise typer.Exit(1)
    rr = run_replay(cfg, d0, d1, now=_now(now))
    if not rr.days:
        typer.echo("error: no completed trading days in range", err=True)
        raise typer.Exit(1)
    root = Path(reports_dir or cfg.settings.report.reports_dir)
    out_dir = root / f"replay_{d0.isoformat()}_{d1.isoformat()}"
    path = write_signal_log(rr, out_dir, cfg.settings.replay.forward_horizons)
    manifest = {"command": "replay", "start": d0.isoformat(), "end": d1.isoformat(), "days": len(rr.days),
                "rows": len(rr.rows), "runtime_s": round(rr.runtime_s, 2), "hashes": cfg.hashes,
                "diagnostics": [{"symbol": d.symbol, "stage": d.stage, "severity": d.severity, "message": d.message}
                                for d in rr.diagnostics]}
    (out_dir / "run.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    typer.echo(f"Replayed {len(rr.days)} sessions ({rr.days[0]} → {rr.days[-1]}): {len(rr.rows)} signals in "
               f"{rr.runtime_s:.1f}s")
    typer.echo(f"Signal log: {path.resolve()}")
    raise typer.Exit(2 if any(d.severity != "info" for d in rr.diagnostics) else 0)


@app.command("check-reads")
def check_reads_cmd(
    config_dir: ConfigDir = Path("config"),
    reports_dir: ReportsDir = None,
    now: Now = None,
) -> None:
    """Compare replayed scanner output with config/expected_reads.csv."""
    from scanner.replay import check_reads, write_read_checks

    cfg = _load(config_dir)
    if not cfg.expected_reads:
        typer.echo("No expected reads: create config/expected_reads.csv (date,symbol,expected,note; "
                   "separate multiple expected items with ';').", err=True)
        raise typer.Exit(1)
    checks = check_reads(cfg, now=_now(now))
    n_match = sum(1 for c in checks if not c.misses)
    for c in checks:
        status = "OK  " if not c.misses else "MISS"
        typer.echo(f"{status} {c.read.date} {c.read.symbol:<6} expected [{', '.join(c.read.expected)}]"
                   f"  matches [{', '.join(c.matches)}]  misses [{', '.join(c.misses)}]"
                   f"  extras [{', '.join(c.extras)}]")
        for e in c.evidence:
            typer.echo(f"       {e}")
    path = write_read_checks(checks, Path(reports_dir or cfg.settings.report.reports_dir) / "check_reads.csv")
    typer.echo(f"{n_match}/{len(checks)} reads fully matched · details: {path.resolve()}")
    raise typer.Exit(0)


@app.command()
def validate(
    config_dir: ConfigDir = Path("config"),
    offline: Annotated[bool, typer.Option("--offline", help="Skip the unknown-ticker check (no network).")] = False,
) -> None:
    """Lint universe, sector map, strategies, settings and markets.yaml; list unknown tickers."""
    from scanner.markets.config import MARKETS, load_markets

    cfg, issues = load_config(config_dir)
    if (Path(config_dir) / MARKETS).exists():  # the Markets page's lists (optional file)
        issues += load_markets(config_dir, cfg.settings if cfg else None)[1]
    for i in issues:
        typer.echo(str(i))
    errors = [i for i in issues if i.severity == "error"]
    if errors or cfg is None:
        typer.echo(f"✗ {len(errors)} error(s), {len(issues) - len(errors)} warning(s)")
        raise typer.Exit(1)
    unknown: list[str] = []
    if not offline:
        unknown = _unknown_tickers(cfg)
        for sym in unknown:
            typer.echo(f"ERROR universe: {sym}: no yfinance data (unknown ticker?)")
    active = [s for s in cfg.strategies if s.active]
    typer.echo(f"{'✗' if unknown else '✓'} {len(cfg.symbols)} symbols, {len(cfg.sector_map)} sector mappings, "
               f"{len(active)}/{len(cfg.strategies)} strategies active, {len(issues)} warning(s)"
               + (f", {len(unknown)} unknown ticker(s)" if unknown else ""))
    raise typer.Exit(1 if unknown else (2 if issues else 0))


def _unknown_tickers(cfg: Config) -> list[str]:
    from scanner.data import ParquetCache, YFinanceProvider
    from scanner.pipeline import all_symbols

    s = cfg.settings
    cache = ParquetCache(Path(s.data.cache_dir), s.data.price_adjustment)
    syms = [x for x in all_symbols(cfg) if cache.load(x) is None]
    if not syms:
        return []
    now = datetime.now(sessions.ET)
    end = sessions.latest_completed_session(now)
    start = sessions.sessions_back(end, 10)
    got = YFinanceProvider(s.data.price_adjustment, s.data.batch_size, 1).fetch(syms, start, end)
    return sorted(x for x, r in got.items() if not hasattr(r, "empty") or r.empty)


@cache_app.command("info")
def cache_info(config_dir: ConfigDir = Path("config")) -> None:
    """Show cached symbols, date ranges and last fetch times."""
    from scanner.data import ParquetCache

    cfg = _load(config_dir, quiet=True)
    s = cfg.settings
    cache = ParquetCache(Path(s.data.cache_dir), s.data.price_adjustment)
    meta = cache.meta()
    typer.echo(f"Cache: {cache.dir.resolve()} ({len(meta)} symbols)")
    for sym in sorted(meta):
        m = meta[sym]
        typer.echo(f"  {sym:<8} {m.get('first')} → {m.get('last')}  {m.get('bars', 0):>5} bars  "
                   f"fetched {m.get('fetched_at')}")
    raise typer.Exit(0)


@cache_app.command("refresh")
def cache_refresh(config_dir: ConfigDir = Path("config"), now: Now = None) -> None:
    """Re-download full history for the universe, sector ETFs and benchmark."""
    from scanner.pipeline import all_symbols, load_data

    cfg = _load(config_dir, quiet=True)
    s = cfg.settings
    t = _now(now) or datetime.now(sessions.ET)
    end = sessions.latest_completed_session(t, s.data.close_buffer_minutes)
    start = sessions.sessions_back(end, s.window_bars + 10)
    dr = load_data(cfg, all_symbols(cfg), start, end, t, refresh=True)
    ok = [k for k, v in dr.data.items() if v.completed is not None]
    typer.echo(f"Refreshed {len(ok)}/{len(dr.data)} symbols through {end}.")
    for d in dr.diagnostics:
        typer.echo(f"  {d.severity}: {d.symbol}: {d.message}")
    raise typer.Exit(0 if len(ok) == len(dr.data) else 2)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    app()


if __name__ == "__main__":
    main()
