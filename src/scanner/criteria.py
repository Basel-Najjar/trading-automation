"""Criteria view model: the exact logic of each scan, with the live settings values it is evaluated with.

Pure functions over a `Config` (no Streamlit). The criterion list mirrors `scans.evaluate_scan` one for one and in the
same order, so the Criteria tab and the Results checklist use the same labels; a test guards that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scanner.config import Config, Criterion, Scan, Settings
from scanner.scans import criterion_label
from scanner.signals import REGISTRY

TREND_RULES = {
    "uptrend": "the last two confirmed pivot highs are higher **and** the last two confirmed pivot lows are higher",
    "downtrend": "the last two confirmed pivot highs are lower **and** the last two confirmed pivot lows are lower",
    "range": "pivot highs equal **and** pivot lows equal, and the close lies between the range low's zone low and the "
             "range high's zone high",
    "mixed": "anything else, including fewer than two confirmed pivots on either side",
}

FLOW_LABELS = ("**countertrend** if opposing components outnumber agreeing ones; **with_flow** if none oppose and at "
               "least one agrees; **mixed** otherwise. `mixed`/`n/a` components count as neutral.")


@dataclass
class SignalCriterion:
    name: str
    kind: str
    direction: str
    description: str
    params: list[tuple[str, Any]]
    has_near: bool


@dataclass
class RuleCriterion:
    check: str
    label: str
    weight: float
    summary: str
    details: list[str] = field(default_factory=list)
    signals: list[SignalCriterion] = field(default_factory=list)
    table: list[tuple[str, str, str]] = field(default_factory=list)  # flow table: direction, agree, oppose


@dataclass
class ScanCriteria:
    name: str
    scan: Scan
    active: bool
    rules: list[RuleCriterion]
    outcome: list[str]


def param_value(s: Settings, name: str) -> Any:
    """A settings value by its bare key, searched across the sections signal parameters live in."""
    for section in (s.signals, s.levels, s.indicators, s.range, s.pivots):
        if name in type(section).model_fields:
            return getattr(section, name)
    raise KeyError(name)


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (list, tuple)):
        return "–".join(_fmt(x) for x in v)
    return f"{v:g}" if isinstance(v, float) else str(v)


def signal_text(name: str, s: Settings) -> str:
    """The registry description with each backticked parameter shown with its live value; stale defaults dropped."""
    d = REGISTRY[name]
    text = re.sub(r"\s*\(default [^)]*\)", "", d.description)
    for p in d.params:
        text = text.replace(f"`{p}`", f"`{p}` = **{_fmt(param_value(s, p))}**")
    return text


def signal_criterion(name: str, s: Settings) -> SignalCriterion:
    d = REGISTRY[name]
    return SignalCriterion(name, d.kind, d.direction, signal_text(name, s),
                           [(p, param_value(s, p)) for p in d.params], d.near is not None)


def _tol(s: Settings) -> str:
    return (f"tolerance = `cluster_atr_mult` × ATR({s.indicators.atr_period}) = **{_fmt(s.levels.cluster_atr_mult)}** "
            f"× ATR; pivots need `pivot_window` = **{s.pivots.pivot_window}** bars each side and are confirmed only "
            "once those later bars have closed")


def _rule(c: Criterion, direction: str, s: Settings, window: int = 1) -> RuleCriterion:
    label, w, v = criterion_label(c), c.weight, c.value
    if c.check == "trend":
        return RuleCriterion("trend", label, w, f"The trend state must be one of: {', '.join(f'`{t}`' for t in v)}.",
                             [f"`{t}`: {TREND_RULES[t]}" for t in v] + [_tol(s)])
    if c.check in ("signal_all", "signal_any"):
        joiner = "**All** of these signals must fire" if c.check == "signal_all" else "**At least one** must fire"
        when = ("on the latest bar" if window == 1
                else f"on any of the latest **{window}** bars" + (" (this criterion's `within`)" if c.within else ""))
        return RuleCriterion(c.check, label, w, f"{joiner} {when}.",
                             ["Each earlier bar is evaluated with only the data that existed on it."],
                             [signal_criterion(n, s) for n in v])
    if c.check == "channel":
        return RuleCriterion(
            "channel", label, w,
            f"Every close of the last **{v}** completed bars stayed between a horizontal support and a horizontal "
            "resistance.",
            ["Support and resistance are horizontal levels (pivot clusters, range boundaries or manual horizontal "
             "levels) known on the bar before the window; sloped lines never count.",
             "No close below the support's zone low or above the resistance's zone high, and the two zones do not "
             "overlap.",
             f"Each side's zone was touched by at least `channel_min_touches` = **{s.range.channel_min_touches}** "
             "bar(s) of the window (low into the support zone, high into the resistance zone).",
             "Uses completed bars only, so it is never pending."])
    if c.check in ("rsi_below", "rsi_above", "rsi_between"):
        period = s.indicators.rsi_period
        cond = {"rsi_below": lambda: f"< **{_fmt(v)}**", "rsi_above": lambda: f"> **{_fmt(v)}**",
                "rsi_between": lambda: f"between **{_fmt(v[0])}** and **{_fmt(v[1])}** (inclusive)"}[c.check]()
        return RuleCriterion(c.check, label, w, f"RSI({period}) on the latest bar is {cond}.")
    want = ("the flow label is **with_flow**" if v == "with_flow" else "the flow label is not **countertrend**")
    agree, oppose = s.flow.agree.get(direction, []), s.flow.oppose.get(direction, [])
    return RuleCriterion(
        "flow", label, w, f"Met when {want}.",
        [f"Components: market (`{s.data.benchmark}` trend), sector (sector ETF / {s.data.benchmark}), name "
         "(symbol / sector ETF; `n/a` without a sector mapping). Ratios use the same trend logic.",
         f"Label: {FLOW_LABELS}"],
        table=[(direction, ", ".join(agree) or "—", ", ".join(oppose) or "—")])


def scan_rules(direction: str, scan: Scan, s: Settings) -> list[RuleCriterion]:
    """Criteria in the order `evaluate_scan` emits them."""
    return [_rule(c, direction, s, scan.window(c)) for c in scan.criteria]


def _outcome(scan: Scan) -> list[str]:
    total = sum(c.weight for c in scan.criteria)
    weights = ", ".join(f"{criterion_label(c)} **{_fmt(c.weight)}**" for c in scan.criteria)
    return [f"**Match %** = weight of the criteria met ÷ total weight ({_fmt(total)}). Weights: {weights}.",
            f"Listed when the match is at least **{_fmt(scan.min_pct)}%**, highest first. A ticker can be listed in "
            "more than one scan.",
            "On a still-forming bar a criterion that would be met is PENDING_CLOSE, which counts as not met; at equal "
            "match, confirmed tickers rank first.",
            "**Watch list** when firing a near signal would lift the ticker to the threshold."]


def scan_criteria(cfg: Config) -> list[ScanCriteria]:
    s = cfg.settings
    return [ScanCriteria(name, sc, sc.active, scan_rules(name, sc, s), _outcome(sc) if sc.active else [])
            for name, sc in cfg.scans.items()]


def global_criteria(cfg: Config) -> list[tuple[str, str]]:
    """Definitions every scan shares, with live values: (term, markdown definition)."""
    s = cfg.settings
    k = _fmt(s.levels.zone_atr_mult)
    return [
        ("Bar N", "The latest bar evaluated: the latest completed bar, or the forming bar during market hours. "
                  "**N-1** is the bar before it."),
        ("ATR", f"Wilder ATR({s.indicators.atr_period}) on the bar being tested."),
        ("Zone", f"`[level − k·ATR, level + k·ATR]` with `zone_atr_mult` k = **{k}**. For SMAs and diagonals the "
                 "level is the line's value on that bar, so the zone moves with it."),
        ("Levels", f"SMA50/SMA200 (support when price is above, resistance below); horizontal clusters of pivots "
                   f"(width ≤ `cluster_atr_mult` = **{_fmt(s.levels.cluster_atr_mult)}** × ATR, ≥ "
                   f"**{s.levels.min_touches_horizontal}** touches); diagonal trendlines through two pivots within "
                   f"**{s.levels.trendline_lookback_bars}** bars (≥ **{s.levels.min_touches_diagonal}** touches, best "
                   f"**{s.levels.max_trendlines_per_side}** per side); manual levels from `manual_levels.yaml`."),
        ("Pivots", f"A pivot high's high is above each of the **{s.pivots.pivot_window}** bars before it and ≥ each of "
                   "the same number after; it exists only once those later bars have closed. Pivot lows mirror this."),
        ("Full-bodied", f"`body / range ≥ full_body_ratio` = **{_fmt(s.signals.full_body_ratio)}**; a zero-range bar "
                        "never is."),
        ("2-bar break", "Bars N-1 and N both close beyond the zone after price was on the level's side. With "
                        f"`break_confirm` = **{s.signals.break_confirm}**: "
                        + ("both bars are full-bodied candles in the break direction and N closes beyond N-1's high "
                           "(bullish) or low (bearish)." if s.signals.break_confirm == "prior_extreme"
                           else "N is full-bodied in the break direction and its body engulfs N-1's.")),
        ("Slope", "Horizontal levels are flat. Every other level (diagonal trendline, SMA) is rising or falling by its "
                  "change into the bar: rising lines that held count toward non-bearish, falling ones toward "
                  "non-bullish, whether price held above or below them."),
        ("Engulfing", f"Bar N's body covers bar N-1's body within `engulfing_tolerance_atr` = "
                      f"**{_fmt(s.signals.engulfing_tolerance_atr)}** × ATR. Opposite colours required for breaks "
                      f"(engulf mode): **{_fmt(s.signals.break_engulfing_require_opposite_color)}**; for patterns: "
                      f"**{_fmt(s.signals.pattern_engulfing_require_opposite_color)}**."),
        ("PENDING_CLOSE", "During market hours a criterion that would be met on the forming bar is pending and counts "
                          "as not met. Pivots, levels and trend never use a forming bar."),
    ]
