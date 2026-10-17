"""T-3: strategy engine — every seeded strategy matches, misses on one rule, fails on trend; flow table (B2)."""

from __future__ import annotations

from datetime import date
from itertools import product

import pytest

from conftest import ROOT
from scanner.config import load_config, load_strategies
from scanner.flow import classify_flow
from scanner.models import BIASES, NA, FlowResult, NearResult, SignalResult, Tri
from scanner.strategies import evaluate_strategy, watch_nears

CFG, _ = load_config(ROOT / "config")
CFG.strategies = load_strategies(ROOT / "config" / "strategies.default.yaml", [])  # the pinned seed playbook
S = CFG.settings
KIND = {"support_hold": "hold", "resistance_hold": "hold", "support_break": "break", "resistance_break": "break",
        "bullish_engulfing": "pattern", "bearish_engulfing": "pattern"}


def sig(name: str, status: Tri = Tri.TRUE, age: int = 0) -> SignalResult:
    return SignalResult(name, status, age, 100 - age, date(2026, 9, 30), KIND.get(name, "context"), "neutral")


def flow(market="range", sector="range", name="range") -> FlowResult:
    return FlowResult(market, sector, name, "XLK", "SPY")


# A matching scenario per enabled seeded strategy: (trend, signals, a trend that must fail or None)
SCENARIOS = {
    "bull_call_spread": ("mixed", ["resistance_break"], None),
    "bear_put_spread": ("mixed", ["support_break"], None),
    "bull_put_spread": ("uptrend", ["support_hold"], "downtrend"),
    "bear_call_spread": ("downtrend", ["resistance_hold"], "uptrend"),
    "neutral_calendar": ("range", ["range_bound", "range_mid"], "uptrend"),
    "pmcc": ("uptrend", ["support_hold"], "range"),
    "pmcp": ("downtrend", ["resistance_hold"], "range"),
    "wheel": ("range", ["support_hold"], "mixed"),
    "long_stock": ("uptrend", ["support_hold"], "downtrend"),
}


def test_every_enabled_seeded_strategy_has_a_scenario():
    assert {s.id for s in CFG.strategies if s.active} == set(SCENARIOS)


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_seeded_strategy_match_miss_and_trend(sid):
    st = CFG.strategy(sid)
    trend, names, bad_trend = SCENARIOS[sid]
    ev = evaluate_strategy(st, trend, [sig(n) for n in names], flow(), S)
    assert ev.status is Tri.TRUE, [(r.rule, r.status, r.detail) for r in ev.rules]
    assert {q.name for q in ev.qualifying} == set(names)
    # miss on one rule: drop one required signal
    ev = evaluate_strategy(st, trend, [sig(n) for n in names[1:]], flow(), S)
    assert ev.status is Tri.FALSE
    if bad_trend:
        ev = evaluate_strategy(st, bad_trend, [sig(n) for n in names], flow(), S)
        assert ev.status is Tri.FALSE
        assert next(r for r in ev.rules if r.rule == "trend").status is Tri.FALSE


def test_not_extended_follows_bias():
    bull = CFG.strategy("bull_call_spread")
    ev = evaluate_strategy(bull, "mixed", [sig("resistance_break"), sig("extended_up")], flow(), S)
    assert ev.status is Tri.FALSE
    ev = evaluate_strategy(bull, "mixed", [sig("resistance_break"), sig("extended_down")], flow(), S)
    assert ev.status is Tri.TRUE
    bear = CFG.strategy("bear_put_spread")
    assert evaluate_strategy(bear, "mixed", [sig("support_break"), sig("extended_down")], flow(), S).status is Tri.FALSE
    neutral = CFG.strategy("bull_put_spread")  # not_extended: none
    ev = evaluate_strategy(neutral, "uptrend", [sig("support_hold"), sig("extended_up")], flow(), S)
    assert ev.status is Tri.TRUE
    # extension is a current-state check: an age-1 extension does not block
    ev = evaluate_strategy(bull, "mixed", [sig("resistance_break"), sig("extended_up", age=1)], flow(), S)
    assert ev.status is Tri.TRUE


def test_alignment_required_drops_countertrend():
    st = CFG.strategy("bull_put_spread").model_copy(deep=True)
    st.technical.alignment = "required"
    counter = flow("downtrend", "downtrend", "uptrend")
    assert evaluate_strategy(st, "uptrend", [sig("support_hold")], counter, S).status is Tri.FALSE
    soft = CFG.strategy("bull_put_spread")
    ev = evaluate_strategy(soft, "uptrend", [sig("support_hold")], counter, S)
    assert ev.status is Tri.TRUE and ev.flow_label == "countertrend"


def test_pending_propagates_kleene():
    st = CFG.strategy("bull_call_spread")
    ev = evaluate_strategy(st, "mixed", [sig("resistance_break", Tri.PENDING)], flow(), S)
    assert ev.status is Tri.PENDING
    ev = evaluate_strategy(st, "mixed", [sig("resistance_break", Tri.PENDING), sig("bullish_engulfing")], flow(), S)
    assert ev.status is Tri.PENDING  # decision B5: engulfing alone does not qualify a bull call spread
    pmcc = CFG.strategy("pmcc")  # signals_any: [support_hold, bullish_engulfing] → Kleene OR
    ev = evaluate_strategy(pmcc, "uptrend", [sig("support_hold", Tri.PENDING), sig("bullish_engulfing")], flow(), S)
    assert ev.status is Tri.TRUE
    ev = evaluate_strategy(st, "mixed", [sig("resistance_break"), sig("extended_up", Tri.PENDING)], flow(), S)
    assert ev.status is Tri.PENDING


def test_trend_any_and_rule_trace():
    st = CFG.strategy("bull_call_spread")
    ev = evaluate_strategy(st, "downtrend", [], flow(), S)
    rules = {r.rule: r for r in ev.rules}
    assert rules["trend"].status is Tri.TRUE
    assert rules["signals_all"].status is Tri.FALSE and "not fired" in rules["signals_all"].evidence[0]
    assert set(rules) == {"trend", "signals_all", "not_extended", "alignment"}


def test_watch_nears():
    st = CFG.strategy("bull_put_spread")
    ev = evaluate_strategy(st, "uptrend", [], flow(), S)
    nr = NearResult("support_hold", None, "approaching", 0.3, [])
    assert watch_nears(ev, {"support_hold": [nr]}) == [nr]
    assert watch_nears(ev, {"resistance_break": [nr]}) == []
    ev = evaluate_strategy(st, "downtrend", [], flow(), S)  # trend fails → not a watch item
    assert watch_nears(ev, {"support_hold": [nr]}) == []


# ---------------------------------------------------------------- flow table (decision B2)

AGREE = {"bullish": {"uptrend"}, "non_bearish": {"uptrend", "range"}, "bearish": {"downtrend"},
         "non_bullish": {"downtrend", "range"}, "neutral": {"range"}}
OPPOSE = {"bullish": {"downtrend"}, "non_bearish": {"downtrend"}, "bearish": {"uptrend"},
          "non_bullish": {"uptrend"}, "neutral": {"uptrend", "downtrend"}}


@pytest.mark.parametrize("bias", BIASES)
def test_flow_table_all_combinations(bias):
    states = ["uptrend", "downtrend", "range", "mixed", NA]
    for comps in product(states, repeat=3):
        a = sum(c in AGREE[bias] for c in comps)
        o = sum(c in OPPOSE[bias] for c in comps)
        expected = "countertrend" if o > a else ("with_flow" if o == 0 and a >= 1 else "mixed")
        assert classify_flow(bias, list(comps), S) == expected, (bias, comps)


def test_flow_examples():
    assert classify_flow("bullish", ["uptrend", "uptrend", "uptrend"], S) == "with_flow"
    assert classify_flow("bullish", ["uptrend", "downtrend", "uptrend"], S) == "mixed"
    assert classify_flow("bullish", ["downtrend", "downtrend", "uptrend"], S) == "countertrend"
    assert classify_flow("neutral", ["range", NA, NA], S) == "with_flow"
    assert classify_flow("bullish", ["mixed", "mixed", NA], S) == "mixed"
