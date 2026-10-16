"""T-5 + determinism + isolation: scan() sees only bars ≤ as_of; same inputs → identical outputs."""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from conftest import FakeProvider, random_frame
from scanner import pipeline, sessions
from scanner.models import Tri
from scanner.pipeline import run_scan, scan
from scanner.report import candidate_dict, level_dict, signal_dict, watch_dict


def snapshot(res) -> str:
    k = res.settings.levels.zone_atr_mult
    out = {
        "as_of": res.as_of.isoformat(),
        "candidates": [candidate_dict(c, res.analyses[c.symbol], k) for c in res.candidates],
        "watch": [watch_dict(w) for w in res.watchlist],
        "symbols": {s: {"trend": sa.trend.state, "reason": sa.trend.reason,
                        "signals": [signal_dict(x) for x in sa.signals],
                        "levels": [level_dict(lv, sa, k) for lv in sa.levels],
                        "flow": sa.flow.components() if sa.flow else None}
                    for s, sa in sorted(res.analyses.items())},
    }
    return json.dumps(out, sort_keys=True, default=str)


D = date(2026, 6, 15)


def test_as_of_equals_truncated_data(cfg, market_frames):
    full = scan(cfg, market_frames, D)
    trunc = {k: v[v.index <= pd.Timestamp(D)] for k, v in market_frames.items()}
    assert snapshot(full) == snapshot(scan(cfg, trunc, D))
    assert full.as_of == D and all(sa.as_of == D for sa in full.analyses.values())


def test_future_perturbation_does_not_change_past(cfg, market_frames):
    base = snapshot(scan(cfg, market_frames, D))
    rng = np.random.default_rng(7)
    noisy = {}
    for k, v in market_frames.items():
        v = v.copy()
        after = v.index > pd.Timestamp(D)
        scale = rng.uniform(0.3, 3.0, after.sum())
        for c in ("open", "high", "low", "close"):
            v.loc[after, c] = v.loc[after, c] * scale
        noisy[k] = v
    assert snapshot(scan(cfg, noisy, D)) == base


def test_no_pivot_confirmed_after_as_of(cfg, market_frames):
    res = scan(cfg, market_frames, D)
    for sa in res.analyses.values():
        for tech in sa.techs:
            assert all(p.confirm_idx <= tech.cutoff for p in tech.highs + tech.lows)
            assert tech.cutoff <= sa.last_idx


def test_deterministic(cfg, market_frames):
    assert snapshot(scan(cfg, market_frames, D)) == snapshot(scan(cfg, market_frames, D))


def test_scan_produces_matches_over_many_days(cfg, market_frames):
    days = sessions.sessions_between(date(2026, 3, 2), date(2026, 6, 30))
    n_cands = sum(len(scan(cfg, market_frames, d, quality=False).candidates) for d in days[::5])
    assert n_cands > 0  # the engine finds setups on synthetic data


def test_insufficient_history_and_missing_data(cfg, market_frames):
    frames = dict(market_frames)
    frames["CCC"] = random_frame(100, seed=9)
    del frames["BBB"]
    res = scan(cfg, frames, D)
    reasons = dict(res.skipped)
    assert "insufficient history" in reasons["CCC"] and reasons["BBB"] == "no price data"
    assert res.exit_code == 2 and "AAA" in res.analyses


def test_partial_history_allowed(cfg, market_frames):
    cfg.settings = cfg.settings.model_copy(
        update={"data": cfg.settings.data.model_copy(update={"allow_partial_history": True})})
    frames = dict(market_frames)
    frames["CCC"] = random_frame(150, seed=9, end=date(2026, 9, 30))
    res = scan(cfg, frames, date(2026, 9, 30))
    sa = res.analyses["CCC"]
    assert all(lv.type != "sma200" for lv in sa.levels)
    assert any("partial history" in d.message for d in res.diagnostics)


def test_per_symbol_error_isolation(cfg, market_frames, monkeypatch):
    real = pipeline.analyze_symbol

    def boom(symbol, *a, **kw):
        if symbol == "BBB":
            raise RuntimeError("synthetic failure")
        return real(symbol, *a, **kw)

    monkeypatch.setattr(pipeline, "analyze_symbol", boom)
    res = scan(cfg, market_frames, D)
    assert "BBB" not in res.analyses and "AAA" in res.analyses
    assert any(d.symbol == "BBB" and d.severity == "error" for d in res.diagnostics)


def test_missing_benchmark_and_sector(cfg, market_frames):
    frames = {k: v for k, v in market_frames.items() if k not in ("SPY", "XLK")}
    res = scan(cfg, frames, D)
    sa = res.analyses["AAA"]
    assert sa.flow.market == "n/a" and sa.flow.sector == "n/a" and sa.flow.name == "n/a"
    assert res.analyses["CCC"].flow.sector_etf is None
    assert any("benchmark SPY unavailable" in d.message for d in res.diagnostics)


def test_flow_components_present(cfg, market_frames):
    res = scan(cfg, market_frames, D)
    aaa, ccc = res.analyses["AAA"].flow, res.analyses["CCC"].flow
    assert aaa.market == res.context["SPY"].trend.state
    assert aaa.sector in ("uptrend", "downtrend", "range", "mixed") and aaa.name != "n/a"
    assert ccc.sector == "n/a" and ccc.name == "n/a"


def test_strategy_filter_and_disabled(cfg, market_frames):
    res = scan(cfg, market_frames, D, strategy_ids=["bull_put_spread", "flyagonal"])
    assert res.strategies_enabled == ["bull_put_spread"]
    assert res.strategies_skipped == [("flyagonal", "stub")]
    assert all(c.strategy_id == "bull_put_spread" for c in res.candidates)


def test_ranking_order(cfg, market_frames):
    days = sessions.sessions_between(date(2026, 3, 2), date(2026, 6, 30))
    for d in days[::3]:
        res = scan(cfg, market_frames, d, quality=False)
        keys = [(c.pending, -c.score, -round(c.level_quality, 9), c.symbol, c.strategy_id) for c in res.candidates]
        assert keys == sorted(keys)
        assert [c.rank for c in res.candidates] == list(range(1, len(res.candidates) + 1))
        for c in res.candidates:
            assert abs(sum(x.points for x in c.components) - c.score) < 0.01
            assert 0 <= c.score <= 100


def test_run_scan_with_fake_provider_and_provisional(cfg, market_frames):
    prov = FakeProvider(market_frames)
    midday = datetime(2026, 9, 30, 12, 0, tzinfo=sessions.ET)
    res = run_scan(cfg, now=midday, provider=prov)
    assert res.as_of == date(2026, 9, 30) and res.provisional
    for sa in res.analyses.values():
        assert sa.provisional and sa.completed_idx == sa.last_idx - 1
        assert all(s.status is Tri.PENDING for s in sa.signals if s.age == 0)
        assert all(s.status is Tri.TRUE for s in sa.signals if s.age == 1)
    assert not any("stale" in d.message for d in res.diagnostics)
    after = run_scan(cfg, now=datetime(2026, 9, 30, 18, 0, tzinfo=sessions.ET), provider=prov)
    assert not after.provisional


def test_run_scan_as_of_snaps_to_session(cfg, market_frames):
    res = run_scan(cfg, as_of=date(2026, 7, 4), now=datetime(2026, 9, 30, 18, 0, tzinfo=sessions.ET),
                   provider=FakeProvider(market_frames))
    assert res.as_of == date(2026, 7, 2) and res.requested_as_of == date(2026, 7, 4)
    assert any("not a trading day" in n for n in res.notes)


@pytest.mark.parametrize("overrides", [{"full_body_ratio": 0.99}])
def test_strategy_overrides_use_signal_variant(cfg, market_frames, overrides):
    st = cfg.strategy("bear_put_spread")
    st.technical.overrides = overrides
    res = scan(cfg, market_frames, D)
    key = pipeline.overrides_key(overrides)
    assert all(key in sa.signal_variants for sa in res.analyses.values())
