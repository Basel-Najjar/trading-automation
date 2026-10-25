"""Edit the config from the Streamlit app: universe.csv + benchmark, and each scan's criteria.

Validation reuses `load_config` on a temporary copy of the config directory, so nothing is written to the real
directory unless the edited configuration loads without errors.

Scan edits rewrite scans.yaml from the validated models in one fixed layout (one flow-style line per criterion), so
comments inside the file are not kept; a criterion's `label:` is the place for a note. `scans.default.yaml` is the
preserved original that "restore defaults" copies back.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from scanner.config import Config, Criterion, Issue, Scan, has_errors, load_config, load_scans, pydantic_issues
from scanner.models import CHECKS

COLUMNS = ["symbol", "type", "sector_etf", "enabled", "notes"]
SCANS = "scans.yaml"
DEFAULT_SCANS = "scans.default.yaml"
CRITERION_COLUMNS = ["check", "value", "weight", "within", "label"]
CHECK_CHOICES = list(CHECKS)
FLOW_CHOICES = list(get_args(get_args(Criterion.model_fields["flow"].annotation)[0]))
VALUE_HELP = ("trend / signal_all / signal_any: names separated by commas · rsi_below / rsi_above: a number · "
              f"rsi_between: low, high · flow: {' or '.join(FLOW_CHOICES)} · channel: number of bars")
SCANS_HEADER = """\
# scans.yaml: the five scans, most bullish to most bearish. Edit by hand or in the app's Criteria tab (it validates
# before saving and rewrites this file in this layout, so put notes in a criterion's `label:` rather than in comments).
#
# Each scan: enabled, min_pct (listed when the weighted % of criteria met reaches it), lookback_bars (a signal on any
# of the latest N bars counts; 5 bars ≈ one week) and criteria. Each criterion has exactly one check, an optional
# weight (default 1) and an optional label:
#   trend: [uptrend, downtrend, range, mixed]   signal_all / signal_any: [signal names, see docs/signals.md]
#   rsi_below: 70   rsi_above: 30   rsi_between: [40, 60]   flow: with_flow | not_countertrend
#   channel: 10 (every close of the last 10 completed bars between a horizontal support and resistance)
# A signal criterion can narrow the scan's lookback with `within: N`.
"""
_BENCH_RE = re.compile(r"^(?P<pre>[ \t]+benchmark:[ \t]*)(?P<val>[^\s#]+)", re.MULTILINE)


def _trial(root: Path, write) -> list[Issue]:
    """The issues `load_config` reports after applying `write(dir)` to a temp copy of `root` (root untouched)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "config"
        shutil.copytree(root, d)
        write(d)
        return load_config(d)[1]


def _validated_write(root: Path, write) -> list[Issue]:
    """Apply `write(dir)` to a temp copy and load it; if error-free, apply it to `root` too. Returns the issues."""
    root = Path(root)
    issues = _trial(root, write)
    if not has_errors(issues):
        write(root)
    return issues


# ----------------------------------------------------------------------------------------------
# universe.csv + benchmark
# ----------------------------------------------------------------------------------------------


def universe_table(cfg: Config) -> list[dict]:
    """One row per universe entry, in universe.csv's column order."""
    return [{"symbol": u.symbol, "type": u.type, "sector_etf": u.sector_etf or "", "enabled": u.enabled,
             "notes": u.notes} for u in cfg.universe]


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
                    "sector_etf": _text(r.get("sector_etf")).upper(), "enabled": _bool(r.get("enabled")),
                    "notes": _text(r.get("notes"))})
    return out


def _set_benchmark(text: str, benchmark: str) -> str:
    if _BENCH_RE.search(text):
        return _BENCH_RE.sub(lambda m: m["pre"] + benchmark, text, count=1)
    if re.search(r"^data:[ \t]*(#.*)?$", text, re.MULTILINE):
        return re.sub(r"^(data:[^\n]*\n)", rf"\g<1>  benchmark: {benchmark}\n", text, count=1, flags=re.MULTILINE)
    return text.rstrip("\n") + f"\n\ndata:\n  benchmark: {benchmark}\n"


def _write_universe(root: Path, rows: list[dict], benchmark: str) -> None:
    with (root / "universe.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r["symbol"], r["type"], r["sector_etf"], "true" if r["enabled"] else "false", r["notes"]])
    p = root / "settings.yaml"
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    p.write_text(_set_benchmark(text, benchmark), encoding="utf-8")


def _universe_precheck(rows: list[dict], benchmark: str) -> list[Issue]:
    issues: list[Issue] = []
    if not benchmark:
        issues.append(Issue("settings.yaml", None, "data.benchmark", "benchmark must not be empty"))
    syms = [r["symbol"] for r in normalize_rows(rows)]
    for sym in sorted({s for s in syms if syms.count(s) > 1}):
        issues.append(Issue("universe.csv", None, sym, "duplicate symbol"))
    return issues


def validate_edits(root: Path, rows: list[dict], benchmark: str) -> list[Issue]:
    """Issues `load_config` would report after the edit; the real config directory is not touched."""
    benchmark = benchmark.strip().upper()
    if issues := _universe_precheck(rows, benchmark):
        return issues
    return _trial(root, lambda d: _write_universe(d, normalize_rows(rows), benchmark))


def save_edits(root: Path, rows: list[dict], benchmark: str) -> list[Issue]:
    """Validate, then write universe.csv and settings.yaml's benchmark. Writes nothing on errors."""
    benchmark = benchmark.strip().upper()
    if issues := _universe_precheck(rows, benchmark):
        return issues
    return _validated_write(Path(root), lambda d: _write_universe(d, normalize_rows(rows), benchmark))


# ----------------------------------------------------------------------------------------------
# scans.yaml: criteria, enabled, min_pct, restore defaults
# ----------------------------------------------------------------------------------------------


def _num(v: float) -> str:
    return f"{v:g}"


def _yaml_value(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_yaml_value(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _num(float(v))
    return str(v)


def criterion_line(c: Criterion) -> str:
    """One criterion as a flow mapping: the check first, then weight (if not 1), within and label (if set)."""
    parts = [f"{c.check}: {_yaml_value(c.value)}"]
    if c.weight != 1:
        parts.append(f"weight: {_num(c.weight)}")
    if c.within is not None:
        parts.append(f"within: {c.within}")
    if c.label:
        parts.append(f"label: {json.dumps(c.label, ensure_ascii=False)}")
    return "{" + ", ".join(parts) + "}"


def scans_text(scans: dict[str, Scan]) -> str:
    """scans.yaml in its one canonical layout (the app writes this; scans.default.yaml uses it too)."""
    out = [SCANS_HEADER]
    for name, sc in scans.items():
        out.append(f"\n{name}:\n  enabled: {_yaml_value(sc.enabled)}\n  min_pct: {_num(sc.min_pct)}\n"
                   f"  lookback_bars: {sc.lookback_bars}\n")
        if not sc.criteria:
            out.append("  criteria: []\n")
            continue
        out.append("  criteria:\n")
        out += [f"    - {criterion_line(c)}\n" for c in sc.criteria]
    return "".join(out)


def _cell(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(_cell(x) for x in v)
    if isinstance(v, float):
        return _num(v)
    return _text(v)


def criterion_rows(scan: Scan) -> list[dict]:
    """One editor row per criterion (CRITERION_COLUMNS); list values are comma-separated text."""
    return [{"check": c.check, "value": _cell(c.value), "weight": c.weight, "within": c.within, "label": c.label}
            for c in scan.criteria]


def _whole(text: str) -> int:
    x = float(text)
    if not x.is_integer():
        raise ValueError(text)
    return int(x)


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[\s,\[\]]+", text) if w]


def parse_criterion_rows(rows: list[dict], where: str) -> tuple[list[dict], list[Issue]]:
    """Editor rows → raw criterion mappings for `Criterion` to validate. Blank rows are dropped."""
    out, issues = [], []
    for n, r in enumerate(rows, start=1):
        check, value, label = _text(r.get("check")), _text(r.get("value")), _text(r.get("label"))
        if not check and not value:
            continue
        loc = f"{where}.criteria row {n}"
        if check not in CHECKS:
            issues.append(Issue(SCANS, None, loc, f"pick a check ({', '.join(CHECKS)})"))
            continue
        try:
            if check in ("trend", "signal_all", "signal_any"):
                parsed: Any = _words(value)
            elif check == "rsi_between":
                parsed = [float(x) for x in _words(value)]
                if len(parsed) != 2:
                    raise ValueError
            elif check == "flow":
                parsed = value
            elif check == "channel":
                parsed = _whole(value)
            else:
                parsed = float(value)
        except ValueError:
            issues.append(Issue(SCANS, None, loc, f"cannot read '{value}' for {check} ({VALUE_HELP})"))
            continue
        raw: dict[str, Any] = {check: parsed}
        if w := _text(r.get("weight")):
            try:
                raw["weight"] = float(w)
            except ValueError:
                issues.append(Issue(SCANS, None, loc, f"weight must be a number, got '{w}'"))
                continue
        if (n := _text(r.get("within"))) and n.lower() not in ("nan", "<na>", "none"):
            try:
                raw["within"] = _whole(n)
            except ValueError:
                issues.append(Issue(SCANS, None, loc, f"within must be a whole number of bars, got '{n}'"))
                continue
        if label:
            raw["label"] = label
        out.append(raw)
    return out, issues


def save_scan(root: Path, name: str, enabled: bool, min_pct: float, rows: list[dict],
              lookback_bars: int | None = None) -> list[Issue]:
    """Validate, then rewrite scans.yaml with scan `name` replaced (`lookback_bars` None keeps the current one).
    Writes nothing on errors."""
    root = Path(root)
    criteria, issues = parse_criterion_rows(rows, name)
    if issues:
        return issues
    current = load_scans(root / SCANS, issues)
    if current is None:
        return issues
    lookback = current[name].lookback_bars if lookback_bars is None else lookback_bars
    try:
        scan = Scan.model_validate({"enabled": enabled, "min_pct": min_pct, "lookback_bars": lookback,
                                    "criteria": criteria})
    except ValidationError as e:
        return pydantic_issues(e, SCANS, {}, label=f"{name}.", root=Scan)
    new = {**current, name: scan}
    return _validated_write(root, lambda d: (d / SCANS).write_text(scans_text(new), encoding="utf-8"))


def default_scans(root: Path) -> dict[str, Scan] | None:
    """The preserved original scans (None if scans.default.yaml is missing or invalid)."""
    p = Path(root) / DEFAULT_SCANS
    if not p.exists():
        return None
    issues: list[Issue] = []
    scans = load_scans(p, issues)
    return None if scans is None or has_errors(issues) else scans


def modified_scans(cfg: Config) -> list[str]:
    """The scans that differ from scans.default.yaml."""
    defaults = default_scans(cfg.root)
    if defaults is None:
        return []
    return [n for n, s in cfg.scans.items() if s != defaults.get(n)]


def restore_default_scans(root: Path) -> list[Issue]:
    """Copy scans.default.yaml over scans.yaml, after checking the whole config still loads."""
    root = Path(root)
    if not (root / DEFAULT_SCANS).exists():
        return [Issue(DEFAULT_SCANS, None, "", "missing — nothing to restore from")]
    return _validated_write(root, lambda d: shutil.copyfile(root / DEFAULT_SCANS, d / SCANS))
