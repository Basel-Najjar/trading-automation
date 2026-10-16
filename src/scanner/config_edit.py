"""Edit universe.csv / sector_map.csv / benchmark from a table (used by the Streamlit app).

Validation reuses `load_config` on a temporary copy of the config directory, so nothing is written to the real
directory unless the edited configuration loads without errors.
"""

from __future__ import annotations

import csv
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from scanner.config import Config, Issue, load_config

COLUMNS = ["symbol", "type", "enabled", "notes", "sector_etf"]
_BENCH_RE = re.compile(r"^(?P<pre>[ \t]+benchmark:[ \t]*)(?P<val>[^\s#]+)", re.MULTILINE)


def universe_table(cfg: Config) -> list[dict]:
    """One row per universe entry, with its effective sector ETF (sector_map.csv wins, as in the loader)."""
    return [{"symbol": u.symbol, "type": u.type, "enabled": u.enabled, "notes": u.notes,
             "sector_etf": cfg.sector_for(u.symbol) or ""} for u in cfg.universe]


def _text(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v) if _text(v) else True


def normalize_rows(rows: list[dict]) -> list[dict]:
    """Upper-case symbols/ETFs, fill defaults and drop rows without a symbol."""
    out = []
    for r in rows:
        sym = _text(r.get("symbol")).upper()
        if not sym:
            continue
        out.append({"symbol": sym, "type": (_text(r.get("type")) or "stock").lower(),
                    "enabled": _bool(r.get("enabled")), "notes": _text(r.get("notes")),
                    "sector_etf": _text(r.get("sector_etf")).upper()})
    return out


def _set_benchmark(text: str, benchmark: str) -> str:
    if _BENCH_RE.search(text):
        return _BENCH_RE.sub(lambda m: m["pre"] + benchmark, text, count=1)
    if re.search(r"^data:[ \t]*(#.*)?$", text, re.MULTILINE):
        return re.sub(r"^(data:[^\n]*\n)", rf"\g<1>  benchmark: {benchmark}\n", text, count=1, flags=re.MULTILINE)
    return text.rstrip("\n") + f"\n\ndata:\n  benchmark: {benchmark}\n"


def _write(root: Path, rows: list[dict], benchmark: str) -> None:
    with (root / "universe.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "type", "enabled", "notes"])
        for r in rows:
            w.writerow([r["symbol"], r["type"], "true" if r["enabled"] else "false", r["notes"]])
    with (root / "sector_map.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "sector_etf"])
        for r in rows:
            if r["sector_etf"]:
                w.writerow([r["symbol"], r["sector_etf"]])
    p = root / "settings.yaml"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    p.write_text(_set_benchmark(text, benchmark), encoding="utf-8")


def validate_edits(root: Path, rows: list[dict], benchmark: str) -> list[Issue]:
    """Issues `load_config` would report after the edit; the real config directory is not touched."""
    root = Path(root)
    benchmark = benchmark.strip().upper()
    issues: list[Issue] = []
    if (root / "universe.txt").exists():
        issues.append(Issue("universe.txt", None, "", "the editor manages universe.csv; move universe.txt "
                                                       "into universe.csv (or delete it) first"))
    if not benchmark:
        issues.append(Issue("settings.yaml", None, "data.benchmark", "benchmark must not be empty"))
    syms = [r["symbol"] for r in normalize_rows(rows)]
    for sym in sorted({s for s in syms if syms.count(s) > 1}):
        issues.append(Issue("universe", None, sym, "duplicate symbol"))
    if issues:
        return issues
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "config"
        shutil.copytree(root, d)
        _write(d, normalize_rows(rows), benchmark)
        _, found = load_config(d)
    return found


def save_edits(root: Path, rows: list[dict], benchmark: str) -> list[Issue]:
    """Validate, then write universe.csv, sector_map.csv and settings.yaml's benchmark. Writes nothing on errors."""
    issues = validate_edits(root, rows, benchmark)
    if not any(i.severity == "error" for i in issues):
        _write(Path(root), normalize_rows(rows), benchmark.strip().upper())
    return issues
