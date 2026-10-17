"""View model for the Streamlit app: % of rules met, symbol ranking, post-scan filtering, detail panel."""

from __future__ import annotations

from datetime import date

import pytest

from scanner.app_view import (
    card_rankings,
    card_tooltip,
    detail_view,
    filtered_result,
    score_color,
    split_summary,
    strategy_rows,
    symbol_rankings,
    ticker_description,
)
from scanner.data import Profile
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


def test_card_rankings_one_per_symbol_strategy(res):
    cards = card_rankings(res)
    assert len(cards) == sum(len(evs) for evs in res.evals.values())
    assert len({(s.symbol, r.ev.strategy.id) for s, r in cards}) == len(cards)
    keys = [(-r.fraction, -r.score) for _, r in cards]
    assert keys == sorted(keys)


def test_card_rankings_filters(res):
    cards = card_rankings(res)
    sym, sid = cards[0][0].symbol, cards[0][1].ev.strategy.id
    assert card_rankings(res, [], []) == cards
    assert {s.symbol for s, _ in card_rankings(res, [sym])} == {sym}
    assert {r.ev.strategy.id for _, r in card_rankings(res, strategy_ids=[sid])} == {sid}
    both = card_rankings(res, [sym], [sid])
    assert [(s.symbol, r.ev.strategy.id) for s, r in both] == [(sym, sid)]


def test_score_color_scale():
    assert score_color(0) == "#d62728"
    assert score_color(50) == "#e6b800"
    assert score_color(100) == "#2ca02c"
    assert score_color(-5) == score_color(0) and score_color(140) == score_color(100)
    assert score_color(25) == "#de7014"


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
            assert len(d["rules"]) == r.total
            types = [t.type for t in d["chart"].data]
            assert types.count("candlestick") == (2 if res.analyses[sym].provisional else 1)
            assert any(t.name == "RSI" for t in d["chart"].data)


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
