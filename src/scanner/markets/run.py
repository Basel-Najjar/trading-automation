"""Compute everything the Markets page shows for one resolution, from already-loaded prices (pure, no network)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from scanner.config import Settings
from scanner.markets.config import MarketsConfig, settings_for
from scanner.markets.pca import PcaResult, compute_pca
from scanner.markets.rrg import RrgResult, compute_rrg
from scanner.markets.series import Prices, build_series, closes
from scanner.markets.structure import analyze_all, summary_rows
from scanner.models import Diagnostic, SymbolAnalysis


@dataclass
class MarketsResult:
    resolution: str
    as_of: date | None
    settings: Settings  # the resolution's effective technical settings
    display_bars: int
    analyses: dict[str, SymbolAnalysis]
    summary: list[dict]
    rrg: RrgResult
    rrg_order: list[str]
    pca: PcaResult
    diagnostics: list[Diagnostic] = field(default_factory=list)


def run_markets(mc: MarketsConfig, base: Settings, prices: Prices, res: str) -> MarketsResult:
    p = mc.params(res)
    s = settings_for(base, p)
    history = mc.bars(res, p.history_years)
    diags = list(prices.diagnostics)
    weekly = res == "weekly"

    frames, d = build_series(mc.specs(mc.levels), prices, res, history)
    diags += d
    analyses, d = analyze_all(frames, s)
    diags += d

    rrg_specs = mc.specs([*mc.rrg.members, mc.rrg.benchmark])
    rrg_frames, d = build_series(rrg_specs, prices, res, history)
    diags += d
    rrg = compute_rrg(closes(rrg_frames, weekly), mc.rrg.benchmark, mc.rrg.members, p.rrg.window, p.rrg.tail)

    pca_frames, d = build_series(mc.specs(mc.pca.members), prices, res, history)
    diags += d
    pca = compute_pca(closes(pca_frames, weekly), p.pca.window, p.pca.components)
    diags += [Diagnostic(m, "pca", "warning", why) for m, why in pca.skipped]
    diags += [Diagnostic(m, "rrg", "warning", why) for m, why in rrg.skipped]

    # one message per (series, text): the three lists often share series
    seen, unique = set(), []
    for x in diags:
        if (x.symbol, x.message) not in seen:
            seen.add((x.symbol, x.message))
            unique.append(x)
    return MarketsResult(res, prices.as_of, s, mc.bars(res, p.display_years), analyses, summary_rows(analyses, res),
                         rrg, list(mc.rrg.members), pca, unique)


def config_digest(mc: MarketsConfig) -> str:
    return hashlib.sha256(json.dumps(mc.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:12]
