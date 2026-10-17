"""Criteria view model: the exact scan logic of each strategy, with the live settings values it is evaluated with.

Pure functions over a `Config` (no Streamlit). The rule list mirrors `strategies.evaluate_strategy` rule for rule and
in the same order, so the Criteria tab and the Results rule trace use the same names; a test guards that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scanner.config import Config, Settings, Strategy
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
    rule: str
    summary: str
    details: list[str] = field(default_factory=list)
    signals: list[SignalCriterion] = field(default_factory=list)
    table: list[tuple[str, str, str]] = field(default_factory=list)  # flow table: bias, agree, oppose


@dataclass
class StrategyCriteria:
    strategy: Strategy
    active: bool
    overrides: dict[str, Any]
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


def _trend_rule(strat: Strategy, s: Settings) -> RuleCriterion:
    states = strat.technical.trend
    if states == ["any"]:
        return RuleCriterion("trend", "Any trend state (always passes).")
    return RuleCriterion("trend", f"The symbol's trend state must be one of: {', '.join(f'`{t}`' for t in states)}.",
                         [f"`{t}`: {TREND_RULES[t]}" for t in states] + [_tol(s)])


def _signals_rule(rule: str, names: list[str], s: Settings) -> RuleCriterion:
    joiner = "**All** of these signals must fire" if rule == "signals_all" else "**At least one** of these must fire"
    age = s.signals.max_signal_age_bars
    when = ("on the latest bar" if age == 0
            else f"on the latest bar or up to **{age}** bar{'s' if age != 1 else ''} earlier")
    return RuleCriterion(
        rule, f"{joiner} {when}.",
        ["Each earlier bar is evaluated with only the data that existed on it.",
         "On a still-forming bar a signal that would fire is PENDING_CLOSE, which counts as not met."],
        [signal_criterion(n, s) for n in names])


def _not_extended_rule(strat: Strategy, s: Settings) -> RuleCriterion | None:
    ne = strat.not_extended_resolved
    if ne not in ("up", "down"):
        return None
    how = f"`not_extended: {strat.technical.not_extended}`" + (f" → `{ne}` from bias `{strat.bias}`"
                                                              if strat.technical.not_extended == "auto" else "")
    period = s.indicators.rsi_period
    if ne == "up":
        cond = f"RSI({period}) ≥ `rsi_overbought` = **{_fmt(s.indicators.rsi_overbought)}**"
    else:
        cond = f"RSI({period}) ≤ `rsi_oversold` = **{_fmt(s.indicators.rsi_oversold)}**"
    return RuleCriterion("not_extended", f"Fails if `extended_{ne}` fires on the latest bar: {cond}.",
                         [f"From {how}. Only the latest bar is checked, not earlier ones."])


def _alignment_rule(strat: Strategy, s: Settings) -> RuleCriterion:
    mode = strat.technical.alignment
    w = s.scoring.weights.get("flow", 0)
    summary = {
        "soft": f"`soft`: always passes. The flow label is recorded on the candidate and scores the flow component "
                f"(weight {_fmt(w)}).",
        "required": "`required`: fails if the flow label is **countertrend**.",
        "ignore": f"`ignore`: always passes; flow is not considered (scores "
                  f"{_fmt(s.scoring.flow_scores.get('ignored', 0.5))} on the flow component).",
    }[mode]
    if mode == "ignore":
        return RuleCriterion("alignment", summary)
    agree, oppose = s.flow.agree.get(strat.bias, []), s.flow.oppose.get(strat.bias, [])
    return RuleCriterion(
        "alignment", summary,
        [f"Components: market (`{s.data.benchmark}` trend), sector (sector ETF / {s.data.benchmark}), name "
         "(symbol / sector ETF; `n/a` without a sector mapping). Ratios use the same trend logic.",
         f"Label: {FLOW_LABELS}"],
        table=[(strat.bias, ", ".join(agree) or "—", ", ".join(oppose) or "—")])


def strategy_rules(strat: Strategy, s: Settings) -> list[RuleCriterion]:
    """Rules in the order `evaluate_strategy` emits them."""
    tech = strat.technical
    rules = [_trend_rule(strat, s)]
    if tech.signals_all:
        rules.append(_signals_rule("signals_all", tech.signals_all, s))
    if tech.signals_any:
        rules.append(_signals_rule("signals_any", tech.signals_any, s))
    if (ne := _not_extended_rule(strat, s)) is not None:
        rules.append(ne)
    rules.append(_alignment_rule(strat, s))
    return rules


def _outcome(strat: Strategy, s: Settings) -> list[str]:
    tech = strat.technical
    sig_names = tech.signals_all + tech.signals_any
    nears = [n for n in sig_names if REGISTRY[n].near is not None]
    out = ["**Candidate** when every rule is met. If any rule is only PENDING_CLOSE the candidate is pending and "
           "ranks after confirmed ones."]
    if sig_names:
        watch = ("**Watch list** when every non-signal rule passes and each missing signal is near-firing"
                 + (f" (near-miss checks exist for {', '.join(f'`{n}`' for n in nears)})." if nears
                    else "; none of its signals has a near-miss check, so it never reaches the watch list."))
        out.append(watch)
    out.append(f"Candidates are ranked by score (0–100) across {len(s.scoring.weights)} weighted components.")
    return out


def strategy_criteria(cfg: Config) -> list[StrategyCriteria]:
    out = []
    for strat in cfg.strategies:
        s = cfg.settings.with_overrides(strat.technical.overrides)
        # an inactive strategy with no technical rules (a stub) has nothing to show
        rules = [] if strat.technical.is_empty and not strat.active else strategy_rules(strat, s)
        out.append(StrategyCriteria(strat, strat.active, dict(strat.technical.overrides), rules,
                                    _outcome(strat, s) if strat.active else []))
    return out


def global_criteria(cfg: Config) -> list[tuple[str, str]]:
    """Definitions every strategy shares, with live values: (term, markdown definition)."""
    s = cfg.settings
    k = _fmt(s.levels.zone_atr_mult)
    return [
        ("Bar N", "The latest bar evaluated: the latest completed bar, or the forming bar during market hours. "
                  "**N-1** is the bar before it."),
        ("ATR", f"Wilder ATR({s.indicators.atr_period}) on the bar being tested."),
        ("Zone", f"`[level − k·ATR, level + k·ATR]` with `zone_atr_mult` k = **{k}**. For SMAs and diagonals the "
                 "level is the line's value on that bar, so the zone moves with it."),
        ("Levels", f"SMA50/SMA200 (support when price is above, resistance below); horizontal clusters of pivots "
                   f"(spread ≤ `cluster_atr_mult` = **{_fmt(s.levels.cluster_atr_mult)}** × ATR, ≥ "
                   f"**{s.levels.min_touches_horizontal}** touches); diagonal trendlines through two pivots within "
                   f"**{s.levels.trendline_lookback_bars}** bars (≥ **{s.levels.min_touches_diagonal}** touches, best "
                   f"**{s.levels.max_trendlines_per_side}** per side); manual levels from `manual_levels.yaml`."),
        ("Pivots", f"A pivot high's high is above each of the **{s.pivots.pivot_window}** bars before it and ≥ each of "
                   "the same number after; it exists only once those later bars have closed. Pivot lows mirror this."),
        ("Full-bodied", f"`body / range ≥ full_body_ratio` = **{_fmt(s.signals.full_body_ratio)}**; a zero-range bar "
                        "never is."),
        ("Engulfing", f"Bar N's body covers bar N-1's body within `engulfing_tolerance_atr` = "
                      f"**{_fmt(s.signals.engulfing_tolerance_atr)}** × ATR. Opposite colours required for breaks: "
                      f"**{_fmt(s.signals.break_engulfing_require_opposite_color)}**; for patterns: "
                      f"**{_fmt(s.signals.pattern_engulfing_require_opposite_color)}**."),
        ("PENDING_CLOSE", "During market hours a signal that would fire on the forming bar is pending and counts as "
                          "not met. Pivots, levels and trend never use a forming bar."),
    ]
