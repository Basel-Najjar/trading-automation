"""Criteria tab: the rule list mirrors evaluate_strategy, with live (override-aware) settings values."""

from __future__ import annotations

from datetime import date

from scanner.criteria import global_criteria, signal_text, strategy_criteria
from scanner.pipeline import scan
from scanner.signals import REGISTRY

D = date(2026, 9, 30)


def test_rules_match_evaluator_order(cfg, market_frames):
    res = scan(cfg, market_frames, D)
    by_id = {c.strategy.id: c for c in strategy_criteria(cfg)}
    seen = set()
    for evs in res.evals.values():
        for ev in evs:
            assert [r.rule for r in by_id[ev.strategy.id].rules] == [r.rule for r in ev.rules]
            seen.add(ev.strategy.id)
    assert seen == {s.id for s in cfg.strategies if s.active}


def test_every_signal_param_resolved(cfg):
    for name, d in REGISTRY.items():
        text = signal_text(name, cfg.settings)
        for p in d.params:
            if f"`{p}`" in d.description:
                assert f"`{p}` = **" in text
        assert "(default" not in text
    names = {n for s in cfg.strategies for n in s.technical.signals_all + s.technical.signals_any}
    assert names <= set(REGISTRY)


def test_overrides_shown(cfg):
    cfg.strategy("bull_call_spread").technical.overrides = {"full_body_ratio": 0.8}
    c = next(c for c in strategy_criteria(cfg) if c.strategy.id == "bull_call_spread")
    assert c.overrides == {"full_body_ratio": 0.8}
    sig = next(r for r in c.rules if r.rule == "signals_all").signals[0]
    assert ("full_body_ratio", 0.8) in sig.params


def test_not_extended(cfg):
    by_id = {c.strategy.id: c for c in strategy_criteria(cfg)}
    assert "not_extended" not in [r.rule for r in by_id["bull_put_spread"].rules]
    ne = next(r for r in by_id["bull_call_spread"].rules if r.rule == "not_extended")
    assert f"`rsi_overbought` = **{cfg.settings.indicators.rsi_overbought:g}**" in ne.summary


def test_stub_strategy_has_no_rules(cfg):
    c = next(c for c in strategy_criteria(cfg) if c.strategy.id == "flyagonal")
    assert not c.active and c.rules == [] and c.outcome == []


def test_global_criteria_uses_live_values(cfg):
    zone = dict(global_criteria(cfg))["Zone"]
    assert f"**{cfg.settings.levels.zone_atr_mult:g}**" in zone
