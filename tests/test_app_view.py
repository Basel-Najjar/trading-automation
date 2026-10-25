"""View model for the Streamlit app: scan lists, filtering, overview, setup pane, chart, descriptions."""

from __future__ import annotations

from datetime import date

import pytest

from scanner.app_view import (
    best_scan,
    card_tooltip,
    chart_overlays,
    detail_view,
    humanize,
    listed_scans,
    overlay_choices,
    overview,
    price_ladder,
    scan_lists,
    scan_recipe,
    split_summary,
    symbol_chart,
    ticker_description,
    ticker_scans,
)
from scanner.config import Criterion, Scan
from scanner.data import Profile
from scanner.pipeline import scan
from scanner.plotly_chart import FADED

D = date(2026, 9, 30)


@pytest.fixture
def res(cfg, market_frames):
    return scan(cfg, market_frames, D)


@pytest.fixture
def listed(cfg, market_frames):
    """A run where bullish lists every ticker (min 0%) and bearish none (it needs two trend states at once)."""
    cfg.scans["bullish"].min_pct = 0
    cfg.scans["bearish"] = Scan(min_pct=100, criteria=[Criterion(trend=["downtrend"]), Criterion(trend=["range"])])
    return scan(cfg, market_frames, D)


def test_scan_lists_and_filters(listed):
    res = listed
    groups = scan_lists(res)
    assert list(groups) == ["bullish", "non_bearish", "neutral", "non_bullish", "bearish"]
    assert {r.symbol for r in groups["bullish"]} == set(res.analyses)
    assert scan_lists(res, [], []) == groups
    sym = sorted(res.analyses)[0]
    assert all(r.symbol == sym for rows in scan_lists(res, [sym]).values() for r in rows)
    sectors = {res.analyses[r.symbol].sector_etf for r in groups["bullish"]} - {None}
    if sectors:
        etf = sorted(sectors)[0]
        assert {res.analyses[r.symbol].sector_etf for r in scan_lists(res, sectors=[etf])["bullish"]} == {etf}


def test_ticker_scans_listed_and_best(listed):
    res = listed
    for sym in res.analyses:
        rows = ticker_scans(res, sym)
        assert [r.scan for r in rows] == res.scans
        assert listed_scans(res, sym) == [r.scan for r in rows if r.passed]
        best = best_scan(res, sym)
        assert best in listed_scans(res, sym)  # bullish lists every ticker
        assert next(r for r in rows if r.scan == best).pct == max(r.pct for r in rows if r.passed)


def test_humanize():
    assert humanize("not_countertrend") == "Not against flow"
    assert humanize("with_flow") == "With flow"
    assert humanize("support_hold") == "Support hold"
    assert humanize("") == "n/a" and humanize(None) == "n/a"


def test_overview_counts_and_sectors(listed, cfg):
    res = listed
    ov = overview(res, cfg)
    assert ov["counts"] == {n: len(rows) for n, rows in res.lists.items()}
    names = {r.symbol for rows in res.lists.values() for r in rows}
    assert ov["n_listed"] == len(names) and ov["n_symbols"] == len(res.analyses)
    for e in ov["sectors"]:
        assert e["setups"] == sum(1 for m in e["members"] if m in names)


def test_price_ladder_sorted_with_close():
    ladder = price_ladder([("Invalidation", 95.0, "x"), ("Level", 98.0, "y"), ("Level", 98.0, "dup"),
                           ("Zone high", 101.0, "z")], 100.0)
    assert [r["price"] for r in ladder] == [101.0, 100.0, 98.0, 95.0]
    assert [r["kind"] for r in ladder] == ["zone", "close", "level", "invalidation"]
    assert ladder[-1]["dist_pct"] == pytest.approx(-5.0)


def test_scan_recipe(cfg):
    recipe = scan_recipe(cfg.scans["bullish"])
    assert len(recipe) == len(cfg.scans["bullish"].criteria)
    assert recipe[:3] == ["Trend: uptrend ×2", "2-bar close through resistance ×2", "Not extended (RSI below 70)"]
    assert scan_recipe(cfg.scans["neutral"])[0] == "Horizontal channel, 10 bars ×2"


def test_symbol_chart_is_shared_by_all_scans(res):
    for sym in res.analyses:
        fig = symbol_chart(res, sym)
        types = [t.type for t in fig.data]
        assert types.count("candlestick") == (2 if res.analyses[sym].provisional else 1)
        assert any(t.name == "RSI" for t in fig.data)
        assert fig.layout.uirevision == sym  # zoom survives reruns
        # nothing scan-specific: no faded levels, no invalidation line
        assert all(t.opacity != FADED for t in fig.data)
        assert not any(sh.line.dash == "dot" and sh.yref == "y2" for sh in fig.layout.shapes)


def test_detail_view_for_every_pair(res):
    for sym in res.analyses:
        for r in ticker_scans(res, sym):
            d = detail_view(res, sym, r.scan)
            assert d["result"] is r and len(d["rules"]) == r.total
            assert [(x.check, x.label, x.weight) for x in d["rules"]] == [(c.check, c.label, c.weight)
                                                                         for c in r.criteria]
            assert all(x.summary and "['" not in x.summary for x in d["rules"])  # no Python list reprs
            assert sum(1 for x in d["ladder"] if x["kind"] == "close") == 1


def test_ticker_description_priority(cfg):
    u = cfg.universe[0]
    u.notes = ""
    acme = Profile("Acme Corp", "Makes things.")
    assert ticker_description(cfg, {}, u.symbol) is None
    assert ticker_description(cfg, {u.symbol: Profile()}, u.symbol) is None
    assert ticker_description(cfg, {u.symbol: acme}, u.symbol) == ("Acme Corp", "Makes things.")
    u.notes = "  Acme (my note)  "  # your note names the ticker; the summary still comes from the profile
    assert ticker_description(cfg, {u.symbol: acme}, u.symbol) == ("Acme (my note)", "Makes things.")
    assert ticker_description(cfg, {}, u.symbol) == ("Acme (my note)", "")


def test_card_tooltip_and_summary_split():
    tip = card_tooltip(("Acme", "First sentence here. " * 40))
    assert tip.startswith("**Acme**") and len(tip) < 330 and tip.endswith(".")
    assert card_tooltip(None) is None
    assert card_tooltip(("", "Short note")) == "Short note"
    assert split_summary("One. Two! Three? Four.") == ("One. Two!", "Three? Four.")
    assert split_summary("Only one.") == ("Only one.", "")


def test_overlay_choices_and_alignment(res):
    for sym, sa in res.analyses.items():
        opts = overlay_choices(res, sym)
        if sym == res.benchmark:
            assert "benchmark" not in opts and "ratio" not in opts
        if "ratio" in opts:
            assert {"etf", "benchmark"} <= set(opts)
            assert opts["ratio"] == f"{sa.sector_etf}/{res.benchmark}"
        ovs = chart_overlays(res, sym, list(opts))
        expected = {"etf": sa.sector_etf, "benchmark": res.benchmark, "ratio": opts.get("ratio")}
        assert [o.label for o in ovs] == [expected[k] for k in ("etf", "benchmark", "ratio") if k in opts]
        for o in ovs:
            assert len(o.values) == len(sa.bars.dates)
        if "ratio" in opts:
            etf, bench, ratio = ovs
            t = sa.last_idx
            assert ratio.values[t] == pytest.approx(etf.values[t] / bench.values[t])
        fig = symbol_chart(res, sym)
        assert {t.name for t in fig.data} >= {o.label for o in ovs}


def test_some_symbol_offers_all_three_overlays(res):
    assert any(set(overlay_choices(res, s)) == {"etf", "benchmark", "ratio"} for s in res.analyses)


def _scanner_script():
    from scanner import scanner_page

    scanner_page.render()


def test_switching_scan_opens_its_top_ticker(listed, cfg, config_dir):
    from streamlit.testing.v1 import AppTest

    from scanner import scanner_page

    res = listed
    top = {n: (rows[0].symbol if rows else None) for n, rows in res.lists.items()}
    old = scanner_page.CONFIG_DIR
    scanner_page.CONFIG_DIR = config_dir
    try:
        at = AppTest.from_function(_scanner_script, default_timeout=120)
        at.session_state["res"], at.session_state["cfg"], at.session_state["profiles"] = res, cfg, {}
        at.run()
        assert not at.exception, at.exception
        assert at.session_state["f_direction"] == "bullish" and at.session_state["selected_symbol"] == top["bullish"]
        for group in ("bearish", "non_bullish", "neutral", "non_bearish", "bullish"):
            at.segmented_control(key="f_direction").set_value(group).run()
            assert not at.exception, (group, at.exception)
            if top[group]:
                assert at.session_state["selected_symbol"] == top[group], group
            else:
                assert "selected_symbol" not in at.session_state, group
    finally:
        scanner_page.CONFIG_DIR = old


def test_criteria_tab_renders(cfg, config_dir):
    from streamlit.testing.v1 import AppTest

    from scanner import scanner_page

    old = scanner_page.CONFIG_DIR
    scanner_page.CONFIG_DIR = config_dir
    try:
        at = AppTest.from_function(_scanner_script, default_timeout=120)
        at.session_state["main_tab"] = "Criteria"
        at.run()
        assert not at.exception, at.exception
    finally:
        scanner_page.CONFIG_DIR = old
