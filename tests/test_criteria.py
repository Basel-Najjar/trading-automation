"""Criteria tab: the criterion list mirrors evaluate_scan, with live settings values."""

from __future__ import annotations

from datetime import date

from scanner.criteria import global_criteria, scan_criteria, signal_text
from scanner.pipeline import scan
from scanner.signals import REGISTRY

D = date(2026, 9, 30)


def test_rules_match_evaluator_order(cfg, market_frames):
    res = scan(cfg, market_frames, D)
    by_name = {c.name: c for c in scan_criteria(cfg)}
    seen = set()
    for by_scan in res.results.values():
        for name, r in by_scan.items():
            crit = by_name[name]
            assert [(x.check, x.label, x.weight) for x in crit.rules] == \
                [(x.check, x.label, x.weight) for x in r.criteria]
            seen.add(name)
    assert seen == set(cfg.active_scans)


def test_every_signal_param_resolved(cfg):
    for name, d in REGISTRY.items():
        text = signal_text(name, cfg.settings)
        for p in d.params:
            if f"`{p}`" in d.description:
                assert f"`{p}` = **" in text
        assert "(default" not in text
    names = {n for sc in cfg.scans.values() for c in sc.criteria for n in c.signal_names}
    assert names <= set(REGISTRY)


def test_rsi_and_flow_rules(cfg):
    by_name = {c.name: c for c in scan_criteria(cfg)}
    rsi = next(r for r in by_name["bullish"].rules if r.check == "rsi_below")
    assert "< **70**" in rsi.summary
    flow = next(r for r in by_name["bearish"].rules if r.check == "flow")
    assert flow.table == [("bearish", "downtrend", "uptrend")]
    assert any(f"{cfg.scans['neutral'].min_pct:g}%" in x for x in by_name["neutral"].outcome)


def test_inactive_scan_has_no_outcome(cfg):
    cfg.scans["neutral"].criteria = []
    c = next(c for c in scan_criteria(cfg) if c.name == "neutral")
    assert not c.active and c.rules == [] and c.outcome == []


def test_global_criteria_uses_live_values(cfg):
    zone = dict(global_criteria(cfg))["Zone"]
    assert f"**{cfg.settings.levels.zone_atr_mult:g}**" in zone
