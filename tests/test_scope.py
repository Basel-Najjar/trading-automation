"""Docs coverage and data scope: every signal is documented; no code path reaches non-technical data."""

from __future__ import annotations

import io
import re
import tokenize

from conftest import ROOT
from scanner.models import SIGNAL_NAMES


def test_every_signal_is_documented():
    doc = (ROOT / "docs" / "signals.md").read_text(encoding="utf-8")
    for name in SIGNAL_NAMES:
        assert f"`{name}`" in doc, f"{name} missing from docs/signals.md"


def test_no_options_strategy_vocabulary():
    """The scanner is directional scans only: no options-strategy concepts in code, config or docs."""
    banned = re.compile(r"strateg|spread|pmcc|pmcp|\bwheel\b|flyagonal|premium|debit|"
                        r"ted_calendar|neutral_calendar", re.IGNORECASE)
    # the migration error for a leftover strategies.yaml, and the build backend's wheel target
    allowed = re.compile(r"strategies(\.default)?\.yaml|_STRATEGIES_GONE|tool\.hatch")
    paths = [*(ROOT / "src" / "scanner").rglob("*.py"), *(ROOT / "config").glob("*.yaml"),
             *(ROOT / "docs").glob("*.md"), ROOT / "README.md", ROOT / "pyproject.toml"]
    hits = [f"{p.relative_to(ROOT)}:{n}: {ln.strip()}" for p in paths
            for n, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if banned.search(ln) and not allowed.search(ln)]
    assert not hits, "\n".join(hits)


def test_no_out_of_scope_data_paths():
    """Acceptance #9: no code path touches ORATS, option chains, fundamentals, earnings or macro data.

    Checks code tokens only (names/attributes, split on '_'), not strings or comments, and that yfinance is
    used solely for yf.download (daily OHLCV), plus yf.Ticker(...).info in data.py for the app's display-only
    ticker name and business summary (no other info keys are read).
    """
    forbidden = {"orats", "options", "chain", "earnings", "earning", "fundamental", "fundamentals", "news",
                 "holders", "calendar_events", "financials", "recommendations"}
    for p in (ROOT / "src" / "scanner").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        names = {t.string for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.NAME}
        allowed = set()
        if p.name == "scanner_page.py":
            allowed.add("options")  # Streamlit's dropdown keyword (SelectboxColumn(options=...)), not option chains
        bad = sorted(n for n in names - allowed if forbidden & set(n.lower().split("_")))
        assert not bad, (p.name, bad)
        yf_ok = {"download", "Ticker"} if p.name == "data.py" else {"download"}
        assert set(re.findall(r"\byf\.(\w+)", src)) <= yf_ok, p.name
        if p.name == "data.py":
            keys = set(re.findall(r"""info\.get\(["'](\w+)["']\)""", src))
            assert keys and keys <= {"longName", "shortName", "longBusinessSummary"}, keys
            assert not re.search(r"info\[", src)
