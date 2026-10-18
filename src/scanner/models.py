"""Shared value types. No analysis logic lives here beyond small helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

import numpy as np

# --- Vocabulary (single source of truth for config validation; signals.py registers implementations) ---

SIGNAL_NAMES: tuple[str, ...] = (
    "support_hold",
    "resistance_hold",
    "support_break",
    "resistance_break",
    "bullish_engulfing",
    "bearish_engulfing",
    "approaching_support",
    "approaching_resistance",
    "range_bound",
    "range_mid",
    "extended_up",
    "extended_down",
)
TREND_STATES: tuple[str, ...] = ("uptrend", "downtrend", "range", "mixed")
BIASES: tuple[str, ...] = ("bullish", "bearish", "non_bearish", "non_bullish", "neutral")
UP_BIASES: tuple[str, ...] = ("bullish", "non_bearish")
DOWN_BIASES: tuple[str, ...] = ("bearish", "non_bullish")
NA = "n/a"


class Tri(Enum):
    """Three-valued result: true, false, or depends on a still-forming bar."""

    TRUE = "true"
    FALSE = "false"
    PENDING = "PENDING_CLOSE"

    @staticmethod
    def of(value: bool) -> Tri:
        return Tri.TRUE if value else Tri.FALSE

    @staticmethod
    def any_(values: list[Tri]) -> Tri:
        """Kleene OR. Empty → FALSE."""
        if any(v is Tri.TRUE for v in values):
            return Tri.TRUE
        if any(v is Tri.PENDING for v in values):
            return Tri.PENDING
        return Tri.FALSE

    @staticmethod
    def all_(values: list[Tri]) -> Tri:
        """Kleene AND. Empty → TRUE."""
        if any(v is Tri.FALSE for v in values):
            return Tri.FALSE
        if any(v is Tri.PENDING for v in values):
            return Tri.PENDING
        return Tri.TRUE

    def negate(self) -> Tri:
        return {Tri.TRUE: Tri.FALSE, Tri.FALSE: Tri.TRUE, Tri.PENDING: Tri.PENDING}[self]


@dataclass
class Bars:
    """Per-symbol arrays for the analysis window; index t is the bar number (0 = oldest)."""

    dates: list[date]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    atr: np.ndarray
    rsi: np.ndarray
    sma50: np.ndarray
    sma200: np.ndarray
    relvol: np.ndarray

    def __len__(self) -> int:
        return len(self.close)

    def ohlc(self, t: int) -> dict[str, Any]:
        return {"date": self.dates[t].isoformat(), "open": round(float(self.open[t]), 4),
                "high": round(float(self.high[t]), 4), "low": round(float(self.low[t]), 4),
                "close": round(float(self.close[t]), 4)}


@dataclass(frozen=True)
class Pivot:
    kind: str  # "high" | "low"
    idx: int
    date: date
    price: float
    confirm_idx: int  # first bar index at which this pivot is known (idx + pivot_window)


@dataclass
class Level:
    """A support/resistance reference line. `values[t]` is the line's value at bar t (NaN if undefined)."""

    id: str
    type: str  # sma50 | sma200 | horizontal | diagonal
    values: np.ndarray = field(repr=False)
    origin: str | None = None  # support | resistance (pivot-based levels); None for SMAs
    touches: int = 0
    anchors: list[tuple[date, float]] = field(default_factory=list)
    touch_dates: list[date] = field(default_factory=list)
    last_touch_idx: int | None = None
    slope: float = 0.0  # price units per bar at the latest bar (diagonals, SMAs)
    quality: float = 0.0
    manual: bool = False
    note: str = ""

    def value(self, t: int) -> float:
        if t < 0 or t >= len(self.values):
            return float("nan")
        return float(self.values[t])

    def zone(self, t: int, atr: np.ndarray, k: float) -> tuple[float, float]:
        v = self.value(t)
        a = float(atr[t]) if 0 <= t < len(atr) else float("nan")
        return v - k * a, v + k * a

    def role(self, t: int, close: float) -> str:
        return "support" if close >= self.value(t) else "resistance"

    @property
    def label(self) -> str:
        tag = " (manual)" if self.manual else ""
        return f"{self.type}{tag}"


@dataclass
class AlertPrice:
    label: str
    price: float
    meaning: str


@dataclass
class SignalResult:
    name: str
    status: Tri
    age: int
    bar_idx: int
    date: date
    kind: str  # break | hold | pattern | context
    direction: str  # bullish | bearish | neutral
    level: Level | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        lvl = f" @ {self.level.label} {self.level.value(self.bar_idx):.2f}" if self.level is not None else ""
        age = "" if self.age == 0 else f" (age {self.age})"
        return f"{self.name}{lvl}{age}"


@dataclass
class NearResult:
    """A signal that has not fired but is close to firing (watch-list input)."""

    signal: str
    level: Level | None
    description: str
    distance_atr: float
    triggers: list[AlertPrice]


@dataclass
class RuleResult:
    rule: str
    status: Tri
    detail: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class TrendResult:
    state: str
    reason: str
    pivots_used: list[Pivot] = field(default_factory=list)
    range_high: Level | None = None
    range_low: Level | None = None
    range_width_atr: float | None = None
    range_position: float | None = None
    range_mid: bool = False


@dataclass
class FlowResult:
    market: str
    sector: str
    name: str
    sector_etf: str | None
    benchmark: str

    def components(self) -> list[tuple[str, str]]:
        etf = self.sector_etf or "sector"
        return [
            (f"{self.benchmark}", self.market),
            (f"{etf}/{self.benchmark}", self.sector),
            (f"name/{etf}", self.name),
        ]


@dataclass
class ScoreComponent:
    name: str
    raw: float  # 0..1
    weight: float
    points: float
    note: str = ""


@dataclass
class Candidate:
    symbol: str
    strategy_id: str
    strategy_name: str
    bias: str
    status: Tri  # TRUE (match) or PENDING
    rules: list[RuleResult]
    flow_label: str
    primary: SignalResult | None
    signals: list[SignalResult]
    score: float = 0.0
    components: list[ScoreComponent] = field(default_factory=list)
    stretched: bool = False
    rank: int = 0
    strategy_rank: int = 0
    level_quality: float = 0.0
    alerts: list[AlertPrice] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        return self.status is Tri.PENDING


@dataclass
class WatchItem:
    """A near-setup (symbol, signal, level) with every strategy it would serve."""

    symbol: str
    strategies: list[tuple[str, str]]  # (id, name)
    near: NearResult
    quality: float

    @property
    def sort_key(self) -> tuple:
        lid = self.near.level.id if self.near.level is not None else ""
        return (round(self.near.distance_atr, 6), -self.quality, self.symbol, self.near.signal, lid)


@dataclass
class Diagnostic:
    symbol: str | None
    stage: str
    severity: str  # warning | error | info
    message: str


@dataclass
class Tech:
    """Structural context (pivots, levels, trend) built from bars ≤ `cutoff`."""

    bars: Bars
    cutoff: int
    highs: list[Pivot]
    lows: list[Pivot]
    levels: list[Level]
    trend: TrendResult
    notes: list[str] = field(default_factory=list)
    memo: dict = field(default_factory=dict, repr=False, compare=False)  # per-structure results (signals._break)


@dataclass
class SymbolAnalysis:
    symbol: str
    bars: Bars
    last_idx: int  # latest bar (may be provisional)
    provisional: bool
    techs: list[Tech]  # techs[age]: structure used to evaluate bar last_idx − age
    signals: list[SignalResult]
    confluence: list[dict]
    nearest_support: list[dict]
    nearest_resistance: list[dict]
    near: dict[str, list[NearResult]]
    returns: dict[str, float | None]
    sector_etf: str | None = None
    flow: FlowResult | None = None
    signal_variants: dict[str, list[SignalResult]] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def trend(self) -> TrendResult:
        return self.techs[0].trend

    @property
    def levels(self) -> list[Level]:
        return self.techs[0].levels

    @property
    def completed_idx(self) -> int:
        return self.techs[0].cutoff

    @property
    def as_of(self) -> date:
        return self.bars.dates[self.last_idx]


def jsonable(obj: Any) -> Any:
    """Evidence dicts made JSON-safe and deterministic (numpy scalars → Python, NaN → None, floats rounded)."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return None if np.isnan(obj) else round(float(obj), 4)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj



def unique_by_id[T](items: list[T]) -> list[T]:
    """`items` without repeats of the same object, first occurrence kept. Identity, not ==: dataclass equality
    would compare numpy arrays."""
    seen: set[int] = set()
    out = []
    for x in items:
        if id(x) not in seen:
            seen.add(id(x))
            out.append(x)
    return out
