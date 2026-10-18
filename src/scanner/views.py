"""Framework-free dict views of analysis objects, shared by the Scanner and Markets pages (no Streamlit)."""

from __future__ import annotations

import pandas as pd

from scanner.config import Config
from scanner.flow import ratio_trend, state_of
from scanner.models import Level, SymbolAnalysis, jsonable
from scanner.pipeline import RunResult


def level_dict(lv: Level, sa: SymbolAnalysis, k: float) -> dict:
    t = sa.last_idx
    lo, hi = lv.zone(t, sa.bars.atr, k)
    c = float(sa.bars.close[t])
    role = lv.role(t, c)
    return jsonable({
        "id": lv.id, "type": lv.type, "manual": lv.manual, "origin": lv.origin, "role": role,
        "role_reversal": lv.origin is not None and role != lv.origin, "level": lv.value(t), "zone_low": lo,
        "zone_high": hi, "touches": lv.touches, "anchors": [[d, p] for d, p in lv.anchors],
        "last_touch": lv.touch_dates[-1] if lv.touch_dates else None, "slope": lv.slope, "quality": lv.quality,
        "dist_pct": (lv.value(t) - c) / c * 100, "dist_atr": (lv.value(t) - c) / float(sa.bars.atr[t]),
        "note": lv.note,
    })


def _completed_close(sa: SymbolAnalysis) -> pd.Series:
    n = sa.completed_idx + 1
    return pd.Series(sa.bars.close[:n], index=pd.DatetimeIndex(sa.bars.dates[:n]))


def market_context(res: RunResult, cfg: Config) -> tuple[dict | None, list[dict]]:
    """(benchmark summary or None, one row per sector ETF) for the app's market overview."""
    k = res.settings.levels.zone_atr_mult
    bench = res.series(res.benchmark)
    market = None
    if bench is not None:
        market = {"symbol": res.benchmark, "trend": bench.trend, "close": float(bench.bars.close[bench.last_idx]),
                  "supports": [level_dict(r["level"], bench, k) for r in bench.nearest_support],
                  "resistances": [level_dict(r["level"], bench, k) for r in bench.nearest_resistance],
                  "returns": bench.returns}
    sectors = []
    for etf in res.sector_etfs:
        sa = res.series(etf)
        if sa is None:
            sectors.append({"etf": etf, "trend": "n/a", "vs": "n/a", "returns": {}, "members": []})
            continue
        vs = "n/a"
        if bench is not None:
            ec, bc = _completed_close(sa), _completed_close(bench)
            vs = state_of(ratio_trend(ec, bc, res.settings))
        sectors.append({"etf": etf, "trend": sa.trend.state, "vs": vs, "returns": sa.returns,
                        "members": sorted(x for x in res.analyses if cfg.sector_for(x) == etf)})
    return market, sectors
