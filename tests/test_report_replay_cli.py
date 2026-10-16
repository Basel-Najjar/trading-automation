"""T-7 report rendering, replay/signal log (RP-3/4), check-reads (VR-2), CLI exit codes, docs coverage."""

from __future__ import annotations

import io
import json
import re
import tokenize
from datetime import date, datetime

import pandas as pd
import pytest
from typer.testing import CliRunner

from conftest import ROOT, FakeProvider
from scanner import cli, pipeline, sessions
from scanner.config import ExpectedRead
from scanner.models import SIGNAL_NAMES
from scanner.pipeline import scan
from scanner.replay import check_reads, forward_outcomes, log_rows, run_replay, write_signal_log
from scanner.report import render_html, write_outputs

NOW = datetime(2026, 9, 30, 18, 0, tzinfo=sessions.ET)
SECTIONS = ["summary", "market", "shortlist", "cards", "watch", "diagnostics"]


def _busy_day(cfg, frames):
    for d in sessions.sessions_between(date(2026, 3, 2), date(2026, 9, 30)):
        res = scan(cfg, frames, d, quality=False)
        if len(res.candidates) >= 2:
            return res
    pytest.skip("no day with candidates in synthetic data")


def _assert_self_contained(html: str):
    assert not re.search(r'(src|href)\s*=\s*"(https?:)?//', html), "external resource reference found"
    assert "<link " not in html and "@import" not in html
    for sid in SECTIONS:
        assert f'id="{sid}"' in html
    assert "not investment advice" in html


def test_report_many_candidates(cfg, market_frames):
    res = _busy_day(cfg, market_frames)
    html = render_html(res, cfg)
    _assert_self_contained(html)
    for c in res.candidates:
        assert f'id="card-{c.symbol}"' in html
    assert "<svg" in html and 'type="checkbox"' in html and "data-copy" in html


def test_report_zero_candidates_and_missing_sector(cfg, market_frames):
    res = scan(cfg, market_frames, date(2026, 6, 15), strategy_ids=["ted_calendar"])
    assert res.candidates == []
    html = render_html(res, cfg)
    _assert_self_contained(html)
    assert "No candidates matched" in html
    frames = {k: v for k, v in market_frames.items() if k not in ("SPY", "XLK")}
    html = render_html(scan(cfg, frames, date(2026, 6, 15)), cfg)
    _assert_self_contained(html)
    assert "Benchmark data unavailable" in html


def test_deep_dive_shows_all_strategies(cfg, market_frames):
    res = scan(cfg, market_frames, date(2026, 6, 15), symbols=["CCC"])
    html = render_html(res, cfg, deep=True)
    assert 'id="card-CCC"' in html
    assert html.count('class="strategy"') == len(res.strategies_enabled)


def test_write_outputs_and_determinism(cfg, market_frames, tmp_path):
    res = _busy_day(cfg, market_frames)
    p1 = write_outputs(res, cfg, reports_dir=tmp_path / "r1")
    p2 = write_outputs(res, cfg, reports_dir=tmp_path / "r2")
    assert p1["json"].read_bytes() == p2["json"].read_bytes()
    assert p1["csv"].read_bytes() == p2["csv"].read_bytes()
    man = json.loads(p1["manifest"].read_text(encoding="utf-8"))
    assert man["counts"]["candidates"] == len(res.candidates) and "settings_effective" in man["hashes"]
    assert set(man["data_fingerprints"]) >= {"AAA", "SPY"}
    tv = p1["tradingview"].read_text(encoding="utf-8").splitlines()
    assert tv[0] == "# shortlist" and res.candidates[0].symbol in tv[1]
    assert (p1["html"].parent / "config_snapshot" / "strategies.yaml").exists()
    assert p1["html"].parent.name == res.as_of.isoformat()


def test_replay_matches_per_day_scans(cfg, market_frames):
    prov = FakeProvider(market_frames)
    rr = run_replay(cfg, date(2026, 6, 1), date(2026, 6, 30), now=NOW, provider=prov)
    assert len(rr.days) == len(sessions.sessions_between(date(2026, 6, 1), date(2026, 6, 30)))
    expected = []
    for d in rr.days:
        expected += log_rows(scan(cfg, market_frames, d, quality=False))
    forward_outcomes(expected, market_frames, cfg.settings.replay.forward_horizons)
    expected.sort(key=lambda r: (r["date"], r["symbol"], r["signal"], r["level_id"]))
    assert rr.rows == expected and rr.rows
    row = rr.rows[0]
    assert row["fwd_ret_3"] != "" and row["mfe_atr_20"] != ""
    df = market_frames[row["symbol"]]
    pos = df.index.get_indexer([pd.Timestamp(row["date"])])[0]
    assert abs(row["fwd_ret_5"] - round(df["close"].iloc[pos + 5] / df["close"].iloc[pos] - 1, 5)) < 1e-9


def test_replay_rows_ignore_future_bars(cfg, market_frames):
    rr = run_replay(cfg, date(2026, 6, 1), date(2026, 6, 12), now=NOW, provider=FakeProvider(market_frames))
    cut = {k: v[v.index <= pd.Timestamp("2026-06-12")] for k, v in market_frames.items()}
    rows = []
    for d in rr.days:
        rows += log_rows(scan(cfg, cut, d, quality=False))
    base = [{k: v for k, v in r.items() if not k.startswith(("fwd_", "mfe_", "mae_"))} for r in rr.rows]
    rows.sort(key=lambda r: (r["date"], r["symbol"], r["signal"], r["level_id"]))
    assert base == rows


def test_signal_log_file(cfg, market_frames, tmp_path):
    rr = run_replay(cfg, date(2026, 6, 1), date(2026, 6, 10), now=NOW, provider=FakeProvider(market_frames))
    p = write_signal_log(rr, tmp_path, cfg.settings.replay.forward_horizons)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert "NOT a backtest" in lines[0]
    assert lines[1].startswith("date,symbol,signal") and "mfe_atr_20" in lines[1]


def test_check_reads(cfg, market_frames):
    d = date(2026, 6, 15)
    res = scan(cfg, market_frames, d)
    sa = res.analyses["AAA"]
    fired = sorted({s.name for s in sa.signals if s.age == 0})
    cfg.expected_reads = [
        ExpectedRead(d, "AAA", [sa.trend.state, *fired[:1]]),
        ExpectedRead(d, "AAA", ["support_break" if "support_break" not in fired else "resistance_break"]),
    ]
    checks = check_reads(cfg, now=NOW, provider=FakeProvider(market_frames))
    assert checks[0].misses == [] and sa.trend.state in checks[0].matches
    assert len(checks[1].misses) == 1 and sa.trend.state in checks[1].extras


# ---------------------------------------------------------------- CLI


@pytest.fixture
def fake_net(monkeypatch, market_frames):
    prov = FakeProvider(market_frames)
    monkeypatch.setattr(pipeline, "YFinanceProvider", lambda *a, **k: prov)
    return prov


def test_cli_validate_exit_codes(config_dir):
    r = CliRunner().invoke(cli.app, ["validate", "--offline", "-c", str(config_dir)])
    assert r.exit_code == 2 and "CCC" in r.output  # unmapped symbol warning
    (config_dir / "sector_map.csv").write_text("symbol,sector_etf\nAAA,XLK\nBBB,XLK\nCCC,XLK\n", encoding="utf-8")
    assert CliRunner().invoke(cli.app, ["validate", "--offline", "-c", str(config_dir)]).exit_code == 0
    p = config_dir / "strategies.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("resistance_break", "resistence_break", 1), encoding="utf-8")
    r = CliRunner().invoke(cli.app, ["validate", "--offline", "-c", str(config_dir)])
    assert r.exit_code == 1 and "did you mean 'resistance_break'" in r.output


def test_cli_run_explain_replay(config_dir, fake_net, tmp_path):
    runner = CliRunner()
    r = runner.invoke(cli.app, ["run", "-c", str(config_dir), "--now", "2026-09-30T18:00:00", "--as-of", "2026-06-15"])
    assert r.exit_code in (0, 2), r.output
    out = tmp_path / "reports" / "2026-06-15"
    assert (out / "scan.html").exists() and (out / "run.json").exists()
    r = runner.invoke(cli.app, ["run", "-c", str(config_dir), "--now", "2026-09-30T18:00:00", "--as-of", "2026-06-15",
                                "--symbol", "aaa"])
    assert (out / "scan_AAA.html").exists(), r.output
    r = runner.invoke(cli.app, ["explain", "-c", str(config_dir), "--now", "2026-09-30T18:00:00", "--symbol", "AAA",
                                "--strategy", "bull_put_spread", "--as-of", "2026-06-15"])
    assert r.exit_code == 0, r.output
    assert "Rules:" in r.output and "trend" in r.output and "Result:" in r.output
    r = runner.invoke(cli.app, ["run", "-c", str(config_dir), "--strategy", "nope"])
    assert r.exit_code == 1
    r = runner.invoke(cli.app, ["replay", "-c", str(config_dir), "--now", "2026-09-30T18:00:00",
                                "--start", "2026-06-01", "--end", "2026-06-05"])
    assert r.exit_code in (0, 2), r.output
    assert (tmp_path / "reports" / "replay_2026-06-01_2026-06-05" / "signals_log.csv").exists()


def test_cli_bad_config_exits_1(config_dir):
    (config_dir / "strategies.yaml").write_text("- id: x\n  name: X\n  bias: sideways\n", encoding="utf-8")
    r = CliRunner().invoke(cli.app, ["run", "-c", str(config_dir)])
    assert r.exit_code == 1


# ---------------------------------------------------------------- docs + scope


def test_every_signal_is_documented():
    doc = (ROOT / "docs" / "signals.md").read_text(encoding="utf-8")
    for name in SIGNAL_NAMES:
        assert f"`{name}`" in doc, f"{name} missing from docs/signals.md"


def test_no_out_of_scope_data_paths():
    """Acceptance #9: no code path touches ORATS, option chains, fundamentals, earnings or macro data.

    Checks code tokens only (names/attributes, split on '_'), not strings or comments, and that yfinance is
    used solely for yf.download (daily OHLCV).
    """
    forbidden = {"orats", "options", "chain", "earnings", "earning", "fundamental", "fundamentals", "news",
                 "holders", "calendar_events", "financials", "recommendations"}
    for p in (ROOT / "src" / "scanner").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        names = {t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.NAME}
        allowed = {"orats_checklist"}  # display-only checklist text (SP-3), never evaluated
        bad = sorted(n for n in names - allowed if forbidden & set(n.lower().split("_")))
        assert not bad, (p.name, bad)
        assert set(re.findall(r"\byf\.(\w+)", src)) <= {"download"}, p.name
