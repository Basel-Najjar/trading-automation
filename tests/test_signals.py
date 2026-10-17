"""T-2: every §6.4 signal definition — positive, negative and edge cases.

Hand-built bars use ATR = 2 and zone_atr_mult = 0.25, so a level at 100 has zone [99.5, 100.5].
"""

from __future__ import annotations

from conftest import bars_from_ohlc, hlevel, tech_for
from scanner.config import Settings
from scanner.models import SIGNAL_NAMES, TrendResult, Tri
from scanner.signals import REGISTRY, body_ratio, engulfs, evaluate_signals, is_full_body, near_signals

S = Settings()
ABOVE = (102.0, 102.5, 101.5, 102.0)  # filler bar closing above the zone
BELOW = (98.0, 98.5, 97.5, 98.0)  # filler bar closing below the zone


def _fire(name, rows, level=100.0, settings=S, origin="support", **kw):
    b = bars_from_ohlc(rows, **kw)
    lv = hlevel(level, len(b), origin=origin)
    return REGISTRY[name].fn(tech_for(b, [lv]), len(b) - 1, settings)


def _with(**signals):
    return S.model_copy(update={"signals": S.signals.model_copy(update=signals)})


def test_registry_covers_vocabulary():
    assert set(REGISTRY) == set(SIGNAL_NAMES)
    assert all(REGISTRY[n].description for n in SIGNAL_NAMES)


# ---------------------------------------------------------------- candle helpers


def test_candle_helpers():
    b = bars_from_ohlc([(100, 101, 99, 100.5), (100.7, 100.8, 97.9, 98.0), (100, 100, 100, 100)])
    assert body_ratio(b, 0) == 0.25 and not is_full_body(b, 0, 0.7)
    assert is_full_body(b, 1, 0.7)
    assert not is_full_body(b, 2, 0.7)  # zero-range bar
    assert engulfs(b, 1, 0.0, False) and engulfs(b, 1, 0.0, True)  # bearish engulfs bullish


def test_equal_bodies_engulf_and_tolerance():
    b = bars_from_ohlc([(100, 101, 99, 101), (101, 101.5, 99.5, 100)])
    assert engulfs(b, 1, 0.0, True)  # identical body range, opposite colour
    b = bars_from_ohlc([(100, 101, 99, 101), (100.9, 101.5, 99.5, 100.05)])
    assert not engulfs(b, 1, 0.0, False) and engulfs(b, 1, 0.1, False)


# ---------------------------------------------------------------- holds


HOLD_OK = [ABOVE] * 12 + [(103, 103.5, 101.5, 102), (102, 102.2, 100.2, 100.8), (100.9, 103, 100.7, 102.5)]


def test_support_hold_positive_with_evidence():
    out = _fire("support_hold", HOLD_OK)
    assert len(out) == 1
    lv, ev = out[0]
    assert lv.id == "h_test" and ev["zone_low"] == 99.5 and ev["test_bar"]["low"] == 100.2
    assert "102.50 > prior high 102.20" in ev["confirmation"]


def test_support_hold_needs_confirmation():
    rows = HOLD_OK[:-1] + [(100.9, 102.1, 100.7, 102.1)]  # close not above prior high 102.2
    assert _fire("support_hold", rows) == []


def test_support_hold_without_test_is_no_signal():
    rows = [ABOVE] * 12 + [(103, 103.5, 101.5, 102), (102, 102.2, 101.0, 101.5), (101.6, 103, 101.2, 102.5)]
    assert _fire("support_hold", rows) == []


def test_support_hold_test_outside_lookback():
    rows = [ABOVE] * 10 + [(102, 102.2, 100.2, 100.8)] + [(101.5, 102.4, 101.2, 102)] * 3 + [(102, 103, 101.6, 102.8)]
    assert _fire("support_hold", rows) == []  # test bar is 4 bars back, lookback 3


def test_support_hold_close_below_zone_is_not_a_test():
    rows = HOLD_OK[:-2] + [(102, 102.2, 98.5, 99.0), (99.2, 103, 99.1, 102.5)]
    # bar N-1 closed below zone_low (99.0 < 99.5); bar N itself tested: low 99.1 ≤ 100.5 and closed 102.5 ≥ 99.5,
    # but N's prior close (99.0) is below zone_low → fails the prior-side condition (decision B4)
    assert _fire("support_hold", rows) == []


def test_b4_gap_from_below_is_not_support_hold():
    rows = [BELOW] * 12 + [(97, 98.5, 96.5, 98.0), (101, 102.5, 100.3, 102.2)]
    assert _fire("support_hold", rows) == []
    assert len(_fire("support_hold", rows, settings=_with(hold_require_prior_side=False))) == 1


def test_resistance_hold_positive_and_negative():
    rows = [BELOW] * 12 + [(98, 100.3, 97.8, 99.0), (98.8, 99.0, 96.5, 97.0)]
    assert len(_fire("resistance_hold", rows, origin="resistance")) == 1
    rows[-1] = (98.8, 99.0, 97.9, 98.0)  # close 98.0 not below prior low 97.8
    assert _fire("resistance_hold", rows, origin="resistance") == []


# ---------------------------------------------------------------- breaks


# 2-bar break (decision B5): N-1 and N both close beyond the zone; N full-bodied and engulfs N-1; the
# `break_prior_side_bars` (3) closes before N-1 stayed on the level's side.
FIRST_BELOW = (100.6, 100.8, 99.0, 99.2)  # bar N-1: first close below zone_low 99.5
BREAK_OK = [ABOVE] * 12 + [FIRST_BELOW, (100.7, 100.8, 97.9, 98.0)]


def test_support_break_positive():
    out = _fire("support_break", BREAK_OK)
    assert len(out) == 1
    _, ev = out[0]
    assert ev["body_ratio"] >= 0.7 and ev["beyond_zone_atr"] == 0.75 and ev["stretched"] is False
    assert ev["first_close_bar"]["close"] == 99.2 and ev["prior_side_bars"] == 3


def test_single_close_beyond_is_not_a_break():
    rows = [ABOVE] * 12 + [(100.6, 101, 100.2, 100.4), (100.7, 100.8, 97.9, 98.0)]  # N-1 closed inside the zone
    assert _fire("support_break", rows) == []


def test_reclaim_of_level_already_beyond_is_not_a_break():
    # UBER 2026-10-15 shape: price above resistance, N-1 dips back into the zone, N engulfs back above
    rows = [BELOW] * 8 + [ABOVE] * 4 + [(101.6, 101.8, 99.6, 99.8), (99.7, 102.0, 99.6, 101.8)]
    assert _fire("resistance_break", rows, origin="resistance") == []


def test_break_without_engulfing_is_no_signal():
    rows = BREAK_OK[:-1] + [(100.5, 100.6, 97.9, 98.0)]  # body top 100.5 < prior body top 100.6
    assert _fire("support_break", rows) == []


def test_engulfing_but_not_full_bodied_is_no_signal():
    rows = BREAK_OK[:-1] + [(100.7, 102.5, 96.0, 98.0)]  # body 2.7 / range 6.5
    assert _fire("support_break", rows) == []


def test_break_requires_second_close_beyond_zone():
    rows = BREAK_OK[:-1] + [(100.7, 100.8, 99.5, 99.6)]  # closes inside the zone
    assert _fire("support_break", rows) == []


def test_break_requires_prior_closes_on_level_side():
    rows = [ABOVE] * 11 + [BELOW, FIRST_BELOW, BREAK_OK[-1]]  # N-2 already closed below the zone
    assert _fire("support_break", rows) == []
    rows = [ABOVE] * 8 + [BELOW] + [ABOVE] * 3 + BREAK_OK[-2:]  # the close below is 4 bars before N-1
    assert len(_fire("support_break", rows)) == 1
    assert _fire("support_break", rows, settings=_with(break_prior_side_bars=4)) == []
    rows = [BELOW] * 12 + BREAK_OK[-2:]  # never on the support side
    assert _fire("support_break", rows) == []


def test_break_same_colour_engulfing_setting():
    assert BREAK_OK[-2][3] < BREAK_OK[-2][0]  # N-1 bearish, N bearish: same colour
    assert len(_fire("support_break", BREAK_OK)) == 1  # spec default allows same colour
    assert _fire("support_break", BREAK_OK, settings=_with(break_engulfing_require_opposite_color=True)) == []


RES_BREAK = [BELOW] * 12 + [(99.6, 100.9, 99.4, 100.7), (99.5, 102.2, 99.4, 102.0)]


def test_resistance_break_positive_and_stretched():
    out = _fire("resistance_break", RES_BREAK, origin="resistance")
    assert len(out) == 1 and out[0][1]["stretched"] is False
    rows = RES_BREAK[:-1] + [(99.5, 104.2, 99.4, 104.0)]
    assert _fire("resistance_break", rows, origin="resistance")[0][1]["stretched"] is True  # 1.75 ATR > 1.5


def test_break_suppresses_opposite_hold_on_same_level():
    # bar N dips into the zone, prior close above it, closes above N-1's high: shaped like a support hold,
    # but the level was resistance until the 2-bar break, so only the break is reported
    assert len(_fire("resistance_break", RES_BREAK, origin="resistance")) == 1
    assert _fire("support_hold", RES_BREAK, origin="resistance") == []


def test_near_break_first_close_and_in_zone():
    b = bars_from_ohlc([ABOVE] * 12 + [FIRST_BELOW])
    near = near_signals(tech_for(b, [hlevel(100.0, len(b))]), len(b) - 1, S)["support_break"][0]
    assert near.distance_atr == 0.0 and "first close below" in near.description
    b = bars_from_ohlc([ABOVE] * 12 + [(101, 101.5, 100.6, 101), (100.8, 101, 100.0, 100.2)])
    near = near_signals(tech_for(b, [hlevel(100.0, len(b))]), len(b) - 1, S)["support_break"][0]
    assert "no break yet" in near.description and near.triggers[0].price == 99.5
    b = bars_from_ohlc(BREAK_OK)  # fired → not a near-miss
    assert near_signals(tech_for(b, [hlevel(100.0, len(b))]), len(b) - 1, S).get("support_break", []) == []


# ---------------------------------------------------------------- patterns


def test_bullish_engulfing_opposite_colour_default():
    rows = [ABOVE] * 5 + [(101, 101.2, 99.8, 100), (99.9, 102.5, 99.7, 102.2)]
    out = _fire("bullish_engulfing", rows)
    assert len(out) == 1 and out[0][1]["at_level"] is True and out[0][0].id == "h_test"
    same = [ABOVE] * 5 + [(100, 101.2, 99.8, 101), (99.9, 102.5, 99.7, 102.2)]
    assert _fire("bullish_engulfing", same) == []  # decision B3: classic pattern by default
    assert len(_fire("bullish_engulfing", same, settings=_with(pattern_engulfing_require_opposite_color=False))) == 1


def test_bearish_engulfing_not_at_level():
    rows = [ABOVE] * 5 + [(110, 111.2, 109.8, 111), (111.1, 111.3, 109.0, 109.5)]
    out = _fire("bearish_engulfing", rows)
    assert len(out) == 1 and out[0][0] is None and out[0][1]["at_level"] is False


# ---------------------------------------------------------------- context


def test_extension_thresholds_inclusive():
    rows = [ABOVE] * 5
    assert len(_fire("extended_up", rows, rsi=70.0)) == 1
    assert _fire("extended_up", rows, rsi=69.9) == []
    assert len(_fire("extended_down", rows, rsi=30.0)) == 1
    assert _fire("extended_down", rows, rsi=30.1) == []


def test_approaching_support():
    rows = [ABOVE] * 5 + [(104, 104.5, 103.5, 104), (103, 103.5, 102.5, 103), (102.5, 103, 102, 102.5),
                          (102, 102.2, 101.6, 101.8)]
    out = _fire("approaching_support", rows)
    assert len(out) == 1 and out[0][1]["dist_to_zone_atr"] == 0.65
    far = rows[:-2] + [(103, 103.5, 102.5, 103.4), (103.1, 103.3, 102.9, 103.0)]  # 1.25 ATR > approach_atr 1.0
    assert _fire("approaching_support", far) == []
    assert len(_fire("approaching_support", far, settings=_with(approach_atr=1.5))) == 1
    away = [ABOVE] * 5 + [(101, 101.2, 100.8, 101), (101, 101.2, 100.8, 101), (101, 101.2, 100.8, 101),
                          (101.8, 102, 101.6, 101.8)]
    assert _fire("approaching_support", away) == []  # moving away from the level
    inside = rows[:-1] + [(100.4, 100.6, 100.2, 100.3)]
    assert _fire("approaching_support", inside) == []  # in the zone, not approaching


def test_range_signals():
    b = bars_from_ohlc([ABOVE] * 5)
    hi, lo = hlevel(110, 5, "resistance", "range_high"), hlevel(90, 5, "support", "range_low")
    tr = TrendResult("range", "t", [], hi, lo, 10.0, 0.5, True)
    t = tech_for(b, [], trend=tr)
    assert len(REGISTRY["range_bound"].fn(t, 4, S)) == 1 and len(REGISTRY["range_mid"].fn(t, 4, S)) == 1
    tr.range_mid = False
    assert REGISTRY["range_mid"].fn(t, 4, S) == []
    assert REGISTRY["range_bound"].fn(tech_for(b, []), 4, S) == []


# ---------------------------------------------------------------- evaluation, ages, PENDING_CLOSE


def test_pending_close_on_provisional_bar():
    b = bars_from_ohlc(HOLD_OK)
    lv = hlevel(100.0, len(b))
    n = len(b) - 1
    t_prov = tech_for(b, [lv], cutoff=n - 1)
    out = evaluate_signals([t_prov, t_prov], n, S)
    holds = [x for x in out if x.name == "support_hold"]
    assert [(x.age, x.status) for x in holds] == [(0, Tri.PENDING)]


def test_signal_age_one():
    rows = HOLD_OK + [(102.4, 102.8, 102.2, 102.6)]
    b = bars_from_ohlc(rows)
    lv = hlevel(100.0, len(b))
    n = len(b) - 1
    out = evaluate_signals([tech_for(b, [lv], n), tech_for(b, [lv], n - 1)], n, S)
    holds = [(x.age, x.status) for x in out if x.name == "support_hold"]
    assert (1, Tri.TRUE) in holds


def test_near_support_hold_and_break_triggers():
    rows = HOLD_OK[:-1] + [(100.9, 102.1, 100.7, 101.9)]  # tested, not yet confirmed
    b = bars_from_ohlc(rows)
    lv = hlevel(100.0, len(b))
    near = near_signals(tech_for(b, [lv]), len(b) - 1, S)
    hold = near["support_hold"][0]
    trig = hold.triggers[0]
    assert trig.label == "Hold trigger" and trig.price == 102.1
    brk = near_signals(tech_for(b, [lv]), len(b) - 1, S).get("support_break", [])
    assert brk == []  # close 101.9 is above the zone
    rows = [ABOVE] * 12 + [(101, 101.5, 100.6, 101), (100.8, 101, 100.0, 100.2)]
    b = bars_from_ohlc(rows)
    near = near_signals(tech_for(b, [hlevel(100.0, len(b))]), len(b) - 1, S)
    assert near["support_break"][0].triggers[0].price == 99.5
