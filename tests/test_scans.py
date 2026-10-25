"""Scans: each criterion check, weighted % met, threshold, PENDING_CLOSE, ranking, flow per direction, watch list."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from scanner.config import Criterion, Scan, Settings
from scanner.flow import classify_flow
from scanner.models import AlertPrice, CriterionResult, FlowResult, NearResult, ScanResult, SignalResult, Tri
from scanner.pipeline import scan
from scanner.scans import build_watchlist, criterion_label, evaluate_criterion, evaluate_scan, rank, watch_nears

D = date(2026, 9, 30)


def _sig(name: str, status: Tri = Tri.TRUE, kind: str = "hold") -> SignalResult:
    return SignalResult(name, status, 0, 10, D, kind, "bullish")


def _sa(trend="uptrend", signals=(), rsi=50.0, provisional=False, flow=None):
    return SimpleNamespace(symbol="X", trend=SimpleNamespace(state=trend), signals=list(signals),
                           bars=SimpleNamespace(rsi=np.array([rsi])), last_idx=0, provisional=provisional, flow=flow)


def _eval(raw: dict, direction="bullish", flow_label="mixed", **sa_kw) -> CriterionResult:
    return evaluate_criterion(Criterion.model_validate(raw), direction, _sa(**sa_kw), flow_label, "", "")


@pytest.mark.parametrize(("raw", "kw", "want"), [
    ({"trend": ["uptrend", "range"]}, {"trend": "range"}, Tri.TRUE),
    ({"trend": ["uptrend"]}, {"trend": "mixed"}, Tri.FALSE),
    ({"signal_any": ["support_hold", "bullish_engulfing"]}, {"signals": [_sig("bullish_engulfing")]}, Tri.TRUE),
    ({"signal_any": ["support_hold"]}, {"signals": [_sig("resistance_hold")]}, Tri.FALSE),
    ({"signal_all": ["support_hold", "range_bound"]}, {"signals": [_sig("support_hold")]}, Tri.FALSE),
    ({"signal_all": ["support_hold", "range_bound"]}, {"signals": [_sig("support_hold"), _sig("range_bound")]},
     Tri.TRUE),
    ({"signal_any": ["support_hold"]}, {"signals": [_sig("support_hold", Tri.PENDING)]}, Tri.PENDING),
    ({"rsi_below": 70}, {"rsi": 69.9}, Tri.TRUE),
    ({"rsi_below": 70}, {"rsi": 70.0}, Tri.FALSE),
    ({"rsi_above": 30}, {"rsi": 31.0}, Tri.TRUE),
    ({"rsi_between": [40, 60]}, {"rsi": 60.0}, Tri.TRUE),
    ({"rsi_between": [40, 60]}, {"rsi": 61.0}, Tri.FALSE),
    ({"rsi_below": 70}, {"rsi": 50.0, "provisional": True}, Tri.PENDING),  # met on the forming bar: pending
    ({"rsi_below": 70}, {"rsi": 80.0, "provisional": True}, Tri.FALSE),
])
def test_criterion_checks(raw, kw, want):
    assert _eval(raw, **kw).status is want


@pytest.mark.parametrize(("value", "label", "want"), [
    ("with_flow", "with_flow", Tri.TRUE), ("with_flow", "mixed", Tri.FALSE),
    ("not_countertrend", "mixed", Tri.TRUE), ("not_countertrend", "countertrend", Tri.FALSE),
])
def test_flow_check(value, label, want):
    assert _eval({"flow": value}, flow_label=label).status is want


def test_signal_criterion_keeps_fired_signals_only():
    c = _eval({"signal_any": ["support_hold", "bullish_engulfing"]},
              signals=[_sig("support_hold"), _sig("support_hold", Tri.FALSE), _sig("resistance_hold")])
    assert [h.name for h in c.signals] == ["support_hold"]


def test_signal_window_counts_only_recent_bars():
    old = SignalResult("support_hold", Tri.TRUE, 4, 6, D, "hold", "bullish")
    c = Criterion(signal_any=["support_hold"])
    for window, want in ((4, Tri.FALSE), (5, Tri.TRUE)):
        r = evaluate_criterion(c, "bullish", _sa(signals=[old]), "mixed", "", "", window)
        assert r.status is want, window
    assert "latest 5 bars" in evaluate_criterion(c, "bullish", _sa(signals=[old]), "mixed", "", "", 5).detail
    sc = Scan(lookback_bars=3, criteria=[c, Criterion(signal_any=["support_hold"], within=5)])
    got = [evaluate_criterion(x, "bullish", _sa(signals=[old]), "mixed", "", "", sc.window(x)).status
           for x in sc.criteria]
    assert got == [Tri.FALSE, Tri.TRUE]


def test_channel_criterion_uses_structure_from_before_the_window(cfg, market_frames):
    res = scan(cfg, market_frames, D)
    for sym, by_scan in res.results.items():
        sa = res.analyses[sym]
        crit = next(c for c in by_scan["neutral"].criteria if c.check == "channel")
        assert len(sa.techs) >= cfg.lookback_bars
        if crit.status is Tri.TRUE:
            assert "stayed between" in crit.detail
        else:
            assert crit.status is Tri.FALSE and "no horizontal channel" in crit.detail


def test_flow_tables_per_direction():
    s = Settings()
    assert classify_flow("bullish", ["uptrend", "uptrend", "range"], s) == "with_flow"
    assert classify_flow("bearish", ["uptrend", "uptrend", "range"], s) == "countertrend"
    assert classify_flow("neutral", ["range", "n/a", "mixed"], s) == "with_flow"
    assert classify_flow("non_bearish", ["uptrend", "range", "mixed"], s) == "with_flow"
    assert classify_flow("non_bearish", ["downtrend", "downtrend", "range"], s) == "countertrend"
    assert classify_flow("non_bullish", ["downtrend", "range", "n/a"], s) == "with_flow"
    assert classify_flow("non_bullish", ["uptrend", "mixed", "mixed"], s) == "countertrend"


def _result(*specs: tuple[Tri, float], min_pct: float = 70, symbol: str = "X") -> ScanResult:
    crits = [CriterionResult("trend", f"c{n}", st, w, "") for n, (st, w) in enumerate(specs)]
    return ScanResult(symbol, "bullish", crits, min_pct, "mixed")


def test_weighted_pct_and_threshold():
    r = _result((Tri.TRUE, 1), (Tri.TRUE, 2), (Tri.FALSE, 1), (Tri.PENDING, 1))
    assert r.pct == pytest.approx(60.0) and r.pending_pct == pytest.approx(20.0)
    assert (r.met, r.total) == (2, 4) and not r.passed
    assert _result((Tri.TRUE, 2), (Tri.TRUE, 1), (Tri.FALSE, 1), min_pct=75).passed  # exactly at the minimum
    assert not ScanResult("X", "bullish", [], 0, "mixed").passed  # no criteria: never listed


def test_rank_by_pct_then_confirmed_then_symbol():
    a = _result((Tri.TRUE, 1), (Tri.FALSE, 1), symbol="A")
    b = _result((Tri.TRUE, 1), (Tri.PENDING, 1), symbol="B")
    c = _result((Tri.TRUE, 1), (Tri.TRUE, 1), symbol="C")
    d = _result((Tri.TRUE, 1), (Tri.FALSE, 1), symbol="0")
    assert [r.symbol for r in rank([a, b, c, d])] == ["C", "0", "A", "B"]
    assert [r.rank for r in rank([a, b, c, d])] == [1, 2, 3, 4]


def test_labels():
    assert criterion_label(Criterion(trend=["uptrend", "range"])) == "Trend: uptrend or range"
    assert criterion_label(Criterion(rsi_between=(40, 60))) == "RSI 40–60"
    assert criterion_label(Criterion(flow="not_countertrend")) == "Not against flow"
    assert criterion_label(Criterion(signal_any=["support_hold"], label="Mine")) == "Mine"


def _near(signal: str) -> NearResult:
    return NearResult(signal, None, "near", 0.3, [AlertPrice("Trigger", 1.0, "x")])


def _watch_result(min_pct: float) -> ScanResult:
    trig = Criterion(signal_any=["support_hold", "bullish_engulfing"], weight=2)
    crits = [CriterionResult("trend", "t", Tri.TRUE, 1, ""),
             CriterionResult("signal_any", "s", Tri.FALSE, 2, "", spec=trig),
             CriterionResult("rsi_below", "r", Tri.FALSE, 1, "")]
    return ScanResult("X", "bullish", crits, min_pct, "mixed")


def test_watch_nears_only_when_firing_reaches_threshold():
    near = {"support_hold": [_near("support_hold")]}
    assert [n.signal for n in watch_nears(_watch_result(75), near)] == ["support_hold"]  # 1 + 2 of 4 = 75%
    assert watch_nears(_watch_result(80), near) == []  # 75% < 80%
    assert watch_nears(_watch_result(75), {"range_mid": [_near("range_mid")]}) == []  # not one of its signals


def test_watch_signal_all_needs_every_missing_signal_near():
    spec = Criterion(signal_all=["support_hold", "range_bound"])
    crit = CriterionResult("signal_all", "s", Tri.FALSE, 1, "", signals=[_sig("range_bound")], spec=spec)
    r = ScanResult("X", "neutral", [crit], 100, "mixed")
    assert [n.signal for n in watch_nears(r, {"support_hold": [_near("support_hold")]})] == ["support_hold"]
    crit.signals = []
    assert watch_nears(r, {"support_hold": [_near("support_hold")]}) == []  # range_bound is missing and not near


def test_build_watchlist_merges_scans(settings):
    near = {"support_hold": [_near("support_hold")]}
    results = {"X": {"bullish": _watch_result(75), "neutral": _watch_result(50)}}
    results["X"]["neutral"].scan = "neutral"
    watch = build_watchlist(results, {"X": SimpleNamespace(near=near)}, settings)
    assert len(watch) == 1 and watch[0].scans == ["bullish", "neutral"]


def test_evaluate_scan_on_real_analysis(cfg, market_frames):
    res = scan(cfg, market_frames, D)
    sa = res.analyses["AAA"]
    sc = Scan(criteria=[Criterion(trend=[sa.trend.state]), Criterion(rsi_between=(0, 100), weight=3)])
    r = evaluate_scan("bullish", sc, sa, cfg.settings)
    assert r.pct == pytest.approx(100.0) and r.passed and [c.status for c in r.criteria] == [Tri.TRUE, Tri.TRUE]
    assert r.flow_label == classify_flow("bullish", [sa.flow.market, sa.flow.sector, sa.flow.name], cfg.settings)
    assert isinstance(sa.flow, FlowResult) and r.alerts  # nearest support/resistance at least
