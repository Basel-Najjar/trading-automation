"""View model for the Streamlit app: % of rules met, symbol ranking, post-scan filtering, detail panel."""

from __future__ import annotations

from datetime import date

import pytest

from scanner.app_view import chart_page, detail_view, filtered_result, strategy_rows, symbol_rankings
from scanner.models import RuleResult, Tri
from scanner.pipeline import scan
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


def test_detail_view_for_every_pair(res, cfg):
    for sym in res.analyses:
        for r in strategy_rows(res, sym):
            d = detail_view(res, cfg, sym, r.ev.strategy.id)
            assert d["chart"].startswith("<svg") and len(d["rules"]) == r.total
    page = chart_page("<svg></svg>")
    assert "<style>" in page and ".chart .wick" in page
