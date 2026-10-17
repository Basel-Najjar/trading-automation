"""View model for the Streamlit app: % of rules met, ranking and grouping, tiers, overview, setup pane, filtering."""

from __future__ import annotations

from datetime import date

import pytest

from scanner.app_view import (
    BIAS_GROUPS,
    card_tooltip,
    chart_overlays,
    detail_view,
    filtered_result,
    humanize,
    opportunity_rows,
    overlay_choices,
    overview,
    price_ladder,
    setup_pairs,
    split_summary,
    strategy_recipe,
    strategy_rows,
    symbol_chart,
    symbol_rankings,
    ticker_description,
    tier,
    tier_cutoffs,
)
from scanner.data import Profile
from scanner.models import RuleResult, Tri
from scanner.pipeline import scan
from scanner.plotly_chart import FADED
from scanner.report import render_html

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


def test_opportunity_rows_one_per_symbol(res):
    rows = opportunity_rows(res, matches_only=False)
    assert len(rows) == len({r.symbol for r in rows}) == len(symbol_rankings(res))
    matched = opportunity_rows(res)
    assert {r.symbol for r in matched} == {c.symbol for c in res.candidates}
    assert all(r.best.cand is not None for r in matched)
    keys = [(r.best.cand is None, -r.best.score) for r in rows]
    assert keys == sorted(keys)
    for r in rows:
        assert [x.fraction for x in r.rows] == sorted((x.fraction for x in r.rows), reverse=True)


def test_opportunity_rows_filters(res):
    rows = opportunity_rows(res, matches_only=False)
    sym, sid = rows[0].symbol, rows[0].best.ev.strategy.id
    assert opportunity_rows(res, [], [], [], [], False) == rows
    assert [r.symbol for r in opportunity_rows(res, [sym], matches_only=False)] == [sym]
    assert all(x.ev.strategy.id == sid for r in opportunity_rows(res, strategy_ids=[sid], matches_only=False)
               for x in r.rows)
    for group, biases in BIAS_GROUPS.items():
        assert all(x.ev.strategy.bias in biases for r in opportunity_rows(res, bias_groups=[group], matches_only=False)
                   for x in r.rows)
    sectors = {r.sector for r in rows if r.sector}
    if sectors:
        etf = sorted(sectors)[0]
        assert {r.sector for r in opportunity_rows(res, sectors=[etf], matches_only=False)} == {etf}


def test_setup_pairs(res):
    rows = opportunity_rows(res, matches_only=False)
    everything = setup_pairs(rows, matches_only=False)
    assert len(everything) == sum(len(r.rows) for r in rows)
    assert len({(s.symbol, r.ev.strategy.id) for s, r in everything}) == len(everything)
    only = setup_pairs(rows)
    assert len(only) == len(res.candidates) and all(r.cand for _, r in only)
    assert [r.score for _, r in only] == sorted((r.score for _, r in only), reverse=True)


def test_tier_cutoffs_are_relative_to_the_run():
    scores = [float(x) for x in range(10, 0, -1)]  # 10 … 1
    cut = tier_cutoffs(scores)
    assert cut == (9.0, 5.0)  # top 20% = 2 scores, top 60% = 6 scores
    assert [tier(x, cut) for x in scores] == ["A"] * 2 + ["B"] * 4 + ["C"] * 4
    assert tier(None, cut) is None
    assert tier_cutoffs([70.0, 70.0, 70.0]) == (70.0, 70.0)  # ties share the tier
    assert tier(5.0, tier_cutoffs([])) == "C"
    assert tier(80.0, tier_cutoffs([80.0])) == "A"


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


def test_filtered_result_keeps_only_picks(res, cfg):
    sym = sorted(res.analyses)[0]
    sid = res.strategies_enabled[0]
    f = filtered_result(res, [sym], [sid])
    assert all(c.symbol == sym and c.strategy_id == sid for c in f.candidates)
    assert all(ev.strategy.id == sid for evs in f.evals.values() for ev in evs)
    assert all(w.symbol == sym and [s for s, _ in w.strategies] == [sid] for w in f.watchlist)
    assert f.focus_symbols == [sym] and res.focus_symbols is None
    assert len(res.evals[sym]) == len(res.strategies_enabled)  # original untouched
    html = render_html(f, cfg, deep=True)
    assert sym in html


def test_filtered_result_empty_picks_is_everything(res):
    f = filtered_result(res, [], [])
    assert f.candidates == res.candidates and f.evals == res.evals


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
