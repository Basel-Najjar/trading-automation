"""View model for the Streamlit app: % of rules met, ranking, Bullish/Bearish/Neutral grouping, overview, setup pane,
filtering."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from scanner.app_view import (
    StrategyRow,
    SymbolRow,
    bias_group,
    card_tooltip,
    chart_overlays,
    detail_view,
    direction,
    direction_groups,
    direction_setups,
    humanize,
    overlay_choices,
    overview,
    pane_rows,
    price_ladder,
    setup_summary,
    split_summary,
    strategy_recipe,
    strategy_rows,
    symbol_chart,
    symbol_rankings,
    ticker_description,
)
from scanner.data import Profile
from scanner.models import RuleResult, Tri
from scanner.pipeline import scan
from scanner.plotly_chart import FADED

D = date(2026, 9, 30)


@pytest.fixture
def res(cfg, market_frames):
    return scan(cfg, market_frames, D)


def test_fraction_counts_only_true_rules(res):
    for sym in res.analyses:
        for r in strategy_rows(res, sym):
            assert r.total == len(r.ev.rules)
            assert r.met == sum(x.status is Tri.TRUE for x in r.ev.rules)
            assert (r.cand is not None) == (r.ev.status is not Tri.FALSE)


def test_pending_counts_as_not_met(res):
    sym = next(iter(res.analyses))
    ev = res.evals[sym][0]
    ev.rules = [RuleResult("trend", Tri.TRUE, ""), RuleResult("signals_any", Tri.PENDING, "")]
    row = next(r for r in strategy_rows(res, sym) if r.ev is ev)
    assert (row.met, row.total, row.pending, row.fraction) == (1, 2, 1, 0.5)


def test_rankings_sorted_and_best_is_max(res):
    ranked = symbol_rankings(res)
    assert {r.symbol for r in ranked} == set(res.analyses)
    for r in ranked:
        assert r.best.fraction == max(x.fraction for x in r.rows)
        assert [x.fraction for x in r.rows] == sorted((x.fraction for x in r.rows), reverse=True)
    fr = [r.best.fraction for r in ranked]
    assert fr == sorted(fr, reverse=True)
    for c in res.candidates:
        if c.status is Tri.TRUE:
            assert next(r for r in ranked if r.symbol == c.symbol).best.fraction == 1.0


def _row(*specs: tuple[str, float | None]) -> SymbolRow:
    """A hand-built symbol row: one strategy per (bias, score); a None score is a strategy that did not match."""
    rows = []
    for n, (bias, score) in enumerate(specs):
        ev = SimpleNamespace(strategy=SimpleNamespace(id=f"s{n}", name=f"S{n} {bias}", bias=bias))
        cand = SimpleNamespace(score=score) if score is not None else None
        rows.append(StrategyRow(ev, 1, 2, 0, cand))
    return SymbolRow("X", rows, 100.0, 0.0, "range")


@pytest.mark.parametrize(("specs", "want"), [
    ([("bullish", 60.0), ("non_bearish", 50.0)], "Bullish"),
    ([("bearish", 60.0), ("non_bullish", None)], "Bearish"),
    ([("neutral", 40.0)], "Neutral"),
    ([("non_bearish", 70.0), ("neutral", 80.0)], "Bullish"),  # a neutral match beside one side keeps that side
    ([("non_bearish", 70.0), ("non_bullish", 65.0)], "Neutral"),  # both sides: two-sided, e.g. a range
    ([("bullish", None), ("bearish", None)], None),  # no match: not put in a group
])
def test_direction_from_matched_setups(specs, want):
    assert direction(_row(*specs)) == want


def test_bias_group():
    assert {b: bias_group(b) for b in ("bullish", "non_bearish", "bearish", "non_bullish", "neutral")} == {
        "bullish": "Bullish", "non_bearish": "Bullish", "bearish": "Bearish", "non_bullish": "Bearish",
        "neutral": "Neutral"}


def test_setup_summary_and_pane_order():
    row = _row(("non_bearish", 50.0), ("bullish", 70.0), ("neutral", 80.0), ("bearish", None))
    summary = setup_summary(row)
    assert list(summary) == ["Bullish", "Bearish", "Neutral"]
    assert [r.score for r in summary["Bullish"]] == [70.0, 50.0] and summary["Bearish"] == []
    assert [r.score for r in summary["Neutral"]] == [80.0]
    # the ticker's own direction first, then its other matches, then the strategies that did not match
    assert [r.ev.strategy.id for r in pane_rows(row)] == ["s1", "s0", "s2", "s3"]


@pytest.fixture
def matched(res):
    """`res` with stand-in candidates (the fixture market matches nothing): the first symbol matches a bullish and a
    bearish strategy, the second only bullish ones."""
    syms = sorted(res.analyses)
    by_bias = {}
    for ev in res.evals[syms[0]]:
        by_bias.setdefault(bias_group(ev.strategy.bias), ev.strategy.id)
    bull, bear = by_bias["Bullish"], by_bias["Bearish"]
    res.candidates = [SimpleNamespace(symbol=syms[0], strategy_id=bull, score=60.0),
                      SimpleNamespace(symbol=syms[0], strategy_id=bear, score=55.0),
                      SimpleNamespace(symbol=syms[1], strategy_id=bull, score=70.0)]
    return res


def test_direction_groups_partition_matched_tickers(matched):
    res = matched
    groups = direction_groups(res)
    syms = sorted(res.analyses)
    assert [r.symbol for r in groups["Neutral"]] == [syms[0]]  # bullish + bearish matches: two-sided
    assert [r.symbol for r in groups["Bullish"]] == [syms[1]] and groups["Bearish"] == []
    assert list(groups) == ["Bullish", "Bearish", "Neutral"]
    listed = [r.symbol for rows in groups.values() for r in rows]
    assert len(listed) == len(set(listed))  # each ticker in at most one group
    assert set(listed) == {c.symbol for c in res.candidates}  # only tickers with a match
    for g, rows in groups.items():
        assert all(direction(r) == g for r in rows)
        best = [direction_setups(r, g)[0].score for r in rows]
        assert best == sorted(best, reverse=True)


def test_direction_groups_filters(matched):
    res = matched
    groups = direction_groups(res)
    assert direction_groups(res, [], [], []) == groups
    first = next(r for rows in groups.values() for r in rows)
    sym, sid = first.symbol, first.matches[0].ev.strategy.id
    assert [r.symbol for rows in direction_groups(res, [sym]).values() for r in rows] == [sym]
    narrowed = [r for rows in direction_groups(res, strategy_ids=[sid]).values() for r in rows]
    assert narrowed and all(x.ev.strategy.id == sid for r in narrowed for x in r.rows)
    bull_only = direction_groups(res, strategy_ids=[res.candidates[0].strategy_id])  # the direction follows the filter
    assert {r.symbol for r in bull_only["Bullish"]} == {c.symbol for c in res.candidates}
    sectors = {r.sector for rows in groups.values() for r in rows if r.sector}
    if sectors:
        etf = sorted(sectors)[0]
        assert {r.sector for rows in direction_groups(res, sectors=[etf]).values() for r in rows} == {etf}


def test_humanize():
    assert humanize("non_bearish") == "Not bearish"
    assert humanize("with_flow") == "With flow"
    assert humanize("support_hold") == "Support hold"
    assert humanize("") == "n/a" and humanize(None) == "n/a"


def test_overview_counts_and_sectors(res, cfg):
    ov = overview(res, cfg)
    assert ov["n_candidates"] == len(res.candidates)
    assert ov["n_symbols_with"] == len({c.symbol for c in res.candidates})
    with_cand = {c.symbol for c in res.candidates}
    for e in ov["sectors"]:
        assert e["setups"] == sum(1 for m in e["members"] if m in with_cand)
    assert ov["directions"] == {g: len(rows) for g, rows in direction_groups(res).items()}
    assert sum(ov["directions"].values()) == ov["n_symbols_with"]


def test_price_ladder_sorted_with_close():
    ladder = price_ladder([("Invalidation", 95.0, "x"), ("Level", 98.0, "y"), ("Level", 98.0, "dup"),
                           ("Zone high", 101.0, "z")], 100.0)
    assert [r["price"] for r in ladder] == [101.0, 100.0, 98.0, 95.0]
    assert [r["kind"] for r in ladder] == ["zone", "close", "level", "invalidation"]
    assert ladder[-1]["dist_pct"] == pytest.approx(-5.0)


def test_strategy_recipe(cfg):
    for stg in cfg.strategies:
        terms = [t for t, _ in strategy_recipe(stg)]
        assert terms[0] == "Trend" and terms[-1] == "Flow"
        assert ("Requires" in terms) == bool(stg.technical.signals_all)
        assert ("Any of" in terms) == bool(stg.technical.signals_any)


def test_symbol_chart_is_shared_by_all_strategies(res):
    for sym in res.analyses:
        fig = symbol_chart(res, sym)
        types = [t.type for t in fig.data]
        assert types.count("candlestick") == (2 if res.analyses[sym].provisional else 1)
        assert any(t.name == "RSI" for t in fig.data)
        assert fig.layout.uirevision == sym  # zoom survives reruns
        # nothing strategy-specific: no faded levels, no invalidation line
        assert all(t.opacity != FADED for t in fig.data)
        assert not any(sh.line.dash == "dot" and sh.yref == "y2" for sh in fig.layout.shapes)


def test_detail_view_for_every_pair(res, cfg):
    for sym in res.analyses:
        for r in strategy_rows(res, sym):
            d = detail_view(res, cfg, sym, r.ev.strategy.id)
            assert len(d["rules"]) == r.total
            assert [x.rule for x in d["rules"]] == [x.rule for x in r.ev.rules]
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


def test_switching_direction_opens_the_groups_top_ticker(matched, cfg, config_dir):
    from streamlit.testing.v1 import AppTest

    from scanner import scanner_page

    for c in matched.candidates:  # the fields the setup pane reads
        c.status, c.alerts, c.components, c.signals = Tri.TRUE, [], [], []
    syms = sorted(matched.analyses)
    old = scanner_page.CONFIG_DIR
    scanner_page.CONFIG_DIR = config_dir
    try:
        at = AppTest.from_function(_scanner_script, default_timeout=120)
        at.session_state["res"], at.session_state["cfg"], at.session_state["profiles"] = matched, cfg, {}
        at.run()
        assert not at.exception, at.exception
        assert at.session_state["f_direction"] == "Bullish" and at.session_state["selected_symbol"] == syms[1]
        for group, expected in (("Neutral", syms[0]), ("Bearish", None), ("Bullish", syms[1])):
            at.segmented_control(key="f_direction").set_value(group).run()
            assert not at.exception, (group, at.exception)
            assert at.session_state["selected_symbol"] == expected if expected else \
                "selected_symbol" not in at.session_state, group
    finally:
        scanner_page.CONFIG_DIR = old
