"""Owner-maintained inputs: loading, validation and linting (pydantic at the boundary only)."""

from __future__ import annotations

import csv
import difflib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from scanner.models import CHECKS, DIRECTIONS, MAX_LOOKBACK_BARS, SIGNAL_NAMES, TREND_STATES

# ----------------------------------------------------------------------------------------------
# settings.yaml
# ----------------------------------------------------------------------------------------------


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataSettings(Strict):
    history_years: float = 3
    price_adjustment: Literal["split_only", "total_return"] = "split_only"
    benchmark: str = "SPY"
    min_bars: int = 250
    allow_partial_history: bool = False
    gap_skip_atr: float = Field(3.0, gt=0)
    gap_lookback_bars: int = Field(15, ge=1)
    stale_bar_sessions: int = 1
    cache_dir: str = "cache"
    cache_revision_rel_tol: float = 1e-4
    close_buffer_minutes: int = 30
    batch_size: int = 40
    max_retries: int = 3


class IndicatorSettings(Strict):
    atr_period: int = 14
    rsi_period: int = 14
    rsi_overbought: float = 70
    rsi_oversold: float = 30
    relvol_period: int = 20


class PivotSettings(Strict):
    pivot_window: int = Field(5, ge=1)


class LevelSettings(Strict):
    zone_atr_mult: float = 0.25
    cluster_atr_mult: float = 0.5
    min_touches_horizontal: int = 2
    min_touches_diagonal: int = 2
    trendline_lookback_bars: int = 250
    trendline_scale: Literal["linear", "log"] = "linear"
    nearest_levels_per_side: int = 3
    horizontal_lookback_bars: int | None = None  # None = full analysis window
    recency_half_life_bars: float = 60
    max_trendlines_per_side: int = 2
    sma_base_quality: dict[str, float] = Field(default_factory=lambda: {"sma50": 2.0, "sma200": 3.0})
    manual_quality: float = 3.0


class SignalSettings(Strict):
    full_body_ratio: float = 0.70
    break_confirm: Literal["prior_extreme", "engulf"] = "prior_extreme"
    break_engulfing_require_opposite_color: bool = False
    pattern_engulfing_require_opposite_color: bool = True
    engulfing_tolerance_atr: float = 0.0
    hold_test_lookback: int = 3
    hold_require_prior_side: bool = True
    break_prior_side_bars: int = Field(3, ge=1, le=20)
    approach_atr: float = 1.0
    approach_lookback_bars: int = 3
    chase_atr: float = 1.5
    held_swing_bars: int = Field(5, ge=1, le=20)


class RangeSettings(Strict):
    mid_band: tuple[float, float] = (0.35, 0.65)
    channel_min_touches: int = Field(1, ge=1)


def _default_agree() -> dict[str, list[str]]:
    return {"bullish": ["uptrend"], "non_bearish": ["uptrend", "range"], "neutral": ["range"],
            "non_bullish": ["downtrend", "range"], "bearish": ["downtrend"]}


def _default_oppose() -> dict[str, list[str]]:
    return {"bullish": ["downtrend"], "non_bearish": ["downtrend"], "neutral": ["uptrend", "downtrend"],
            "non_bullish": ["uptrend"], "bearish": ["uptrend"]}


def _flow_table(v: dict[str, list[str]]) -> dict[str, list[str]]:
    for d, states in v.items():
        if d not in DIRECTIONS:
            raise ValueError(f"unknown direction '{d}'{_suggest(d, DIRECTIONS)}; allowed: {list(DIRECTIONS)}")
        for t in states:
            if t not in TREND_STATES:
                raise ValueError(f"invalid trend state '{t}'{_suggest(t, TREND_STATES)}")
    return v


class FlowSettings(Strict):
    agree: dict[str, list[str]] = Field(default_factory=_default_agree)
    oppose: dict[str, list[str]] = Field(default_factory=_default_oppose)

    _tables = field_validator("agree", "oppose")(_flow_table)


class DisplaySettings(Strict):
    max_watch_items: int = 30
    max_watch_items_per_symbol: int = 3
    chart_bars: int = 150


class Settings(Strict):
    data: DataSettings = Field(default_factory=DataSettings)
    indicators: IndicatorSettings = Field(default_factory=IndicatorSettings)
    pivots: PivotSettings = Field(default_factory=PivotSettings)
    levels: LevelSettings = Field(default_factory=LevelSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    range: RangeSettings = Field(default_factory=RangeSettings)
    flow: FlowSettings = Field(default_factory=FlowSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)

    @property
    def window_bars(self) -> int:
        return max(int(round(self.data.history_years * 252)), self.data.min_bars)


# ----------------------------------------------------------------------------------------------
# scans.yaml
# ----------------------------------------------------------------------------------------------


def _suggest(name: str, choices: tuple[str, ...] | list[str] | frozenset[str]) -> str:
    match = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.6)
    return f" — did you mean '{match[0]}'?" if match else ""


class Criterion(Strict):
    """One check of a scan: exactly one of the CHECKS keys, plus an optional `weight` and display `label`."""

    trend: list[str] | None = None
    signal_all: list[str] | None = None
    signal_any: list[str] | None = None
    rsi_below: float | None = Field(None, ge=0, le=100)
    rsi_above: float | None = Field(None, ge=0, le=100)
    rsi_between: tuple[float, float] | None = None
    flow: Literal["with_flow", "not_countertrend"] | None = None
    channel: int | None = Field(None, ge=3, le=MAX_LOOKBACK_BARS)
    within: int | None = Field(None, ge=1, le=MAX_LOOKBACK_BARS)
    weight: float = Field(1.0, gt=0)
    label: str = ""

    @field_validator("trend")
    @classmethod
    def _trend(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            if not v:
                raise ValueError("list at least one trend state")
            for t in v:
                if t not in TREND_STATES:
                    raise ValueError(f"invalid trend state '{t}'{_suggest(t, TREND_STATES)}")
        return v

    @field_validator("signal_all", "signal_any")
    @classmethod
    def _signals(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            if not v:
                raise ValueError("list at least one signal")
            for s in v:
                if s not in SIGNAL_NAMES:
                    raise ValueError(f"unknown signal '{s}'{_suggest(s, SIGNAL_NAMES)}")
        return v

    @field_validator("rsi_between")
    @classmethod
    def _between(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is not None and not 0 <= v[0] < v[1] <= 100:
            raise ValueError("rsi_between must be [low, high] with 0 ≤ low < high ≤ 100")
        return v

    @model_validator(mode="after")
    def _one_check(self) -> Criterion:
        given = [c for c in CHECKS if getattr(self, c) is not None]
        if len(given) != 1:
            raise ValueError(f"each criterion needs exactly one of {list(CHECKS)}; got {given or 'none'}")
        if self.within is not None and given[0] not in ("signal_all", "signal_any"):
            raise ValueError("`within` applies only to signal_all / signal_any criteria")
        return self

    @property
    def check(self) -> str:
        return next(c for c in CHECKS if getattr(self, c) is not None)

    @property
    def value(self) -> Any:
        return getattr(self, self.check)

    @property
    def signal_names(self) -> list[str]:
        return list(self.value) if self.check in ("signal_all", "signal_any") else []


class Scan(Strict):
    enabled: bool = True
    min_pct: float = Field(70.0, ge=0, le=100)
    lookback_bars: int = Field(5, ge=1, le=MAX_LOOKBACK_BARS)  # a signal on any of the latest N bars counts
    criteria: list[Criterion] = Field(default_factory=list)

    @field_validator("criteria", mode="before")
    @classmethod
    def _none(cls, v: Any) -> Any:
        return [] if v is None else v

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.criteria)

    def window(self, c: Criterion) -> int:
        """Bars a signal criterion looks back over: its own `within`, else the scan's `lookback_bars`."""
        return c.within or self.lookback_bars

    @property
    def bars_needed(self) -> int:
        """Bars (latest first) that need their own structure: every signal window, and a channel's window plus the
        bar before it, whose structure the channel is drawn from (one more when the latest bar is still forming)."""
        need = [self.lookback_bars] + [c.within or 0 for c in self.criteria]
        need += [c.channel + 2 for c in self.criteria if c.channel]
        return max(need)


class ScansFile(Strict):
    bullish: Scan | None = None
    non_bearish: Scan | None = None
    neutral: Scan | None = None
    non_bullish: Scan | None = None
    bearish: Scan | None = None

    def as_dict(self) -> dict[str, Scan]:
        """Every direction; one missing from the file is a disabled scan with no criteria."""
        return {d: getattr(self, d) or Scan(enabled=False) for d in DIRECTIONS}

    def missing(self) -> list[str]:
        return [d for d in DIRECTIONS if getattr(self, d) is None]


# ----------------------------------------------------------------------------------------------
# Other inputs
# ----------------------------------------------------------------------------------------------


@dataclass
class UniverseEntry:
    symbol: str
    type: str = "stock"
    enabled: bool = True
    notes: str = ""
    sector_etf: str | None = None


class ManualLevel(Strict):
    type: Literal["horizontal", "diagonal"]
    role: Literal["support", "resistance"]
    price: float | None = None
    anchors: list[tuple[date, float]] | None = None
    note: str = ""

    @model_validator(mode="after")
    def _shape(self) -> ManualLevel:
        if self.type == "horizontal" and self.price is None:
            raise ValueError("horizontal manual level needs 'price'")
        if self.type == "diagonal" and (not self.anchors or len(self.anchors) != 2):
            raise ValueError("diagonal manual level needs exactly two 'anchors': [[date, price], [date, price]]")
        if self.anchors and self.anchors[0][0] >= self.anchors[1][0]:
            raise ValueError("diagonal anchors must be in date order")
        return self


@dataclass
class Issue:
    file: str
    line: int | None
    where: str
    message: str
    severity: str = "error"  # error | warning

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        where = f" {self.where}:" if self.where else ""
        return f"{self.severity.upper()} {loc}:{where} {self.message}"


def error_issues(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)


@dataclass
class Config:
    root: Path
    settings: Settings
    scans: dict[str, Scan]  # every direction, in DIRECTIONS order
    universe: list[UniverseEntry]
    sector_map: dict[str, str]
    manual_levels: dict[str, list[ManualLevel]] = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return [u.symbol for u in self.universe if u.enabled]

    @property
    def active_scans(self) -> dict[str, Scan]:
        return {d: s for d, s in self.scans.items() if s.active}

    @property
    def lookback_bars(self) -> int:
        """Latest bars the active scans evaluate, each with structure built from the data it had."""
        return max((s.bars_needed for s in self.active_scans.values()), default=1)

    def sector_for(self, symbol: str) -> str | None:
        return self.sector_map.get(symbol)


# ----------------------------------------------------------------------------------------------
# YAML helpers: line numbers for error messages
# ----------------------------------------------------------------------------------------------


def _line_map(node: yaml.Node, path: tuple = (), out: dict | None = None) -> dict[tuple, int]:
    out = {} if out is None else out
    out[path] = node.start_mark.line + 1
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            key = k.value
            out[(*path, key)] = k.start_mark.line + 1
            _line_map(v, (*path, key), out)
            out[(*path, key)] = k.start_mark.line + 1
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            _line_map(item, (*path, i), out)
    return out


def line_for(lines: dict[tuple, int], loc: tuple) -> int | None:
    loc = tuple(loc)
    while loc:
        if loc in lines:
            return lines[loc]
        loc = loc[:-1]
    return lines.get(())


def read_yaml(path: Path, issues: list[Issue]) -> tuple[Any, dict[tuple, int]]:
    text = path.read_text(encoding="utf-8")
    try:
        node = yaml.compose(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        issues.append(Issue(path.name, mark.line + 1 if mark else None, "", f"YAML syntax error: {e}"))
        return None, {}
    return data, (_line_map(node) if node is not None else {})


def _fmt_loc(loc: tuple) -> str:
    out = ""
    for p in loc:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def pydantic_issues(
    err: ValidationError, fname: str, lines: dict[tuple, int], prefix: tuple = (), label: str = "",
    root: type[BaseModel] | None = None,
) -> list[Issue]:
    issues = []
    for e in err.errors():
        loc = tuple(x for x in e["loc"] if not (isinstance(x, str) and x.startswith("function-")))
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            key = loc[-1] if loc else "?"
            sibling_fields = _known_fields_at(loc[:-1], root or Settings)
            msg = f"unknown key '{key}'{_suggest(str(key), sibling_fields)}"
        where = (label + _fmt_loc(loc)) if label else _fmt_loc(loc)
        issues.append(Issue(fname, line_for(lines, prefix + loc), where, msg))
    return issues


def _model_in(annotation: Any) -> Any:
    """The pydantic model an annotation holds (itself, or the item type of a list/optional)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return next((a for a in getattr(annotation, "__args__", ()) if isinstance(a, type) and issubclass(a, BaseModel)),
                None)


def _known_fields_at(loc: tuple, root: Any) -> list[str]:
    model: Any = root
    for p in loc:
        if isinstance(p, int):
            continue
        f = getattr(model, "model_fields", {}).get(p)
        if f is None:
            return []
        model = _model_in(f.annotation)
    return list(getattr(model, "model_fields", {}).keys())


# ----------------------------------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------------------------------


REMOVED_SETTINGS = {
    "report": "static reports were removed; rename this section to `display:` and keep only max_watch_items, "
              "max_watch_items_per_symbol and chart_bars",
    "replay": "replay was removed; delete this section",
    "scoring": "weighted scores were removed; scans rank by the weighted % of criteria met (set `weight:` on a "
               "criterion in scans.yaml); delete this section",
}
REMOVED_SETTING_KEYS = {
    ("signals", "max_signal_age_bars"): "each scan now sets its own `lookback_bars` in scans.yaml (and a signal "
                                        "criterion can narrow it with `within:`); delete this line",
}


def load_settings(path: Path, issues: list[Issue]) -> Settings | None:
    if not path.exists():
        issues.append(Issue(path.name, None, "", "file missing — using all defaults", "warning"))
        return Settings()
    data, lines = read_yaml(path, issues)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        issues.append(Issue(path.name, 1, "", "top level must be a mapping"))
        return None
    for key, why in REMOVED_SETTINGS.items():
        if key in data:
            issues.append(Issue(path.name, line_for(lines, (key,)), key, f"no longer supported: {why}"))
    for (section, key), why in REMOVED_SETTING_KEYS.items():
        if isinstance(data.get(section), dict) and key in data[section]:
            issues.append(Issue(path.name, line_for(lines, (section, key)), f"{section}.{key}",
                                f"no longer supported: {why}"))
    if has_errors(issues):
        return None
    try:
        s = Settings.model_validate(data)
    except ValidationError as e:
        issues.extend(pydantic_issues(e, path.name, lines))
        return None
    lo, hi = s.range.mid_band
    if not 0 <= lo < hi <= 1:
        issues.append(Issue(path.name, line_for(lines, ("range", "mid_band")), "range.mid_band",
                            "mid_band must satisfy 0 ≤ low < high ≤ 1"))
        return None
    return s


def load_scans(path: Path, issues: list[Issue]) -> dict[str, Scan] | None:
    """scans.yaml: a mapping keyed by the DIRECTIONS. A missing key is a disabled scan with no criteria (warned)."""
    if not path.exists():
        issues.append(Issue(path.name, None, "", "file missing (required)"))
        return None
    data, lines = read_yaml(path, issues)
    if data is None:
        if any(i.file == path.name for i in issues):
            return None
        data = {}
    if not isinstance(data, dict):
        issues.append(Issue(path.name, 1, "", f"top level must be a mapping with the keys {list(DIRECTIONS)}"))
        return None
    try:
        parsed = ScansFile.model_validate(data)
    except ValidationError as e:
        issues.extend(pydantic_issues(e, path.name, lines, root=ScansFile))
        return None
    scans = parsed.as_dict()
    if missing := parsed.missing():
        issues.append(Issue(path.name, None, "", f"no {', '.join(missing)} scan in the file — "
                                                 f"{'it is' if len(missing) == 1 else 'they are'} off; use Restore "
                                                 "defaults in the Criteria tab (or add the key) to turn "
                                                 f"{'it' if len(missing) == 1 else 'them'} on", "warning"))
    for d, s in scans.items():
        if s.enabled and not s.criteria:
            issues.append(Issue(path.name, line_for(lines, (d,)), d, "enabled scan has no criteria — it is not "
                                                                       "evaluated", "warning"))
    return scans


def clean_symbol(s: str) -> str:
    return s.strip().upper()


_STRATEGIES_GONE = ("strategies were replaced by the bullish, non-bearish, neutral, non-bullish and bearish scans in "
                    "scans.yaml; move any rules you want to keep there and delete {}")
LEGACY_FILES = {
    "expected_reads.csv": "check-reads (and replay) were removed; delete expected_reads.csv",
    "strategies.yaml": _STRATEGIES_GONE.format("strategies.yaml"),
    "strategies.default.yaml": _STRATEGIES_GONE.format("strategies.default.yaml"),
    "universe.txt": "symbols now live only in universe.csv (columns symbol,type,sector_etf,enabled,notes); move the "
                    "symbols there and delete universe.txt",
    "sector_map.csv": "sector ETFs now live in universe.csv's sector_etf column; move the mappings there and delete "
                      "sector_map.csv",
}


def load_universe(root: Path, issues: list[Issue]) -> list[UniverseEntry] | None:
    """universe.csv: symbol (required), type (stock/etf), sector_etf, enabled, notes. The only symbol config file."""
    for name, why in LEGACY_FILES.items():
        if (root / name).exists():
            issues.append(Issue(name, None, "", f"no longer supported: {why}"))
    csv_p = root / "universe.csv"
    if not csv_p.exists():
        issues.append(Issue("universe.csv", None, "", "missing config/universe.csv"))
        return None
    if any(i.file in LEGACY_FILES for i in issues):
        return None
    fname = csv_p.name
    entries: list[UniverseEntry] = []
    with csv_p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        if "symbol" not in cols:
            issues.append(Issue(fname, 1, "", "missing required column 'symbol'"))
            return None
        for n, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            sym = clean_symbol(row.get("symbol", ""))
            if not sym:
                issues.append(Issue(fname, n, "symbol", "empty symbol — row ignored", "warning"))
                continue
            typ = (row.get("type") or "stock").lower()
            if typ not in ("etf", "stock"):
                issues.append(Issue(fname, n, "type", f"type must be 'etf' or 'stock', got '{typ}'"))
                return None
            en = (row.get("enabled") or "true").lower()
            if en not in ("true", "false", "1", "0", "yes", "no", "y", "n"):
                issues.append(Issue(fname, n, "enabled", f"enabled must be true/false, got '{en}'"))
                return None
            entries.append(
                UniverseEntry(sym, typ, en in ("true", "1", "yes", "y"), row.get("notes", ""),
                              clean_symbol(row["sector_etf"]) if row.get("sector_etf") else None)
            )
    seen: set[str] = set()
    deduped = []
    for e in entries:
        if e.symbol in seen:
            issues.append(Issue(fname, None, e.symbol, "duplicate symbol — de-duplicated", "warning"))
            continue
        seen.add(e.symbol)
        deduped.append(e)
    if not any(e.enabled for e in deduped):
        issues.append(Issue(fname, None, "", "no enabled symbols"))
        return None
    for e in deduped:
        if e.enabled and not e.sector_etf and e.type != "etf":
            issues.append(Issue(fname, None, e.symbol, "no sector_etf — name-vs-sector check will be n/a", "warning"))
    return deduped


def sector_map_of(universe: list[UniverseEntry]) -> dict[str, str]:
    return {e.symbol: e.sector_etf for e in universe if e.sector_etf}


def load_manual_levels(root: Path, issues: list[Issue]) -> dict[str, list[ManualLevel]]:
    p = root / "manual_levels.yaml"
    if not p.exists():
        return {}
    data, lines = read_yaml(p, issues)
    if not data:
        return {}
    if not isinstance(data, dict):
        issues.append(Issue(p.name, 1, "", "top level must map SYMBOL → list of levels"))
        return {}
    out: dict[str, list[ManualLevel]] = {}
    for sym, items in data.items():
        key = clean_symbol(str(sym))
        for i, item in enumerate(items or []):
            try:
                out.setdefault(key, []).append(ManualLevel.model_validate(item))
            except ValidationError as e:
                issues.extend(pydantic_issues(e, p.name, lines, prefix=(sym, i), label=f"{key}[{i}]."))
    return out


def load_config(root: Path) -> tuple[Config | None, list[Issue]]:
    """Load and validate every owner-maintained file. Returns (None, issues) when any error is present."""
    root = Path(root)
    issues: list[Issue] = []
    if not root.is_dir():
        return None, [Issue(str(root), None, "", "config directory not found")]
    settings = load_settings(root / "settings.yaml", issues)
    scans = load_scans(root / "scans.yaml", issues)
    universe = load_universe(root, issues)
    sector_map = sector_map_of(universe) if universe is not None else {}
    manual = load_manual_levels(root, issues)
    if has_errors(issues) or None in (settings, scans, universe):
        return None, issues
    if not any(s.active for s in scans.values()):
        issues.append(Issue("scans.yaml", None, "", "no enabled scan has criteria — nothing will be listed",
                            "warning"))
    return Config(root, settings, scans, universe, sector_map, manual), issues
