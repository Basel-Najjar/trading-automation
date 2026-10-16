"""Outputs (§10): self-contained HTML report, candidates CSV/JSON, TradingView list, run manifest."""

from __future__ import annotations

import csv
import json
import platform
import shutil
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import jinja2
import numpy as np
import pandas as pd

from scanner import __version__
from scanner.charts import chart_levels, svg_chart
from scanner.config import Config, Strategy
from scanner.flow import ratio_trend, state_of
from scanner.levels import confluence_types
from scanner.models import Candidate, Level, SignalResult, SymbolAnalysis, Tri, WatchItem
from scanner.pipeline import RunResult
from scanner.scoring import fill_placeholders
from scanner.strategies import StrategyEval

DISCLAIMER = ("Automated technical screening based on the owner's predefined rules; not investment advice. "
              "Manual validation (volatility, fundamentals, events, macro) is required before any trade.")


def _r(x: Any, nd: int = 4) -> Any:
    if x is None:
        return None
    if isinstance(x, (float, np.floating)):
        return None if np.isnan(x) else round(float(x), nd)
    return x


def _clean(obj: Any) -> Any:
    """Make evidence dicts JSON-safe and deterministic."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return _r(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def level_dict(lv: Level, sa: SymbolAnalysis, k: float) -> dict:
    t = sa.last_idx
    lo, hi = lv.zone(t, sa.bars.atr, k)
    c = float(sa.bars.close[t])
    role = lv.role(t, c)
    return _clean({
        "id": lv.id, "type": lv.type, "manual": lv.manual, "origin": lv.origin, "role": role,
        "role_reversal": lv.origin is not None and role != lv.origin, "level": lv.value(t), "zone_low": lo,
        "zone_high": hi, "touches": lv.touches, "anchors": [[d, p] for d, p in lv.anchors],
        "last_touch": lv.touch_dates[-1] if lv.touch_dates else None, "slope": lv.slope, "quality": lv.quality,
        "dist_pct": (lv.value(t) - c) / c * 100, "dist_atr": (lv.value(t) - c) / float(sa.bars.atr[t]),
        "note": lv.note,
    })


def signal_dict(sig: SignalResult) -> dict:
    return _clean({"name": sig.name, "status": sig.status.value, "age": sig.age, "date": sig.date,
                   "kind": sig.kind, "direction": sig.direction, "level_id": sig.level.id if sig.level else None,
                   "summary": sig.summary, "evidence": sig.evidence})


def candidate_dict(c: Candidate, sa: SymbolAnalysis, k: float) -> dict:
    return _clean({
        "rank": c.rank, "strategy_rank": c.strategy_rank, "symbol": c.symbol, "strategy_id": c.strategy_id,
        "strategy": c.strategy_name, "bias": c.bias, "status": c.status.value, "score": c.score,
        "stretched": c.stretched, "flow": c.flow_label,
        "primary_signal": signal_dict(c.primary) if c.primary else None,
        "primary_level": level_dict(c.primary.level, sa, k) if c.primary and c.primary.level else None,
        "signals": [signal_dict(s) for s in c.signals],
        "rules": [{"rule": r.rule, "status": r.status.value, "detail": r.detail, "evidence": r.evidence}
                  for r in c.rules],
        "score_breakdown": [{"component": x.name, "raw": x.raw, "weight": x.weight, "points": x.points,
                             "note": x.note} for x in c.components],
        "alerts": [{"label": a.label, "price": a.price, "meaning": a.meaning} for a in c.alerts],
        "trend": sa.trend.state, "close": float(sa.bars.close[sa.last_idx]), "rsi": float(sa.bars.rsi[sa.last_idx]),
        "as_of": sa.as_of,
    })


def watch_dict(w: WatchItem) -> dict:
    n = w.near
    return _clean({"symbol": w.symbol, "strategies": [s for s, _ in w.strategies], "signal": n.signal,
                   "level_id": n.level.id if n.level else None, "level_type": n.level.label if n.level else None,
                   "description": n.description, "distance_atr": n.distance_atr,
                   "triggers": [{"label": a.label, "price": a.price, "meaning": a.meaning} for a in n.triggers]})


def _versions() -> dict[str, str]:
    out = {"python": platform.python_version(), "scanner": __version__}
    for pkg in ("pandas", "numpy", "yfinance", "exchange-calendars", "pydantic", "jinja2"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = "n/a"
    return out


# ----------------------------------------------------------------------------------------------
# View model
# ----------------------------------------------------------------------------------------------


def _plan_items(strat: Strategy, sa: SymbolAnalysis, s) -> list[tuple[str, Any]]:
    items = []
    for key, val in strat.plan.items():
        if isinstance(val, list):
            items.append((key, [fill_placeholders(str(v), sa, s) for v in val]))
        else:
            items.append((key, fill_placeholders(str(val), sa, s)))
    return items


def _strategy_view(ev: StrategyEval, cand: Candidate | None, sa: SymbolAnalysis, s) -> dict:
    st = ev.strategy
    extra = {k: v for k, v in st.extra_fields.items()}
    return {
        "id": st.id, "name": st.name, "bias": st.bias, "status": st.status, "description": st.description,
        "match": ev.status.value, "flow": ev.flow_label, "rules": ev.rules, "cand": cand,
        "construction": st.construction, "orats": st.orats_checklist, "manual": st.other_manual_checks,
        "plan": _plan_items(st, sa, s), "extra": extra,
    }


def _completed_close(sa: SymbolAnalysis) -> pd.Series:
    n = sa.completed_idx + 1
    return pd.Series(sa.bars.close[:n], index=pd.DatetimeIndex(sa.bars.dates[:n]))


def _rsi_verdict(rsi: float, s) -> str:
    if np.isnan(rsi):
        return "n/a"
    if rsi >= s.indicators.rsi_overbought:
        return "extended up"
    if rsi <= s.indicators.rsi_oversold:
        return "extended down"
    return "not extended"


def build_view(res: RunResult, cfg: Config, deep: bool = False) -> dict:
    s = res.settings
    k = s.levels.zone_atr_mult
    tz = ZoneInfo(s.report.timezone)
    by_sym: dict[str, list[Candidate]] = {}
    for c in res.candidates:
        by_sym.setdefault(c.symbol, []).append(c)

    rows = []
    for sym, cands in sorted(by_sym.items(), key=lambda kv: kv[1][0].rank):
        sa = res.analyses[sym]
        best = cands[0]
        t = sa.last_idx
        near = (sa.nearest_support[:1] + sa.nearest_resistance[:1])
        nearest = min(near, key=lambda r: abs(r["dist_atr"]), default=None)
        alert = next((a for a in best.alerts if a.label == "Invalidation"), best.alerts[0] if best.alerts else None)
        rows.append({
            "rank": best.rank, "symbol": sym, "price": float(sa.bars.close[t]), "cands": cands, "best": best,
            "trend": sa.trend.state, "rsi": float(sa.bars.rsi[t]), "nearest": nearest, "alert": alert,
            "pending": all(c.pending for c in cands),
            "strategies": " ".join(c.strategy_id for c in cands), "biases": " ".join(sorted({c.bias for c in cands})),
            "signals": " ".join(sorted({c.primary.name for c in cands if c.primary})),
            "flows": " ".join(sorted({c.flow_label for c in cands})),
        })

    card_syms = list(by_sym)
    if deep and res.focus_symbols:
        card_syms = [x for x in res.focus_symbols if x in res.analyses]
    cards = []
    for sym in card_syms:
        sa = res.analyses[sym]
        cands = by_sym.get(sym, [])
        cmap = {c.strategy_id: c for c in cands}
        evs = res.evals.get(sym, [])
        shown = [ev for ev in evs if deep or ev.strategy.id in cmap]
        sig_levels = [sg.level for c in cands for sg in c.signals if sg.level is not None]
        if deep:
            sig_levels += [sg.level for sg in sa.signals if sg.level is not None and sg.kind != "context"]
        sig_bars = [sg.bar_idx for c in cands for sg in c.signals] or [sg.bar_idx for sg in sa.signals
                                                                      if sg.kind in ("break", "hold", "pattern")]
        lv_rows, seen = [], set()
        for lv in chart_levels(sa, sig_levels) + [r["level"] for r in sa.nearest_support + sa.nearest_resistance]:
            if lv.id not in seen:
                seen.add(lv.id)
                d = level_dict(lv, sa, k)
                tech0 = sa.techs[0]
                d["confluence"] = confluence_types(lv, tech0.levels, sa.last_idx, sa.bars, k)
                lv_rows.append(d)
        lv_rows.sort(key=lambda d: -d["level"] if d["level"] is not None else 0)
        alerts, aseen = [], set()
        for c in cands:
            for a in c.alerts:
                key = (a.label, round(a.price, 2))
                if key not in aseen:
                    aseen.add(key)
                    alerts.append(a)
        t = sa.last_idx
        prev = float(sa.bars.close[t - 1]) if t > 0 else float("nan")
        cards.append({
            "symbol": sym, "sa": sa, "cands": cands, "best": cands[0] if cands else None,
            "close": float(sa.bars.close[t]), "change": float(sa.bars.close[t]) - prev,
            "change_pct": (float(sa.bars.close[t]) / prev - 1) * 100 if prev else 0.0,
            "chart": svg_chart(sa, chart_levels(sa, sig_levels), sig_bars, s.report.chart_bars, k,
                               s.indicators.rsi_overbought, s.indicators.rsi_oversold),
            "levels": lv_rows, "confluence": [_clean(z) for z in sa.confluence],
            "signals": [sg for sg in sa.signals], "rsi": float(sa.bars.rsi[t]),
            "rsi_verdict": _rsi_verdict(float(sa.bars.rsi[t]), s), "relvol": float(sa.bars.relvol[t]),
            "strategies": [_strategy_view(ev, cmap.get(ev.strategy.id), sa, s) for ev in shown],
            "alerts": alerts, "returns": sa.returns,
        })

    bench = res.context.get(res.benchmark) or res.analyses.get(res.benchmark)
    market = None
    if bench is not None:
        market = {"symbol": res.benchmark, "trend": bench.trend, "close": float(bench.bars.close[bench.last_idx]),
                  "supports": [level_dict(r["level"], bench, k) for r in bench.nearest_support],
                  "resistances": [level_dict(r["level"], bench, k) for r in bench.nearest_resistance],
                  "returns": bench.returns}
    sectors = []
    for etf in res.sector_etfs:
        sa = res.context.get(etf) or res.analyses.get(etf)
        if sa is None:
            sectors.append({"etf": etf, "trend": "n/a", "vs": "n/a", "returns": {}, "members": []})
            continue
        vs = "n/a"
        if bench is not None:
            ec, bc = _completed_close(sa), _completed_close(bench)
            vs = state_of(ratio_trend(ec, bc, s))
        sectors.append({"etf": etf, "trend": sa.trend.state, "vs": vs, "returns": sa.returns,
                        "members": sorted(x for x in res.analyses if cfg.sector_for(x) == etf)})

    started = (res.started or datetime.now()).astimezone(tz)
    bars_through = max((sa.bars.dates[sa.completed_idx] for sa in res.analyses.values()), default=None)
    filt = {
        "strategies": sorted({c.strategy_id for c in res.candidates}),
        "biases": sorted({c.bias for c in res.candidates}),
        "signals": sorted({c.primary.name for c in res.candidates if c.primary}),
        "flows": sorted({c.flow_label for c in res.candidates}),
    }
    errors = [d for d in res.diagnostics if d.severity == "error"]
    warnings = [d for d in res.diagnostics if d.severity == "warning"]
    infos = [d for d in res.diagnostics if d.severity == "info"]
    return {
        "res": res, "cfg": cfg, "s": s, "rows": rows, "cards": cards, "market": market, "sectors": sectors,
        "watch": res.watchlist, "started": started.strftime("%Y-%m-%d %H:%M %Z"), "bars_through": bars_through,
        "filters": filt, "errors": errors, "warnings": warnings, "infos": infos, "deep": deep,
        "settings_hash": res.hashes.get("settings_effective", "")[:12],
        "strategies_hash": res.hashes.get("strategies.yaml", "")[:12], "disclaimer": DISCLAIMER,
        "strategy_names": {st.id: st.name for st in cfg.strategies},
    }


# ----------------------------------------------------------------------------------------------
# Writers
# ----------------------------------------------------------------------------------------------


def _env() -> jinja2.Environment:
    env = jinja2.Environment(loader=jinja2.PackageLoader("scanner", "templates"),
                             autoescape=jinja2.select_autoescape(["html", "j2"]),
                             trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)

    def money(x: Any) -> str:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "n/a"
        return f"${x:,.2f}"

    def num(x: Any, nd: int = 2) -> str:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return "n/a"
        return f"{x:,.{nd}f}"

    def pct(x: Any) -> str:
        return "n/a" if x is None else f"{x * 100:+.1f}%"

    env.filters.update(money=money, num=num, pct=pct, tri=lambda t: t.value if isinstance(t, Tri) else str(t),
                       evjson=lambda x: json.dumps(_clean(x), sort_keys=True, ensure_ascii=False))
    return env


def render_html(res: RunResult, cfg: Config, deep: bool = False) -> str:
    return _env().get_template("scan.html.j2").render(**build_view(res, cfg, deep))


def output_dir(res: RunResult, reports_dir: Path) -> Path:
    return Path(reports_dir) / res.as_of.isoformat()


def suffix_for(symbols: list[str] | None, strategy_ids: list[str] | None) -> str:
    parts = []
    if symbols:
        parts.append("_".join(symbols))
    if strategy_ids:
        parts.append("strategy_" + "_".join(strategy_ids))
    return ("_" + "_".join(parts)) if parts else ""


def write_outputs(res: RunResult, cfg: Config, *, reports_dir: Path | None = None, symbols: list[str] | None = None,
                  strategy_ids: list[str] | None = None) -> dict[str, Path]:
    s = res.settings
    k = s.levels.zone_atr_mult
    out = output_dir(res, Path(reports_dir or s.report.reports_dir))
    out.mkdir(parents=True, exist_ok=True)
    sfx = suffix_for(symbols, strategy_ids)
    deep = bool(symbols)
    paths = {
        "html": out / f"scan{sfx}.html",
        "csv": out / f"candidates{sfx}.csv",
        "json": out / f"candidates{sfx}.json",
        "tradingview": out / f"tradingview_watchlist{sfx}.txt",
        "manifest": out / f"run{sfx}.json",
    }
    paths["html"].write_text(render_html(res, cfg, deep), encoding="utf-8")

    cand_json = [candidate_dict(c, res.analyses[c.symbol], k) for c in res.candidates]
    payload = {
        "as_of": res.as_of.isoformat(), "provisional": res.provisional, "candidates": cand_json,
        "watchlist": [watch_dict(w) for w in res.watchlist],
        "symbols": {sym: {"trend": sa.trend.state, "trend_reason": sa.trend.reason,
                          "pivots_used": [[p.kind, p.date.isoformat(), _r(p.price)] for p in sa.trend.pivots_used],
                          "flow": dict(sa.flow.components()) if sa.flow else None,
                          "signals": [signal_dict(x) for x in sa.signals],
                          "nearest_support": [level_dict(r["level"], sa, k) for r in sa.nearest_support],
                          "nearest_resistance": [level_dict(r["level"], sa, k) for r in sa.nearest_resistance],
                          "confluence": _clean(sa.confluence)}
                    for sym, sa in sorted(res.analyses.items())},
    }
    paths["json"].write_text(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")

    with paths["csv"].open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "strategy_rank", "symbol", "strategy_id", "bias", "status", "score", "primary_signal",
                    "age", "level_type", "level", "zone_low", "zone_high", "trend", "flow", "rsi", "close",
                    "stretched", "invalidation"])
        for c, d in zip(res.candidates, cand_json, strict=True):
            ps, pl = d["primary_signal"] or {}, d["primary_level"] or {}
            inval = next((a["price"] for a in d["alerts"] if a["label"] == "Invalidation"), "")
            w.writerow([c.rank, c.strategy_rank, c.symbol, c.strategy_id, c.bias, c.status.value, c.score,
                        ps.get("name", ""), ps.get("age", ""), pl.get("type", ""), pl.get("level", ""),
                        pl.get("zone_low", ""), pl.get("zone_high", ""), d["trend"], c.flow_label,
                        _r(d["rsi"], 2), _r(d["close"]), c.stretched, inval])

    short = list(dict.fromkeys(c.symbol for c in res.candidates))
    watch = [x for x in dict.fromkeys(w.symbol for w in res.watchlist) if x not in short]
    paths["tradingview"].write_text(
        "# shortlist\n" + ",".join(short) + "\n# watch list\n" + ",".join(watch) + "\n", encoding="utf-8")

    snap = out / "config_snapshot"
    snap.mkdir(exist_ok=True)
    for name in ("settings.yaml", "strategies.yaml"):
        src = cfg.root / name
        if src.exists():
            shutil.copyfile(src, snap / name)
    paths["manifest"].write_text(json.dumps(manifest(res, symbols, strategy_ids, paths), indent=2,
                                            sort_keys=True), encoding="utf-8")
    return paths


def manifest(res: RunResult, symbols, strategy_ids, paths: dict[str, Path] | None = None) -> dict:
    return {
        "command": "run", "as_of": res.as_of.isoformat(),
        "requested_as_of": res.requested_as_of.isoformat() if res.requested_as_of else None,
        "provisional": res.provisional, "symbols_filter": symbols, "strategy_filter": strategy_ids,
        "started": res.started.isoformat() if res.started else None, "runtime_s": round(res.runtime_s, 2),
        "exit_code": res.exit_code, "versions": _versions(), "hashes": res.hashes,
        "data_fingerprints": res.fingerprints,
        "counts": {"symbols_analyzed": len(res.analyses), "symbols_skipped": len(res.skipped),
                   "candidates": len(res.candidates), "watch_items": len(res.watchlist),
                   "strategies_enabled": len(res.strategies_enabled),
                   "strategies_skipped": len(res.strategies_skipped)},
        "skipped": [{"symbol": a, "reason": b} for a, b in res.skipped],
        "strategies_skipped": [{"id": a, "reason": b} for a, b in res.strategies_skipped],
        "diagnostics": [{"symbol": d.symbol, "stage": d.stage, "severity": d.severity, "message": d.message}
                        for d in res.diagnostics],
        "notes": res.notes,
        "outputs": {k: str(v) for k, v in (paths or {}).items()},
    }
