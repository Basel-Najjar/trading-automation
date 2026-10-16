"""Strategy matching (§6.6, §7.3). Always returns the full rule trace (used by explain, cards, deep dives)."""

from __future__ import annotations

from dataclasses import dataclass

from scanner.config import Strategy
from scanner.flow import classify_flow
from scanner.models import FlowResult, NearResult, RuleResult, SignalResult, Tri


@dataclass
class StrategyEval:
    strategy: Strategy
    status: Tri
    rules: list[RuleResult]
    qualifying: list[SignalResult]
    flow_label: str
    signal_status: dict[str, Tri]

    @property
    def non_signal_ok(self) -> bool:
        return all(r.status is Tri.TRUE for r in self.rules if not r.rule.startswith("signals_"))


def signal_status(name: str, signals: list[SignalResult]) -> tuple[Tri, list[SignalResult]]:
    hits = [s for s in signals if s.name == name]
    return Tri.any_([s.status for s in hits]), hits


def flow_label_for(bias: str, flow: FlowResult | None, settings) -> str:
    if flow is None:
        return "mixed"
    return classify_flow(bias, [flow.market, flow.sector, flow.name], settings)


def evaluate_strategy(strat: Strategy, trend_state: str, signals: list[SignalResult], flow: FlowResult | None,
                      settings, provisional: bool = False) -> StrategyEval:
    tech = strat.technical
    rules: list[RuleResult] = []
    statuses: dict[str, Tri] = {}
    qualifying: list[SignalResult] = []
    pnote = " (latest bar is provisional: signals on it are PENDING_CLOSE)" if provisional else ""

    if tech.trend == ["any"]:
        rules.append(RuleResult("trend", Tri.TRUE, f"trend {trend_state}; strategy allows any"))
    else:
        ok = trend_state in tech.trend
        rules.append(RuleResult("trend", Tri.of(ok), f"trend {trend_state} {'∈' if ok else '∉'} {tech.trend}"))

    def _sig_rule(rule: str, names: list[str], combine) -> None:
        parts, ev = [], []
        for name in names:
            st, hits = signal_status(name, signals)
            statuses[name] = st
            parts.append(st)
            live = [h for h in hits if h.status is not Tri.FALSE]
            qualifying.extend(live)
            ev += [f"{name}: {h.summary} [{h.status.value}]" for h in live] or [f"{name}: not fired{pnote}"]
        st = combine(parts)
        joiner = "all of" if rule == "signals_all" else "any of"
        rules.append(RuleResult(rule, st, f"{joiner} {names}", ev))

    if tech.signals_all:
        _sig_rule("signals_all", tech.signals_all, Tri.all_)
    if tech.signals_any:
        _sig_rule("signals_any", tech.signals_any, Tri.any_)

    ne = strat.not_extended_resolved
    if ne in ("up", "down"):
        name = f"extended_{ne}"
        st, hits = signal_status(name, [s for s in signals if s.age == 0])
        statuses[name] = st
        rsi = next((h.evidence.get("rsi") for h in hits), None)
        detail = f"must not be {name}" + (f" (RSI {rsi})" if rsi is not None else " (RSI within threshold)")
        rules.append(RuleResult("not_extended", st.negate(), detail))

    label = flow_label_for(strat.bias, flow, settings)
    comps = "; ".join(f"{k}: {v}" for k, v in flow.components()) if flow else "flow unavailable"
    align = tech.alignment
    if align == "required":
        rules.append(RuleResult("alignment", Tri.of(label != "countertrend"), f"required; {label} ({comps})"))
    elif align == "soft":
        rules.append(RuleResult("alignment", Tri.TRUE, f"soft; {label} ({comps})"))
    else:
        rules.append(RuleResult("alignment", Tri.TRUE, f"ignored ({comps})"))
        label = "ignored"

    status = Tri.all_([r.status for r in rules])
    # de-duplicate qualifying signals (same signal object can appear in both lists)
    seen, uniq = set(), []
    for q in qualifying:
        if id(q) not in seen:
            seen.add(id(q))
            uniq.append(q)
    return StrategyEval(strat, status, rules, uniq, label, statuses)


def watch_nears(ev: StrategyEval, near_map: dict[str, list[NearResult]]) -> list[NearResult]:
    """Near-misses (WL-1): every non-signal rule passes and the signal rules are within reach."""
    if ev.status is not Tri.FALSE or not ev.non_signal_ok:
        return []
    tech = ev.strategy.technical
    nears: list[NearResult] = []
    for name in tech.signals_all:
        if ev.signal_status.get(name) is Tri.TRUE:
            continue
        if name not in near_map:
            return []
        nears += near_map[name]
    if tech.signals_any and Tri.any_([ev.signal_status.get(n, Tri.FALSE) for n in tech.signals_any]) is not Tri.TRUE:
        any_nears = [nr for n in tech.signals_any for nr in near_map.get(n, [])]
        if not any_nears:
            return []
        nears += any_nears
    return nears
