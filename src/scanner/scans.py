"""The five scans (bullish, non-bearish, neutral, non-bullish, bearish): criterion evaluation, ranking by weighted %
met, and the watch list.

Each criterion is TRUE, FALSE or PENDING_CLOSE (it would be met if the forming bar closed now). A result's `pct` is
the weight of TRUE criteria over the total weight; PENDING_CLOSE counts as not met. A ticker is listed in every scan
whose `min_pct` it reaches. A signal criterion counts signals on the scan's latest `lookback_bars` bars (or the
criterion's own `within`).
"""

from __future__ import annotations

from scanner.alerts import build_alerts, primary_signal
from scanner.config import Criterion, Scan
from scanner.flow import classify_flow
from scanner.labels import humanize
from scanner.models import CriterionResult, NearResult, ScanResult, SignalResult, SymbolAnalysis, Tri, WatchItem
from scanner.signals import find_channel


def criterion_label(c: Criterion) -> str:
    """The criterion's own `label`, or a short generated one."""
    if c.label:
        return c.label
    v = c.value
    return {
        "trend": lambda: "Trend: " + " or ".join(humanize(t).lower() for t in v),
        "signal_all": lambda: "All of: " + ", ".join(humanize(n).lower() for n in v),
        "signal_any": lambda: "Any of: " + ", ".join(humanize(n).lower() for n in v),
        "rsi_below": lambda: f"RSI below {v:g}",
        "rsi_above": lambda: f"RSI above {v:g}",
        "rsi_between": lambda: f"RSI {v[0]:g}–{v[1]:g}",
        "flow": lambda: humanize(v),
        "channel": lambda: f"Horizontal channel, {v} bars",
    }[c.check]()


def _signal_status(name: str, signals: list[SignalResult]) -> tuple[Tri, list[SignalResult]]:
    hits = [s for s in signals if s.name == name]
    return Tri.any_([s.status for s in hits]), hits


def _signals(c: Criterion, signals: list[SignalResult], window: int,
             pnote: str) -> tuple[Tri, str, list[str], list[SignalResult]]:
    recent = [s for s in signals if s.age < window]
    parts, ev, live_all = [], [], []
    for name in c.value:
        st, hits = _signal_status(name, recent)
        parts.append(st)
        live = [h for h in hits if h.status is not Tri.FALSE]
        live_all += live
        ev += [f"{name}: {h.summary} [{h.status.value}]" for h in live] or [f"{name}: not fired{pnote}"]
    status = Tri.all_(parts) if c.check == "signal_all" else Tri.any_(parts)
    joiner = "all of" if c.check == "signal_all" else "any of"
    bars = "latest bar" if window == 1 else f"latest {window} bars"
    return status, f"{joiner} {list(c.value)} on the {bars}", ev, live_all


def _channel(c: Criterion, sa: SymbolAnalysis, settings) -> tuple[Tri, str, list[str]]:
    """Completed bars only: the window ends on the latest completed bar and its levels come from the bar before it."""
    n = c.channel
    end = sa.completed_idx
    start = end - n + 1
    age = sa.last_idx - (start - 1)
    if start < 2 or age >= len(sa.techs):
        return Tri.FALSE, f"not enough bars evaluated for a {n}-bar channel", []
    ch = find_channel(sa.techs[age], start, end, settings)
    if ch is None:
        return Tri.FALSE, f"no horizontal channel held every close of the last {n} completed bars", []
    sup, res = ch["support"], ch["resistance"]
    detail = (f"closes {ch['from']}–{ch['to']} stayed between {sup.label} {sup.value(end):.2f} and {res.label} "
              f"{res.value(end):.2f} (width {ch['width_atr']:g} ATR)")
    return Tri.TRUE, detail, [f"support touches {ch['support_touches']}, resistance touches {ch['resistance_touches']}"]


def _rsi(c: Criterion, rsi: float, provisional: bool) -> tuple[Tri, str]:
    v = c.value
    if c.check == "rsi_below":
        ok, rule = rsi < v, f"< {v:g}"
    elif c.check == "rsi_above":
        ok, rule = rsi > v, f"> {v:g}"
    else:
        ok, rule = v[0] <= rsi <= v[1], f"in [{v[0]:g}, {v[1]:g}]"
    # like a signal on the forming bar: a met condition is only pending until the close
    status = (Tri.PENDING if provisional else Tri.TRUE) if ok else Tri.FALSE
    return status, f"RSI {rsi:.1f} {rule}" + (" (forming bar)" if provisional else "")


def evaluate_criterion(c: Criterion, direction: str, sa: SymbolAnalysis, flow_label: str, comps: str,
                       pnote: str, window: int = 1, settings=None) -> CriterionResult:
    label = criterion_label(c)
    if c.check == "trend":
        state = sa.trend.state
        ok = state in c.value
        return CriterionResult("trend", label, Tri.of(ok), c.weight,
                               f"trend {state} {'∈' if ok else '∉'} {list(c.value)}", spec=c)
    if c.check in ("signal_all", "signal_any"):
        status, detail, ev, hits = _signals(c, sa.signals, window, pnote)
        return CriterionResult(c.check, label, status, c.weight, detail, ev, hits, spec=c)
    if c.check == "channel":
        status, detail, ev = _channel(c, sa, settings)
        return CriterionResult("channel", label, status, c.weight, detail, ev, spec=c)
    if c.check in ("rsi_below", "rsi_above", "rsi_between"):
        status, detail = _rsi(c, float(sa.bars.rsi[sa.last_idx]), sa.provisional)
        return CriterionResult(c.check, label, status, c.weight, detail, spec=c)
    ok = flow_label == "with_flow" if c.value == "with_flow" else flow_label != "countertrend"
    return CriterionResult("flow", label, Tri.of(ok), c.weight, f"{flow_label} for {direction} ({comps})", spec=c)


def evaluate_scan(direction: str, scan: Scan, sa: SymbolAnalysis, settings) -> ScanResult:
    """Every criterion of one scan for one symbol, with the alert prices of its primary signal."""
    if sa.flow is None:
        flow_label, comps = "mixed", "flow unavailable"
    else:
        flow_label = classify_flow(direction, [sa.flow.market, sa.flow.sector, sa.flow.name], settings)
        comps = "; ".join(f"{k}: {v}" for k, v in sa.flow.components())
    pnote = " (latest bar is provisional: signals on it are PENDING_CLOSE)" if sa.provisional else ""
    crits = [evaluate_criterion(c, direction, sa, flow_label, comps, pnote, scan.window(c), settings)
             for c in scan.criteria]
    res = ScanResult(sa.symbol, direction, crits, scan.min_pct, flow_label)
    res.primary = primary_signal(res.signals)
    res.alerts = build_alerts(res.primary, sa, settings)
    return res


def rank(results: list[ScanResult]) -> list[ScanResult]:
    """Weighted % met desc; on a tie confirmed before pending (less pending weight first), then symbol."""
    ordered = sorted(results, key=lambda r: (-round(r.pct, 9), round(r.pending_pct, 9), r.symbol))
    for n, r in enumerate(ordered, start=1):
        r.rank = n
    return ordered


# ----------------------------------------------------------------------------------------------
# Watch list
# ----------------------------------------------------------------------------------------------


def _near_gain(res: ScanResult, near_map: dict[str, list[NearResult]]) -> tuple[list[NearResult], float]:
    """Near-firing signals for the unmet signal criteria, and the weight those criteria would add if they fired."""
    nears: list[NearResult] = []
    gained = 0.0
    for c in res.criteria:
        if c.status is not Tri.FALSE or c.check not in ("signal_all", "signal_any"):
            continue
        names = c.spec.signal_names
        if c.check == "signal_any":
            got = [nr for n in names for nr in near_map.get(n, [])]
        else:
            fired = {h.name for h in c.signals}
            missing = [n for n in names if n not in fired]
            got = [nr for n in missing for nr in near_map.get(n, [])] if all(n in near_map for n in missing) else []
        if got:
            nears += got
            gained += c.weight
    return nears, gained


def watch_nears(res: ScanResult, near_map: dict[str, list[NearResult]]) -> list[NearResult]:
    """The near signals that would lift a result below its threshold to `min_pct` if they fired (else [])."""
    if res.passed or not res.total_weight:
        return []
    nears, gained = _near_gain(res, near_map)
    met = sum(c.weight for c in res.criteria if c.status is Tri.TRUE)
    if not nears or (met + gained) / res.total_weight * 100 < res.min_pct - 1e-9:
        return []
    return nears


def build_watchlist(results: dict[str, dict[str, ScanResult]], analyses: dict[str, SymbolAnalysis],
                    s) -> list[WatchItem]:
    items: dict[tuple, WatchItem] = {}
    for sym in sorted(results):
        sa = analyses[sym]
        for name, res in results[sym].items():
            for nr in watch_nears(res, sa.near):
                key = (sym, nr.signal, nr.level.id if nr.level is not None else "")
                if key not in items:
                    items[key] = WatchItem(sym, [], nr, nr.level.quality if nr.level is not None else 0.0)
                if name not in items[key].scans:
                    items[key].scans.append(name)
    ordered = sorted(items.values(), key=lambda w: w.sort_key)
    per_sym: dict[str, int] = {}
    out = []
    for w in ordered:
        if per_sym.get(w.symbol, 0) >= s.display.max_watch_items_per_symbol:
            continue
        per_sym[w.symbol] = per_sym.get(w.symbol, 0) + 1
        out.append(w)
        if len(out) >= s.display.max_watch_items:
            break
    return out
