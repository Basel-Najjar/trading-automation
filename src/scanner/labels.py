"""Display vocabulary shared by the Scanner and Markets pages: readable labels, icons and the day's change."""

from __future__ import annotations

from scanner.models import SymbolAnalysis, Tri

LABELS = {
    # scans
    "bullish": "Bullish", "non_bearish": "Non-bearish", "neutral": "Neutral", "non_bullish": "Non-bullish",
    "bearish": "Bearish",
    # flow
    "with_flow": "With flow", "not_countertrend": "Not against flow", "countertrend": "Against flow",
    "mixed": "Mixed",
    # trend
    "uptrend": "Uptrend", "downtrend": "Downtrend", "range": "Range",
    # criterion checks
    "trend": "Trend", "signal_all": "All signals", "signal_any": "Any signal", "rsi_below": "RSI below",
    "rsi_above": "RSI above", "rsi_between": "RSI between", "flow": "Flow", "channel": "Channel",
}
DIRECTION_ICON = {"bullish": "▲", "non_bearish": "△", "neutral": "↔", "non_bullish": "▽", "bearish": "▼"}
TREND_ICON = {"uptrend": "▲", "downtrend": "▼", "range": "↔", "mixed": "≈"}
STATUS_ICON = {Tri.TRUE: "✓", Tri.FALSE: "✗", Tri.PENDING: "⏳"}


def humanize(token: str | None) -> str:
    """Readable label for a config/scanner identifier (`with_flow` → "With flow", `support_hold` →
    "Support hold"). Unknown tokens get underscores replaced and the first letter capitalised."""
    if not token:
        return "n/a"
    if token in LABELS:
        return LABELS[token]
    text = token.replace("_", " ")
    return text[:1].upper() + text[1:]


def trend_text(state: str) -> str:
    icon = TREND_ICON.get(state)
    return f"{icon} {humanize(state)}" if icon else humanize(state)


def change_pct(sa: SymbolAnalysis) -> float:
    t = sa.last_idx
    close = float(sa.bars.close[t])
    prev = float(sa.bars.close[t - 1]) if t > 0 else close
    return (close / prev - 1) * 100 if prev else 0.0
