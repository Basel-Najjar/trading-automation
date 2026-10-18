"""`config/markets.yaml`: the Markets page's lists (one per tool) and per-resolution parameters.

A series is a ticker (`SPY`) or a ratio of two tickers (`XLK/SPY`). Each resolution's `settings:` is a partial
override deep-merged over the scanner's `settings.yaml`, so technical definitions live in one place and only the
bar-count deltas a weekly chart needs are set here.
"""

from __future__ import annotations

import copy
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from scanner.config import Issue, Settings, _clean_symbol, _line_for, _pydantic_issues, _read_yaml, _Strict

MARKETS = "markets.yaml"
RESOLUTIONS = ("daily", "weekly")
Resolution = Literal["daily", "weekly"]
BARS_PER_YEAR = {"daily": 252, "weekly": 52}
LABELS = {"daily": "Daily (1Y)", "weekly": "Weekly (5Y)"}

# ----------------------------------------------------------------------------------------------
# Series
# ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesSpec:
    """A ticker (`den` None) or the ratio num/den."""

    label: str
    num: str
    den: str | None = None

    @property
    def symbols(self) -> list[str]:
        return [self.num] if self.den is None else [self.num, self.den]

    @property
    def is_ratio(self) -> bool:
        return self.den is not None


def parse_series(text: str) -> SeriesSpec:
    parts = [_clean_symbol(p) for p in str(text).split("/")]
    if not all(parts) or len(parts) > 2:
        raise ValueError(f"'{text}' is not a ticker or a ratio NUM/DEN")
    if any(c.isspace() for p in parts for c in p):
        raise ValueError(f"'{text}' contains whitespace")
    if len(parts) == 2 and parts[0] == parts[1]:
        raise ValueError(f"'{text}' is a ratio of a ticker to itself")
    return SeriesSpec("/".join(parts), parts[0], parts[1] if len(parts) == 2 else None)


def _series_list(v: Any) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError("must be a list of tickers or ratios")
    out: list[str] = []
    for x in v:
        label = parse_series(x).label
        if label in out:
            raise ValueError(f"duplicate series '{label}'")
        out.append(label)
    return out


# ----------------------------------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------------------------------


class RrgParams(_Strict):
    window: int = Field(10, ge=2)  # bars in the RS-Ratio / RS-Momentum z-score windows
    tail: int = Field(10, ge=1)  # bars of trail drawn per member


class PcaParams(_Strict):
    window: int = Field(52, ge=8)  # bars per rolling correlation window
    components: int = Field(3, ge=1)


class ResolutionParams(_Strict):
    display_years: float = Field(gt=0)
    history_years: float = Field(gt=0)  # analysed history (≥ display: warm-up for SMA200, ATR, pivots)
    rrg: RrgParams = Field(default_factory=RrgParams)
    pca: PcaParams = Field(default_factory=PcaParams)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _history(self) -> ResolutionParams:
        if self.history_years < self.display_years:
            raise ValueError("history_years must be ≥ display_years")
        return self


DEFAULT_RESOLUTIONS: dict[str, dict[str, Any]] = {
    "daily": {"display_years": 1, "history_years": 2, "rrg": {"window": 14, "tail": 10},
              "pca": {"window": 63, "components": 3}},
    "weekly": {"display_years": 5, "history_years": 8, "rrg": {"window": 10, "tail": 10},
               "pca": {"window": 52, "components": 3},
               "settings": {"pivots": {"pivot_window": 3},
                            "levels": {"recency_half_life_bars": 26, "trendline_lookback_bars": 104}}},
}


class RrgList(_Strict):
    benchmark: str = "SPY"
    members: list[str] = Field(default_factory=list)

    _members = field_validator("members", mode="before")(_series_list)

    @field_validator("benchmark", mode="before")
    @classmethod
    def _bench(cls, v: Any) -> str:
        spec = parse_series(v)
        if spec.is_ratio:
            raise ValueError("benchmark must be a ticker, not a ratio")
        return spec.label

    @model_validator(mode="after")
    def _not_self(self) -> RrgList:
        if self.benchmark in self.members:
            raise ValueError(f"benchmark {self.benchmark} cannot also be a member")
        if not self.members:
            raise ValueError("rrg.members must list at least one series")
        return self


class PcaList(_Strict):
    members: list[str] = Field(default_factory=list)

    _members = field_validator("members", mode="before")(_series_list)

    @model_validator(mode="after")
    def _enough(self) -> PcaList:
        if len(self.members) < 2:
            raise ValueError("pca.members must list at least two series")
        return self


class MarketsConfig(_Strict):
    levels: list[str]
    rrg: RrgList
    pca: PcaList
    resolutions: dict[Resolution, ResolutionParams] = Field(default_factory=dict, validate_default=True)

    _levels = field_validator("levels", mode="before")(_series_list)

    @field_validator("levels")
    @classmethod
    def _some(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("levels must list at least one series")
        return v

    @field_validator("resolutions", mode="before")
    @classmethod
    def _defaults(cls, v: Any) -> Any:
        """Missing resolutions (or keys within one) fall back to DEFAULT_RESOLUTIONS."""
        v = {} if v is None else v
        if not isinstance(v, dict):
            return v
        out = copy.deepcopy(DEFAULT_RESOLUTIONS)
        for name, params in v.items():
            if name in out and isinstance(params, dict):
                out[name].update(params)
            else:
                out[name] = params
        return out

    def specs(self, names: list[str]) -> list[SeriesSpec]:
        return [parse_series(n) for n in names]

    @property
    def symbols(self) -> list[str]:
        """Every ticker any list needs (ratio legs and the RRG benchmark included), deduplicated in order."""
        out: list[str] = []
        for name in [*self.levels, *self.rrg.members, self.rrg.benchmark, *self.pca.members]:
            out += parse_series(name).symbols
        return list(dict.fromkeys(out))

    def params(self, res: str) -> ResolutionParams:
        return self.resolutions[res]

    def bars(self, res: str, years: float) -> int:
        return max(1, int(round(years * BARS_PER_YEAR[res])))


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def settings_for(base: Settings, params: ResolutionParams) -> Settings:
    """The scanner's settings with this resolution's partial overrides applied (validated)."""
    if not params.settings:
        return base
    return Settings.model_validate(_deep_merge(base.model_dump(), params.settings))


# ----------------------------------------------------------------------------------------------
# Load / save
# ----------------------------------------------------------------------------------------------


def _model_issues(err: ValidationError, lines: dict[tuple, int]) -> list[Issue]:
    out = []
    for e in err.errors():
        loc = tuple(e["loc"])
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "extra_forbidden":
            msg = f"unknown key '{loc[-1] if loc else '?'}'"
        out.append(Issue(MARKETS, _line_for(lines, loc), ".".join(str(x) for x in loc), msg))
    return out


def load_markets(root: Path, base: Settings | None = None) -> tuple[MarketsConfig | None, list[Issue]]:
    """Load and validate `<root>/markets.yaml`. Returns (None, issues) on any error."""
    path = Path(root) / MARKETS
    issues: list[Issue] = []
    if not path.exists():
        return None, [Issue(MARKETS, None, "", "file missing — the Markets page needs it")]
    data, lines = _read_yaml(path, issues)
    if issues:
        return None, issues
    if not isinstance(data, dict):
        return None, [Issue(MARKETS, 1, "", "top level must be a mapping")]
    try:
        mc = MarketsConfig.model_validate(data)
    except ValidationError as e:
        return None, _model_issues(e, lines)
    base = base or Settings()
    for name, params in mc.resolutions.items():
        try:
            settings_for(base, params)
        except ValidationError as e:
            issues += _pydantic_issues(e, MARKETS, lines, ("resolutions", name, "settings"),
                                       f"resolutions.{name}.settings.")
    for name in RESOLUTIONS:
        p = mc.resolutions[name]
        if mc.bars(name, p.history_years) < 2 * p.pca.window:
            issues.append(Issue(MARKETS, _line_for(lines, ("resolutions", name, "pca")), f"resolutions.{name}.pca",
                                "history holds fewer than two PCA windows; the correlation change is n/a",
                                "warning"))
    if any(i.severity == "error" for i in issues):
        return None, issues
    return mc, issues


def _flow_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


HEADER = """\
# markets.yaml: lists for the Markets page (independent of universe.csv and of scanner runs).
# A series is a ticker (SPY) or a ratio NUM/DEN (XLK/SPY). The app's Lists tab rewrites the three lists and keeps
# the resolutions block below as written. Each resolution's `settings:` overrides settings.yaml for that resolution.
"""


def _top_level_block(text: str, key: str) -> str | None:
    """The verbatim text of top-level `key` (its line through the line before the next top-level key)."""
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        return None
    starts = [(k.value, k.start_mark.line) for k, _ in root.value]
    lines = text.splitlines(keepends=True)
    for i, (name, line) in enumerate(starts):
        if name == key:
            end = starts[i + 1][1] if i + 1 < len(starts) else len(lines)
            return "".join(lines[line:end]).rstrip() + "\n"
    return None


def render_markets(levels: list[str], benchmark: str, rrg_members: list[str], pca_members: list[str],
                   resolutions_block: str | None) -> str:
    text = (HEADER + "\n"
            f"levels: {_flow_list(levels)}\n\n"
            f"rrg:\n  benchmark: {benchmark}\n  members: {_flow_list(rrg_members)}\n\n"
            f"pca:\n  members: {_flow_list(pca_members)}\n")
    if resolutions_block:
        text += "\n" + resolutions_block
    return text


def _clean_list(items: list[Any]) -> list[str]:
    out = []
    for x in items:
        s = "" if x is None or (isinstance(x, float) and x != x) else str(x).strip()
        if s:
            out.append(s.upper())
    return out


def save_markets(root: Path, levels: list[Any], benchmark: str, rrg_members: list[Any], pca_members: list[Any],
                 base: Settings | None = None) -> list[Issue]:
    """Validate the edited lists, then rewrite markets.yaml (keeping its resolutions block). Writes nothing on
    errors."""
    path = Path(root) / MARKETS
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        block = _top_level_block(old, "resolutions") if old else None
    except yaml.YAMLError:
        block = None
    text = render_markets(_clean_list(levels), str(benchmark).strip().upper(), _clean_list(rrg_members),
                          _clean_list(pca_members), block)
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / MARKETS).write_text(text, encoding="utf-8")
        _, issues = load_markets(Path(tmp), base)
    if not any(i.severity == "error" for i in issues):
        path.write_text(text, encoding="utf-8")
    return issues
