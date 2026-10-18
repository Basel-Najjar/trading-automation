"""Display vocabulary shared by the Scanner and Markets pages: readable labels, icons and the day's change."""

from __future__ import annotations

from scanner.models import SymbolAnalysis, Tri

LABELS = {
    # biases
    "bullish": "Bullish", "bearish": "Bearish", "non_bearish": "Not bearish", "non_bullish": "Not bullish",
    "neutral": "Neutral",
    # flow
    "with_flow": "With flow", "countertrend": "Against flow", "mixed": "Mixed", "ignored": "Flow ignored",
    # trend
    "uptrend": "Uptrend", "downtrend": "Downtrend", "range": "Range",
    # rules
    "trend": "Trend", "signals_all": "Required", "signals_any": "Trigger", "not_extended": "Not extended",
    "alignment": "Flow",
}
TREND_ICON = {"uptrend": "▲", "downtrend": "▼", "range": "↔", "mixed": "≈"}
STATUS_ICON = {Tri.TRUE: "✓", Tri.FALSE: "✗", Tri.PENDING: "⏳"}


def humanize(token: str | None) -> str:
    """Readable label for a config/scanner identifier (`non_bearish` → "Not bearish", `support_hold` →
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
