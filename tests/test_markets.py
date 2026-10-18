"""Markets page: config, series (ratios, weekly bars, cache), structure, RRG, PCA, export and the page itself."""

from __future__ import annotations

import shutil
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import ROOT, FakeProvider, random_frame, session_index
from scanner import sessions
from scanner.config import Settings
from scanner.markets.config import MARKETS, MarketsConfig, load_markets, parse_series, save_markets, settings_for
from scanner.markets.export import export_name, render_markets_html
from scanner.markets.pca import _align, compute_pca
from scanner.markets.rrg import compute_rrg, quadrant, rrg_series
from scanner.markets.run import run_markets
from scanner.markets.series import (
    Prices,
    build_series,
    closes,
    load_cached,
    ratio_frame,
    refresh_prices,
    to_weekly,
)
from scanner.markets.structure import analyze_all, detail_figure, summary_rows
from scanner.pipeline import analyze_symbol

END = date(2026, 9, 30)  # a Wednesday
AFTER_CLOSE = datetime(2026, 9, 30, 17, 0, tzinfo=sessions.ET)
SYMS = ["SPY", "QQQ", "TLT", "GLD", "XLK", "XLE"]


def _mc(**over) -> MarketsConfig:
    data = {"levels": ["SPY", "XLK/SPY"], "rrg": {"benchmark": "SPY", "members": ["XLK", "XLE", "QQQ/TLT"]},
            "pca": {"members": ["SPY", "TLT", "GLD", "XLK/SPY"]}} | over
    return MarketsConfig.model_validate(data)


@pytest.fixture(scope="module")
def frames() -> dict[str, pd.DataFrame]:
    return {s: random_frame(2100, seed=10 + i, end=END, drift=0.0002 * (i - 2)) for i, s in enumerate(SYMS)}


@pytest.fixture
def mdir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    shutil.copyfile(ROOT / "config" / MARKETS, d / MARKETS)
    text = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    text = text.replace("cache_dir: cache ", f"cache_dir: {(tmp_path / 'cache').as_posix()} ")
    (d / "settings.yaml").write_text(text, encoding="utf-8")
    return d


# ----------------------------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------------------------


def test_repo_markets_yaml_is_valid():
    mc, issues = load_markets(ROOT / "config")
    assert mc is not None, [str(i) for i in issues]
    assert "SPY" in mc.symbols and "XLY" in mc.symbols and "XLP" in mc.symbols  # ratio legs included


def test_parse_series():
    assert parse_series(" xlk / spy ").label == "XLK/SPY"
    assert parse_series("spy").den is None
    for bad in ("A/B/C", "A/A", "/SPY", ""):
        with pytest.raises(ValueError):
            parse_series(bad)


@pytest.mark.parametrize("over, msg", [
    ({"levels": ["SPY", "spy"]}, "duplicate"),
    ({"levels": []}, "at least one"),
    ({"rrg": {"benchmark": "SPY", "members": ["SPY", "XLK"]}}, "cannot also be a member"),
    ({"rrg": {"benchmark": "XLK/SPY", "members": ["XLE"]}}, "not a ratio"),
    ({"pca": {"members": ["SPY"]}}, "at least two"),
])
def test_config_rejects(over, msg):
    with pytest.raises(ValueError, match=msg):
        _mc(**over)


def test_missing_resolutions_use_defaults_and_partial_ones_merge():
    mc = _mc(resolutions={"daily": {"display_years": 1, "history_years": 3}})
    assert mc.params("daily").history_years == 3 and mc.params("daily").pca.window == 63
    assert mc.params("weekly").settings["pivots"]["pivot_window"] == 3
    assert _mc().params("daily").display_years == 1


def test_settings_for_deep_merges_over_scanner_settings():
    base = Settings()
    s = settings_for(base, _mc().params("weekly"))
    assert s.pivots.pivot_window == 3 and s.levels.recency_half_life_bars == 26
    assert s.levels.zone_atr_mult == base.levels.zone_atr_mult  # untouched keys come from settings.yaml


def test_bad_resolution_settings_are_reported(mdir: Path):
    p = mdir / MARKETS
    p.write_text(p.read_text(encoding="utf-8").replace("pivot_window: 3", "pivot_windw: 3"), encoding="utf-8")
    mc, issues = load_markets(mdir)
    assert mc is None and any("pivot_windw" in str(i) for i in issues)


def test_save_round_trip_keeps_resolutions_block(mdir: Path):
    issues = save_markets(mdir, ["spy", "qqq/spy", None, ""], "spy", ["XLK", "XLE"], ["SPY", "TLT"])
    assert not [i for i in issues if i.severity == "error"]
    text = (mdir / MARKETS).read_text(encoding="utf-8")
    assert "levels: [SPY, QQQ/SPY]" in text
    assert "# bar-count settings scaled for weekly bars" in text  # comments inside resolutions survive
    mc, _ = load_markets(mdir)
    assert mc.levels == ["SPY", "QQQ/SPY"] and mc.rrg.members == ["XLK", "XLE"]
    assert mc.params("weekly").pca.window == 52


def test_save_with_errors_writes_nothing(mdir: Path):
    before = (mdir / MARKETS).read_text(encoding="utf-8")
    issues = save_markets(mdir, ["SPY"], "SPY", ["SPY"], ["SPY", "TLT"])
    assert any(i.severity == "error" for i in issues)
    assert (mdir / MARKETS).read_text(encoding="utf-8") == before


# ----------------------------------------------------------------------------------------------
# Series
# ----------------------------------------------------------------------------------------------


def test_ratio_frame_is_exact_on_open_and_close(frames):
    r = ratio_frame(frames["XLK"], frames["SPY"])
    d = r.index[100]
    assert r.at[d, "close"] == pytest.approx(frames["XLK"].at[d, "close"] / frames["SPY"].at[d, "close"])
    assert r.at[d, "open"] == pytest.approx(frames["XLK"].at[d, "open"] / frames["SPY"].at[d, "open"])
    assert (r["high"] >= r[["open", "close"]].max(axis=1) - 1e-12).all()
    assert (r["low"] <= r[["open", "close"]].min(axis=1) + 1e-12).all()
    assert r["volume"].isna().all()


def test_weekly_bars_label_holiday_week_by_last_session():
    df = random_frame(60, end=date(2026, 4, 10))  # Good Friday 2026-04-03 is an NYSE holiday
    weeks, prov = to_weekly(df)
    assert prov is None  # the frame ends on a Friday: every week is complete
    assert pd.Timestamp(2026, 4, 2) in weeks.index
    wk = df.loc["2026-03-30":"2026-04-02"]
    row = weeks.loc[pd.Timestamp(2026, 4, 2)]
    assert row["open"] == wk["open"].iloc[0] and row["close"] == wk["close"].iloc[-1]
    assert row["high"] == wk["high"].max() and row["low"] == wk["low"].min()
    assert row["volume"] == wk["volume"].sum()


def test_in_progress_week_is_provisional():
    weeks, prov = to_weekly(random_frame(60, end=END))  # ends Wednesday
    assert prov is not None and prov.index[-1] == pd.Timestamp(END)
    assert weeks.index[-1] < pd.Timestamp(2026, 9, 28)


def test_build_series_skips_missing_and_short(frames):
    specs = _mc().specs(["SPY", "NOPE", "XLK/NOPE"])
    out, diags = build_series(specs, Prices(frames), "daily", 504)
    assert list(out) == ["SPY"] and len(out["SPY"].completed) == 504
    assert {d.symbol for d in diags} == {"NOPE", "XLK/NOPE"}
    short, diags = build_series(specs[:1], Prices({"SPY": frames["SPY"].tail(20)}), "daily", 504)
    assert not short and "skipped" in diags[0].message


def test_weekly_closes_align_on_friday(frames):
    out, _ = build_series(_mc().specs(["SPY", "TLT"]), Prices(frames), "weekly", 400)
    c = closes(out, weekly=True)
    assert set(c.index.dayofweek) == {4} and not c.isna().any().any()


def test_refresh_then_cache_only_load(mdir: Path, frames):
    from scanner.config import load_settings

    s = load_settings(mdir / "settings.yaml", [])
    mc = _mc()
    fake = FakeProvider(frames)
    dr = refresh_prices(mc, s, AFTER_CLOSE, provider=fake)
    assert fake.calls == 1 and all(v.completed is not None for v in dr.data.values())
    assert set(fake.requests[0][0]) == set(mc.symbols)
    span = len(sessions.sessions_between(fake.requests[0][1], END))
    assert span > 8 * 252  # one refresh covers the weekly history too
    p = load_cached(mc, s)
    assert set(p.frames) == set(mc.symbols) and p.as_of == END and p.fetched_at
    refresh_prices(mc, s, AFTER_CLOSE, provider=fake)
    assert fake.calls == 1  # up to date: no download


# ----------------------------------------------------------------------------------------------
# Structure
# ----------------------------------------------------------------------------------------------


def test_structure_is_the_scanner_engine(frames):
    s = Settings()
    out, _ = build_series(_mc().specs(["SPY", "XLK/SPY"]), Prices(frames), "daily", 504)
    analyses, diags = analyze_all(out, s)
    assert not [d for d in diags if d.severity == "error"]
    ref = analyze_symbol("SPY", out["SPY"].completed, s)
    assert analyses["SPY"].trend.state == ref.trend.state
    assert [x.name for x in analyses["SPY"].signals] == [x.name for x in ref.signals]
    rows = summary_rows(analyses, "daily")
    assert [r["Series"] for r in rows] == ["SPY", "XLK/SPY"]
    assert {"1m", "3m", "6m", "Trend", "Support", "Resistance", "Signal", "Near"} <= set(rows[0])


def test_weekly_structure_marks_forming_week_and_charts_without_session_breaks(frames):
    mc = _mc()
    s = settings_for(Settings(), mc.params("weekly"))
    out, _ = build_series(mc.specs(["SPY"]), Prices(frames), "weekly", 416)
    analyses, _ = analyze_all(out, s)
    sa = analyses["SPY"]
    assert sa.provisional and len(sa.bars) == 417
    fig = detail_figure(sa, s, "weekly", 260)
    assert not fig.layout.xaxis.rangebreaks
    assert [t.name for t in fig.data if t.type == "candlestick"] == ["Price", "Provisional bar"]


# ----------------------------------------------------------------------------------------------
# RRG
# ----------------------------------------------------------------------------------------------


def test_quadrants():
    assert quadrant(101, 101) == "Leading" and quadrant(101, 99) == "Weakening"
    assert quadrant(99, 99) == "Lagging" and quadrant(99, 101) == "Improving"
    assert quadrant(100, 100) == "Leading"


def test_rrg_strength_that_stalls_is_weakening():
    idx = session_index(120, END)
    rng = np.random.default_rng(3)
    bench = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 120))), index=idx)
    rel = np.r_[np.linspace(0, 0.6, 117), 0.6 - 0.004 * np.arange(1, 4)] + rng.normal(0, 0.002, 120)  # 3-bar stall
    member = bench * np.exp(rel)
    df = rrg_series(member, bench, 10)
    assert df["rs_ratio"].iloc[-1] > 100 and df["rs_mom"].iloc[-1] < 100
    r = compute_rrg(pd.DataFrame({"A": member, "SPY": bench}), "SPY", ["A", "B"], 10, 5)
    assert r.table[0]["Quadrant"] == "Weakening" and len(r.tails["A"]) == 6
    assert r.skipped == [("B", "no prices")]


# ----------------------------------------------------------------------------------------------
# PCA
# ----------------------------------------------------------------------------------------------


def _regime_closes(n: int = 400, switch: int = 340) -> pd.DataFrame:
    """Four independent series; from bar `switch` on (the last 60-bar window) A and B share a common factor."""
    rng = np.random.default_rng(7)
    e = rng.normal(0, 0.01, (n, 4))
    f = rng.normal(0, 0.01, n)
    e[switch:, 0] += 1.5 * f[switch:]
    e[switch:, 1] += 1.5 * f[switch:]
    return pd.DataFrame(100 * np.exp(np.cumsum(e, axis=0)), index=session_index(n, END), columns=list("ABCD"))


def test_pca_sees_the_new_correlation():
    p = compute_pca(_regime_closes(), window=60, components=3)
    assert p.members == list("ABCD") and list(p.explained.columns) == ["PC1", "PC2", "PC3"]
    assert p.explained["PC1"].iloc[-1] > p.explained["PC1"].iloc[60] + 0.08  # theory: ~0.42 vs ~0.25-0.3
    assert p.avg_corr.iloc[-1] > p.avg_corr.iloc[60]
    top = p.pair_shifts(1)[0]
    assert top["Pair"] == "A · B" and top["Change"] > 0.5
    load = p.loadings_now["PC1"]
    assert abs(load["A"]) > 0.6 and abs(load["B"]) > 0.6 and np.sign(load["A"]) == np.sign(load["B"])
    assert p.corr_change is not None and p.corr_change.shape == (4, 4)


def test_pca_drops_short_series_and_needs_two():
    c = _regime_closes()
    c.loc[c.index[:300], "D"] = np.nan
    p = compute_pca(c, window=60, components=3)
    assert "D" not in p.members and any(m == "D" for m, _ in p.skipped)
    p1 = compute_pca(c[["A"]], window=60, components=3)
    assert p1.explained.empty and p1.skipped


def test_sign_alignment_follows_the_previous_window():
    ref = np.array([[0.7], [0.7]])
    assert (_align(-ref, ref) == ref).all()
    assert _align(np.array([[-0.6], [-0.8]]), None)[:, 0].sum() > 0


# ----------------------------------------------------------------------------------------------
# Run + export
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("res", ["daily", "weekly"])
def test_run_and_export(frames, res):
    r = run_markets(_mc(), Settings(), Prices(frames), res)
    assert set(r.analyses) == {"SPY", "XLK/SPY"} and not r.diagnostics
    assert set(r.rrg.tails) == {"XLK", "XLE", "QQQ/TLT"}
    assert r.pca.members == ["SPY", "TLT", "GLD", "XLK/SPY"]
    assert r.display_bars == (252 if res == "daily" else 260)
    html = render_markets_html(r)
    assert html.startswith("<!doctype html>") and "QQQ/TLT" in html and "Largest correlation shifts" in html
    assert html.count("plotly.js v") == 1  # embedded once, offline
    assert export_name(r) == f"markets_{END.isoformat()}_{res}.html"


# ----------------------------------------------------------------------------------------------
# Page (Streamlit AppTest, cache pre-filled, no network)
# ----------------------------------------------------------------------------------------------


def _page_script():
    from scanner.markets import page

    page.render()


def test_page_renders_every_tab(mdir: Path, frames):
    from streamlit.testing.v1 import AppTest

    from scanner.config import load_settings
    from scanner.markets import page

    s = load_settings(mdir / "settings.yaml", [])
    mc, _ = load_markets(mdir)
    extra = {sym: random_frame(2100, seed=50 + i, end=END) for i, sym in enumerate(mc.symbols) if sym not in frames}
    refresh_prices(mc, s, AFTER_CLOSE, provider=FakeProvider(frames | extra))
    old = page.CONFIG_DIR
    page.CONFIG_DIR = mdir
    try:
        at = AppTest.from_function(_page_script, default_timeout=120)
        at.run()
        assert not at.exception, at.exception
        assert at.dataframe  # the Structure table
        for tab in ("Rotation", "Correlation", "Lists"):
            at.session_state["mk_tab"] = tab
            at.run()
            assert not at.exception, (tab, at.exception)
        at.session_state["mk_res"] = "daily"
        at.session_state["mk_tab"] = "Structure"
        at.run()
        assert not at.exception, at.exception
    finally:
        page.CONFIG_DIR = old
