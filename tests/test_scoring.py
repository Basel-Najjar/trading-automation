"""Scoring: components are measured for the (symbol, strategy) pair, and strategies can carry their own weights."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np

from conftest import bars_from_ohlc, hlevel
from scanner.config import Settings, Strategy, load_config
from scanner.models import Pivot, SignalResult, TrendResult, Tri
from scanner.scoring import _room, _signal_fit, _trend_fit

S = Settings()


def _strat(**tech) -> Strategy:
    return Strategy.model_validate({"id": "t", "name": "T", "bias": "non_bearish", "technical": tech})


def _sig(name: str, kind: str) -> SignalResult:
    return SignalResult(name, Tri.TRUE, 0, 0, date(2026, 9, 30), kind, "bullish")


def _flat_bars(n: int = 60, close: float = 100.0):
    return bars_from_ohlc([(close, close + 1, close - 1, close)] * n, atr=2.0)


def _uptrend(step: float) -> TrendResult:
    d = date(2026, 9, 1)
    piv = [Pivot("high", 10, d, 100.0, 15), Pivot("high", 30, d, 100.0 + step, 35),
           Pivot("low", 20, d, 90.0, 25), Pivot("low", 40, d, 90.0 + step, 45)]
    return TrendResult("uptrend", "test", piv)


def test_signal_scored_against_the_strategys_best_kind():
    holds = _strat(trend=["uptrend"], signals_any=["support_hold", "approaching_support"])
    assert _signal_fit(_sig("support_hold", "hold"), holds, S.scoring)[0] == 1.0
    assert _signal_fit(_sig("approaching_support", "context"), holds, S.scoring)[0] == 0.2 / 0.75
    breaks = _strat(trend=["uptrend"], signals_all=["resistance_break"], signals_any=["bullish_engulfing"])
    assert _signal_fit(_sig("resistance_break", "break"), breaks, S.scoring)[0] == 1.0
    assert _signal_fit(None, holds, S.scoring)[0] == 0.0


def test_trend_fit_grows_with_pivot_step_and_sma_stack():
    b = _flat_bars(close=110.0)
    t = len(b) - 1
    b.sma50 = np.linspace(100, 105, len(b))
    b.sma200 = np.full(len(b), 95.0)
    weak, _ = _trend_fit(["uptrend"], _uptrend(1.5), b, t, S)
    strong, note = _trend_fit(["uptrend"], _uptrend(4.0), b, t, S)
    assert strong == 1.0 and "3/3" in note
    assert 0.5 < weak < strong
    b.sma50 = np.linspace(105, 100, len(b))  # falling SMA50, still above SMA200, close above both
    assert _trend_fit(["uptrend"], _uptrend(4.0), b, t, S)[0] == (1 + 2 / 3) / 2


def test_trend_fit_any_and_short_history():
    b = _flat_bars()
    assert _trend_fit(["any"], _uptrend(4.0), b, len(b) - 1, S)[0] == S.scoring.trend_fit_any
    b.sma50 = np.full(len(b), np.nan)
    b.sma200 = np.full(len(b), np.nan)
    assert _trend_fit(["uptrend"], _uptrend(4.0), b, len(b) - 1, S)[0] == (1.0 + 0.5) / 2  # SMA checks n/a


def test_trend_fit_range_uses_touches_and_width():
    b = _flat_bars()
    hi, lo = hlevel(110, len(b), "resistance", "rh"), hlevel(100, len(b), "support", "rl")
    hi.touches, lo.touches = 4, 4
    wide = TrendResult("range", "t", [], range_high=hi, range_low=lo, range_width_atr=6.0, range_position=0.5)
    narrow = TrendResult("range", "t", [], range_high=hi, range_low=lo, range_width_atr=2.0, range_position=0.5)
    assert _trend_fit(["range"], wide, b, len(b) - 1, S)[0] == 1.0
    assert _trend_fit(["range"], narrow, b, len(b) - 1, S)[0] == 0.5


def _row(lid: str, dist_atr: float) -> dict:
    return {"level": hlevel(0, 1, lid=lid), "dist_atr": dist_atr}


def _lvl(lid: str):
    return hlevel(0, 1, lid=lid)


def test_room_follows_bias():
    k, r = S.levels.zone_atr_mult, S.scoring.room_atr
    mixed = TrendResult("mixed", "t")
    sup, res = [_row("s", -0.5)], [_row("r", 1.75)]
    assert _room("bullish", mixed, sup, res, k, r)[0] == (1.75 - k) / r
    assert _room("bearish", mixed, sup, res, k, r)[0] == (0.5 - k) / r
    assert _room("bullish", mixed, sup, [], k, r)[0] == 1.0
    assert _room("neutral", mixed, [_row("s", -1.25)], [_row("r", 1.25)], k, r)[0] == 1.0
    assert _room("neutral", TrendResult("range", "t", [], range_position=0.25), sup, res, k, r)[0] == 0.5
    own = _lvl("s")  # price inside the signal level's zone can put that level on the opposing side
    assert _room("bearish", mixed, [_row("s", -0.1), _row("s2", -2.25)], res, k, r, own)[0] == (2.25 - k) / r


def _write_weights(d: Path, sid: str, line: str) -> None:
    p = d / "strategies.yaml"
    lines = p.read_text(encoding="utf-8").splitlines()
    i = lines.index(f"- id: {sid}")
    j = next(n for n in range(i, len(lines)) if lines[n].startswith("  scoring:"))
    lines[j + 1] = f"    weights: {line}"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_strategy_weights_override_global(cfg):
    st = cfg.strategy("bull_put_spread")
    w = cfg.settings.scoring.weights_for(st)
    assert w["level_quality"] == 25 and w["room"] == 0 and sum(w.values()) == 100
    pmcc = cfg.strategy("pmcc")
    assert not pmcc.scoring.weights and cfg.settings.scoring.weights_for(pmcc) == cfg.settings.scoring.weights


def test_strategy_weight_sum_warns_and_unknown_key_errors(config_dir):
    _write_weights(config_dir, "wheel", "{ signal: 50 }")
    cfg, issues = load_config(config_dir)
    assert cfg is not None
    assert any("wheel.scoring.weights" in str(i) and i.severity == "warning" for i in issues)
    _write_weights(config_dir, "wheel", "{ signl: 20 }")
    cfg, issues = load_config(config_dir)
    assert cfg is None and any("did you mean 'signal'" in str(i) for i in issues)


def test_candidate_scores_differ_by_strategy_on_one_symbol(cfg, market_frames):
    from scanner.pipeline import scan

    res = scan(cfg, market_frames, date(2026, 9, 30))
    for c in res.candidates:
        st = cfg.strategy(c.strategy_id)
        w = cfg.settings.scoring.weights_for(st)
        assert [x.weight for x in c.components] == [w.get(x.name, 0.0) for x in c.components]
        assert {x.name for x in c.components} == set(w)
