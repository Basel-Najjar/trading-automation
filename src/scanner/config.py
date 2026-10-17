"""Owner-maintained inputs: loading, validation and linting (pydantic at the boundary only)."""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from scanner.models import BIASES, SIGNAL_NAMES, TREND_STATES

# ----------------------------------------------------------------------------------------------
# settings.yaml
# ----------------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataSettings(_Strict):
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


class IndicatorSettings(_Strict):
    atr_period: int = 14
    rsi_period: int = 14
    rsi_overbought: float = 70
    rsi_oversold: float = 30
    relvol_period: int = 20


class PivotSettings(_Strict):
    pivot_window: int = Field(5, ge=1)


class LevelSettings(_Strict):
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


class SignalSettings(_Strict):
    full_body_ratio: float = 0.70
    break_engulfing_require_opposite_color: bool = False
    pattern_engulfing_require_opposite_color: bool = True
    engulfing_tolerance_atr: float = 0.0
    hold_test_lookback: int = 3
    hold_require_prior_side: bool = True
    break_prior_side_bars: int = Field(3, ge=1, le=20)
    approach_atr: float = 1.0
    approach_lookback_bars: int = 3
    max_signal_age_bars: int = Field(1, ge=0, le=5)
    chase_atr: float = 1.5


class RangeSettings(_Strict):
    mid_band: tuple[float, float] = (0.35, 0.65)


def _default_agree() -> dict[str, list[str]]:
    return {
        "bullish": ["uptrend"],
        "non_bearish": ["uptrend", "range"],
        "bearish": ["downtrend"],
        "non_bullish": ["downtrend", "range"],
        "neutral": ["range"],
    }


def _default_oppose() -> dict[str, list[str]]:
    return {
        "bullish": ["downtrend"],
        "non_bearish": ["downtrend"],
        "bearish": ["uptrend"],
        "non_bullish": ["uptrend"],
        "neutral": ["uptrend", "downtrend"],
    }


class FlowSettings(_Strict):
    agree: dict[str, list[str]] = Field(default_factory=_default_agree)
    oppose: dict[str, list[str]] = Field(default_factory=_default_oppose)


class ScoringSettings(_Strict):
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "signal": 30,
            "level_quality": 20,
            "confluence": 10,
            "trend_fit": 10,
            "flow": 15,
            "rsi_headroom": 5,
            "entry_quality": 5,
            "age": 5,
        }
    )
    signal_kind_scores: dict[str, float] = Field(
        default_factory=lambda: {"break": 1.0, "hold": 0.75, "pattern": 0.5, "context": 0.2}
    )
    level_quality_cap: float = 6.0
    role_reversal_bonus: float = 0.1
    rsi_headroom_scale: float = 30.0
    trend_fit_any: float = 0.5
    flow_scores: dict[str, float] = Field(
        default_factory=lambda: {"with_flow": 1.0, "mixed": 0.5, "countertrend": 0.0, "ignored": 0.5}
    )
    age_scores: list[float] = Field(default_factory=lambda: [1.0, 0.5])

    @field_validator("weights")
    @classmethod
    def _known_weights(cls, v: dict[str, float]) -> dict[str, float]:
        known = {"signal", "level_quality", "confluence", "trend_fit", "flow", "rsi_headroom",
                 "entry_quality", "age"}
        unknown = set(v) - known
        if unknown:
            raise ValueError(f"unknown weight(s) {sorted(unknown)}; allowed: {sorted(known)}")
        return v


class ReportSettings(_Strict):
    max_watch_items: int = 30
    max_watch_items_per_symbol: int = 3
    chart_bars: int = 150
    timezone: str = "America/Chicago"
    reports_dir: str = "reports"


class ReplaySettings(_Strict):
    forward_horizons: list[int] = Field(default_factory=lambda: [3, 5, 10, 20])


class Settings(_Strict):
    data: DataSettings = Field(default_factory=DataSettings)
    indicators: IndicatorSettings = Field(default_factory=IndicatorSettings)
    pivots: PivotSettings = Field(default_factory=PivotSettings)
    levels: LevelSettings = Field(default_factory=LevelSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    range: RangeSettings = Field(default_factory=RangeSettings)
    flow: FlowSettings = Field(default_factory=FlowSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    replay: ReplaySettings = Field(default_factory=ReplaySettings)

    @property
    def window_bars(self) -> int:
        return max(int(round(self.data.history_years * 252)), self.data.min_bars)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    def with_overrides(self, overrides: dict[str, Any]) -> Settings:
        """Apply per-strategy overrides (signal-stage keys only, see OVERRIDABLE_KEYS)."""
        if not overrides:
            return self
        sig = self.signals.model_copy(update={k: v for k, v in overrides.items() if k in SignalSettings.model_fields})
        ind = self.indicators.model_copy(
            update={k: v for k, v in overrides.items() if k in ("rsi_overbought", "rsi_oversold")}
        )
        return self.model_copy(update={"signals": sig, "indicators": ind})


OVERRIDABLE_KEYS: frozenset[str] = frozenset(SignalSettings.model_fields) | {"rsi_overbought", "rsi_oversold"}

# ----------------------------------------------------------------------------------------------
# strategies.yaml
# ----------------------------------------------------------------------------------------------


def _suggest(name: str, choices: tuple[str, ...] | list[str] | frozenset[str]) -> str:
    match = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.6)
    return f" — did you mean '{match[0]}'?" if match else ""


def _none_to_list(v: Any) -> Any:
    return [] if v is None else v


class Technical(_Strict):
    trend: list[str] = Field(default_factory=lambda: ["any"])
    signals_all: list[str] = Field(default_factory=list)
    signals_any: list[str] = Field(default_factory=list)
    not_extended: Literal["auto", "none", "up", "down"] = "auto"
    alignment: Literal["soft", "required", "ignore"] = "soft"
    overrides: dict[str, float | int | bool] = Field(default_factory=dict)

    _lists = field_validator("trend", "signals_all", "signals_any", mode="before")(_none_to_list)

    @field_validator("trend")
    @classmethod
    def _trend(cls, v: list[str]) -> list[str]:
        if not v:
            return ["any"]
        for t in v:
            if t != "any" and t not in TREND_STATES:
                raise ValueError(f"invalid trend state '{t}'{_suggest(t, (*TREND_STATES, 'any'))}")
        if "any" in v and len(v) > 1:
            raise ValueError("'any' cannot be combined with other trend states")
        return v

    @field_validator("signals_all", "signals_any")
    @classmethod
    def _signals(cls, v: list[str]) -> list[str]:
        for s in v:
            if s not in SIGNAL_NAMES:
                raise ValueError(f"unknown signal '{s}'{_suggest(s, SIGNAL_NAMES)}")
        return v

    @field_validator("overrides")
    @classmethod
    def _overrides(cls, v: dict[str, Any]) -> dict[str, Any]:
        for k in v:
            if k not in OVERRIDABLE_KEYS:
                raise ValueError(
                    f"override '{k}' not allowed{_suggest(k, OVERRIDABLE_KEYS)} "
                    "(v1 allows signal-stage keys and rsi thresholds only)"
                )
        return v

    @property
    def is_empty(self) -> bool:
        return self.trend == ["any"] and not self.signals_all and not self.signals_any


class Strategy(BaseModel):
    model_config = ConfigDict(extra="allow")  # unknown display-only keys are preserved and shown

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    enabled: bool = True
    status: Literal["complete", "partial", "stub"] = "complete"
    bias: Literal["bullish", "bearish", "non_bearish", "non_bullish", "neutral"]
    description: str = ""
    technical: Technical = Field(default_factory=Technical)
    construction: list[str] = Field(default_factory=list)
    orats_checklist: list[str] = Field(default_factory=list)
    other_manual_checks: list[str] = Field(default_factory=list)
    plan: dict[str, Any] = Field(default_factory=dict)

    _lists = field_validator("construction", "orats_checklist", "other_manual_checks", mode="before")(
        _none_to_list
    )

    @field_validator("technical", "plan", mode="before")
    @classmethod
    def _none_to_dict(cls, v: Any) -> Any:
        return {} if v is None else v

    @field_validator("bias", mode="before")
    @classmethod
    def _bias(cls, v: Any) -> Any:
        if isinstance(v, str) and v not in BIASES:
            raise ValueError(f"invalid bias '{v}'{_suggest(v, BIASES)}")
        return v

    @property
    def active(self) -> bool:
        return self.enabled and self.status != "stub"

    @property
    def not_extended_resolved(self) -> str:
        ne = self.technical.not_extended
        if ne != "auto":
            return ne
        return {"bullish": "up", "bearish": "down"}.get(self.bias, "none")

    @property
    def extra_fields(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


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


class ManualLevel(_Strict):
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
class ExpectedRead:
    date: date
    symbol: str
    expected: list[str]
    note: str = ""
    line: int = 0


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


@dataclass
class Config:
    root: Path
    settings: Settings
    strategies: list[Strategy]
    universe: list[UniverseEntry]
    sector_map: dict[str, str]
    manual_levels: dict[str, list[ManualLevel]] = field(default_factory=dict)
    expected_reads: list[ExpectedRead] = field(default_factory=list)
    hashes: dict[str, str] = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return [u.symbol for u in self.universe if u.enabled]

    def sector_for(self, symbol: str) -> str | None:
        return self.sector_map.get(symbol)

    def strategy(self, sid: str) -> Strategy | None:
        return next((s for s in self.strategies if s.id == sid), None)


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


def _line_for(lines: dict[tuple, int], loc: tuple) -> int | None:
    loc = tuple(loc)
    while loc:
        if loc in lines:
            return lines[loc]
        loc = loc[:-1]
    return lines.get(())


def _read_yaml(path: Path, issues: list[Issue]) -> tuple[Any, dict[tuple, int]]:
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


def _pydantic_issues(
    err: ValidationError, fname: str, lines: dict[tuple, int], prefix: tuple = (), label: str = ""
) -> list[Issue]:
    issues = []
    for e in err.errors():
        loc = tuple(x for x in e["loc"] if not (isinstance(x, str) and x.startswith("function-")))
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            key = loc[-1] if loc else "?"
            sibling_fields = _known_fields_at(loc[:-1])
            msg = f"unknown key '{key}'{_suggest(str(key), sibling_fields)}"
        where = (label + _fmt_loc(loc)) if label else _fmt_loc(loc)
        issues.append(Issue(fname, _line_for(lines, prefix + loc), where, msg))
    return issues


def _known_fields_at(loc: tuple) -> list[str]:
    model: Any = Settings
    for p in loc:
        if isinstance(p, int):
            continue
        f = getattr(model, "model_fields", {}).get(p)
        if f is None:
            model = Technical if p == "technical" else None
            if model is None:
                return []
            continue
        model = f.annotation
    return list(getattr(model, "model_fields", {}).keys())


# ----------------------------------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------------------------------


def load_settings(path: Path, issues: list[Issue]) -> Settings | None:
    if not path.exists():
        issues.append(Issue(path.name, None, "", "file missing — using all defaults", "warning"))
        return Settings()
    data, lines = _read_yaml(path, issues)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        issues.append(Issue(path.name, 1, "", "top level must be a mapping"))
        return None
    try:
        s = Settings.model_validate(data)
    except ValidationError as e:
        issues.extend(_pydantic_issues(e, path.name, lines))
        return None
    w = s.scoring.weights
    if abs(sum(w.values()) - 100) > 1e-6:
        issues.append(
            Issue(path.name, _line_for(lines, ("scoring", "weights")), "scoring.weights",
                  f"weights sum to {sum(w.values()):g}; scores are scaled to 0–100 only when they sum to 100",
                  "warning")
        )
    lo, hi = s.range.mid_band
    if not 0 <= lo < hi <= 1:
        issues.append(Issue(path.name, _line_for(lines, ("range", "mid_band")), "range.mid_band",
                            "mid_band must satisfy 0 ≤ low < high ≤ 1"))
        return None
    return s


def load_strategies(path: Path, issues: list[Issue]) -> list[Strategy] | None:
    if not path.exists():
        issues.append(Issue(path.name, None, "", "file missing (required)"))
        return None
    data, lines = _read_yaml(path, issues)
    if data is None:
        return None if any(i.file == path.name for i in issues) else []
    if not isinstance(data, list):
        issues.append(Issue(path.name, 1, "", "top level must be a list of strategies (each starting with '- id:')"))
        return None
    out: list[Strategy] = []
    ok = True
    seen: dict[str, int] = {}
    for i, raw in enumerate(data):
        sid = raw.get("id", f"#{i}") if isinstance(raw, dict) else f"#{i}"
        label = f"strategies[{i}] ({sid})"
        if not isinstance(raw, dict):
            issues.append(Issue(path.name, _line_for(lines, (i,)), label, "each strategy must be a mapping"))
            ok = False
            continue
        raw_id = raw.get("id")
        if isinstance(raw_id, str):
            if raw_id in seen:
                issues.append(Issue(path.name, _line_for(lines, (i, "id")), label,
                                    f"duplicate id '{raw_id}' (first defined on line {seen[raw_id]})"))
                ok = False
            seen.setdefault(raw_id, _line_for(lines, (i, "id")) or 0)
        try:
            s = Strategy.model_validate(raw)
        except ValidationError as e:
            issues.extend(_pydantic_issues(e, path.name, lines, prefix=(i,), label=label + "."))
            ok = False
            continue
        if s.active and s.technical.is_empty:
            issues.append(Issue(path.name, _line_for(lines, (i, "technical")), label,
                                "enabled strategy has no technical rules — it would match every symbol",
                                "warning"))
        out.append(s)
    return out if ok else None


def _clean_symbol(s: str) -> str:
    return s.strip().upper()


def load_universe(root: Path, issues: list[Issue]) -> list[UniverseEntry] | None:
    csv_p, txt_p = root / "universe.csv", root / "universe.txt"
    if csv_p.exists() and txt_p.exists():
        issues.append(Issue("universe", None, "", "both universe.csv and universe.txt exist — keep only one"))
        return None
    entries: list[UniverseEntry] = []
    if csv_p.exists():
        fname = csv_p.name
        with csv_p.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = [c.strip().lower() for c in (reader.fieldnames or [])]
            if "symbol" not in cols:
                issues.append(Issue(fname, 1, "", "missing required column 'symbol'"))
                return None
            for n, row in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                sym = _clean_symbol(row.get("symbol", ""))
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
                                  _clean_symbol(row["sector_etf"]) if row.get("sector_etf") else None)
                )
    elif txt_p.exists():
        for line in txt_p.read_text(encoding="utf-8").splitlines():
            sym = _clean_symbol(line.split("#", 1)[0])
            if sym:
                entries.append(UniverseEntry(sym))
    else:
        issues.append(Issue("universe", None, "", "missing config/universe.csv or config/universe.txt"))
        return None
    seen: set[str] = set()
    deduped = []
    for e in entries:
        if e.symbol in seen:
            issues.append(Issue("universe", None, e.symbol, "duplicate symbol — de-duplicated", "warning"))
            continue
        seen.add(e.symbol)
        deduped.append(e)
    if not any(e.enabled for e in deduped):
        issues.append(Issue("universe", None, "", "no enabled symbols"))
        return None
    return deduped


def load_sector_map(root: Path, universe: list[UniverseEntry], issues: list[Issue]) -> dict[str, str]:
    mapping = {e.symbol: e.sector_etf for e in universe if e.sector_etf}
    p = root / "sector_map.csv"
    if p.exists():
        with p.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = [c.strip().lower() for c in (reader.fieldnames or [])]
            if not {"symbol", "sector_etf"} <= set(cols):
                issues.append(Issue(p.name, 1, "", "columns must be 'symbol,sector_etf'"))
                return mapping
            seen: set[str] = set()
            for n, row in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                sym, etf = _clean_symbol(row.get("symbol", "")), _clean_symbol(row.get("sector_etf", ""))
                if not sym or not etf:
                    issues.append(Issue(p.name, n, "", "empty symbol or sector_etf — row ignored", "warning"))
                    continue
                if sym in seen:
                    issues.append(Issue(p.name, n, sym, "duplicate mapping — last one wins", "warning"))
                seen.add(sym)
                mapping[sym] = etf  # sector_map.csv wins over universe.csv column
    universe_syms = {e.symbol for e in universe}
    for e in universe:
        if e.enabled and e.symbol not in mapping and e.type != "etf":
            issues.append(Issue("sector_map", None, e.symbol,
                                "no sector ETF mapping — name-vs-sector check will be n/a", "warning"))
    for sym in sorted(set(mapping) - universe_syms):
        issues.append(Issue("sector_map", None, sym, "mapped symbol not in universe (ignored)", "warning"))
    return mapping


def load_manual_levels(root: Path, issues: list[Issue]) -> dict[str, list[ManualLevel]]:
    p = root / "manual_levels.yaml"
    if not p.exists():
        return {}
    data, lines = _read_yaml(p, issues)
    if not data:
        return {}
    if not isinstance(data, dict):
        issues.append(Issue(p.name, 1, "", "top level must map SYMBOL → list of levels"))
        return {}
    out: dict[str, list[ManualLevel]] = {}
    for sym, items in data.items():
        key = _clean_symbol(str(sym))
        for i, item in enumerate(items or []):
            try:
                out.setdefault(key, []).append(ManualLevel.model_validate(item))
            except ValidationError as e:
                issues.extend(_pydantic_issues(e, p.name, lines, prefix=(sym, i), label=f"{key}[{i}]."))
    return out


def load_expected_reads(root: Path, issues: list[Issue]) -> list[ExpectedRead]:
    p = root / "expected_reads.csv"
    if not p.exists():
        return []
    vocab = (*SIGNAL_NAMES, *TREND_STATES)
    out = []
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        if not {"date", "symbol", "expected"} <= set(cols):
            issues.append(Issue(p.name, 1, "", "columns must include 'date,symbol,expected' (optional 'note')"))
            return []
        for n, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            try:
                d = date.fromisoformat(row["date"])
            except ValueError:
                issues.append(Issue(p.name, n, "date", f"invalid date '{row['date']}' (use YYYY-MM-DD)"))
                continue
            exp = [x.strip() for x in row["expected"].split(";") if x.strip()]
            bad = [x for x in exp if x not in vocab]
            for x in bad:
                issues.append(Issue(p.name, n, "expected", f"unknown signal/trend '{x}'{_suggest(x, vocab)}"))
            if bad or not exp:
                continue
            out.append(ExpectedRead(d, _clean_symbol(row["symbol"]), exp, row.get("note", ""), n))
    return out


def _file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def load_config(root: Path) -> tuple[Config | None, list[Issue]]:
    """Load and validate every owner-maintained file. Returns (None, issues) when any error is present."""
    root = Path(root)
    issues: list[Issue] = []
    if not root.is_dir():
        return None, [Issue(str(root), None, "", "config directory not found")]
    settings = load_settings(root / "settings.yaml", issues)
    strategies = load_strategies(root / "strategies.yaml", issues)
    universe = load_universe(root, issues)
    sector_map = load_sector_map(root, universe, issues) if universe is not None else {}
    manual = load_manual_levels(root, issues)
    reads = load_expected_reads(root, issues)
    if any(i.severity == "error" for i in issues) or None in (settings, strategies, universe):
        return None, issues
    hashes = {
        name: _file_hash(root / name)
        for name in ("settings.yaml", "strategies.yaml", "universe.csv", "universe.txt", "sector_map.csv",
                     "manual_levels.yaml")
        if (root / name).exists()
    }
    hashes["settings_effective"] = settings.digest()
    return Config(root, settings, strategies, universe, sector_map, manual, reads, hashes), issues
