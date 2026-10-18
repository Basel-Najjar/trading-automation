"""Edit the config from the Streamlit app: universe.csv + benchmark, and each strategy's technical rules.

Validation reuses `load_config` on a temporary copy of the config directory, so nothing is written to the real
directory unless the edited configuration loads without errors.

Strategy edits are surgical text edits located with PyYAML node marks: only the edited strategy's `technical:` block
and `enabled:` value change, keeping comments (including each technical key's trailing comment) and the layout of the
rest of the file. `strategies.default.yaml` is the preserved original that "restore defaults" copies back.
"""

from __future__ import annotations

import csv
import math
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, get_args

import yaml

from scanner.config import Config, Issue, Strategy, Technical, has_errors, load_config, load_strategies
from scanner.models import TREND_STATES

COLUMNS = ["symbol", "type", "sector_etf", "enabled", "notes"]
STRATEGIES = "strategies.yaml"
DEFAULT_STRATEGIES = "strategies.default.yaml"
TECH_KEYS = ("trend", "signals_all", "signals_any", "not_extended", "alignment")
# dropdown options, taken from the validating model so the editor can only offer legal values
TREND_CHOICES = ["any", *TREND_STATES]
NOT_EXTENDED_CHOICES = list(get_args(Technical.model_fields["not_extended"].annotation))
ALIGNMENT_CHOICES = list(get_args(Technical.model_fields["alignment"].annotation))
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
# strategies.yaml: technical rules + enabled, restore defaults
# ----------------------------------------------------------------------------------------------


def _flow(v: Any) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _strategy_node(text: str, sid: str) -> yaml.MappingNode:
    root = yaml.compose(text)
    if not isinstance(root, yaml.SequenceNode):
        raise ValueError(f"{STRATEGIES}: top level must be a list")
    for node in root.value:
        if isinstance(node, yaml.MappingNode) and any(k.value == "id" and v.value == sid for k, v in node.value):
            return node
    raise KeyError(sid)


def _last_line(v: yaml.Node) -> int:
    """Last line (0-based) holding part of a value. A block collection's end mark is the *next* token's position, so
    recurse into its last child instead."""
    if isinstance(v, (yaml.MappingNode, yaml.SequenceNode)) and not v.flow_style and v.value:
        last = v.value[-1]
        return _last_line(last[1] if isinstance(v, yaml.MappingNode) else last)
    end = v.end_mark
    return end.line - 1 if end.column == 0 and end.line > v.start_mark.line else end.line


def _trailing_comment(line: str, col: int) -> str:
    """The comment after a value, with the whitespace before it ("" if none)."""
    rest = line[col:].rstrip()
    i = rest.find("#")
    return rest if i >= 0 and not rest[:i].strip() else ""


def set_strategy_text(text: str, sid: str, technical: dict[str, Any], enabled: bool | None = None) -> str:
    """`text` with strategy `sid`'s technical keys (TECH_KEYS) and optionally `enabled` replaced.

    Other technical keys (e.g. `overrides`) keep their original lines; each managed key keeps its trailing comment.
    """
    lines = text.splitlines(keepends=True)
    node = _strategy_node(text, sid)
    entries = {k.value: (k, v) for k, v in node.value}
    edits: list[tuple[int, int, list[str]]] = []  # (first line, last line inclusive, replacement lines)

    first_key = node.value[0][0]
    ind = " " * first_key.start_mark.column
    if "technical" in entries:
        k, v = entries["technical"]
        start, end = k.start_mark.line, max(k.start_mark.line, _last_line(v))
        sub_ind = ind + "  "
        comments, keep, present = {}, [], set()
        if isinstance(v, yaml.MappingNode) and not v.flow_style:
            sub_ind = " " * (v.value[0][0].start_mark.column if v.value else len(ind) + 2)
            for kk, vv in v.value:
                if kk.value in TECH_KEYS:
                    present.add(kk.value)
                    if kk.start_mark.line == _last_line(vv):
                        comments[kk.value] = _trailing_comment(lines[kk.start_mark.line], vv.end_mark.column)
                else:
                    keep += lines[kk.start_mark.line:_last_line(vv) + 1]
        head = lines[start][:k.start_mark.column] + "technical:"
        out = [head + "\n"]
        for key in TECH_KEYS:
            if technical[key] == [] and key not in present:  # don't add empty signal lists that weren't there
                continue
            out.append(f"{sub_ind}{key}: {_flow(technical[key])}{comments.get(key, '')}\n")
        edits.append((start, end, out + keep))
    else:
        last = max(_last_line(v) for _, v in node.value)
        body = [f"{ind}  {key}: {_flow(technical[key])}\n" for key in TECH_KEYS if technical[key] != []]
        edits.append((last + 1, last, [f"{ind}technical:\n", *body]))

    if enabled is not None:
        if "enabled" in entries:
            k, v = entries["enabled"]
            ln = lines[v.start_mark.line]
            new = ln[:v.start_mark.column] + _flow(enabled) + ln[v.end_mark.column:]
            edits.append((v.start_mark.line, v.start_mark.line, [new]))
        else:
            k, v = entries["name"]
            edits.append((_last_line(v) + 1, _last_line(v), [f"{ind}enabled: {_flow(enabled)}\n"]))

    for start, end, new in sorted(edits, key=lambda e: e[0], reverse=True):
        lines[start:end + 1] = new
    return "".join(lines)


def technical_values(strat: Strategy) -> dict[str, Any]:
    t = strat.technical
    return {"trend": list(t.trend), "signals_all": list(t.signals_all), "signals_any": list(t.signals_any),
            "not_extended": t.not_extended, "alignment": t.alignment}


def save_strategy(root: Path, sid: str, technical: dict[str, Any], enabled: bool | None = None) -> list[Issue]:
    """Validate, then rewrite strategy `sid`'s technical rules (and `enabled`). Writes nothing on errors."""
    technical = {**technical, "trend": list(technical.get("trend") or ["any"])}
    try:
        Technical.model_validate(technical)
    except Exception as e:  # pydantic ValidationError: report it the way the loader would
        return [Issue(STRATEGIES, None, f"{sid}.technical", str(e).split("\n", 1)[-1].strip())]

    def write(d: Path) -> None:
        p = d / STRATEGIES
        p.write_text(set_strategy_text(p.read_text(encoding="utf-8"), sid, technical, enabled), encoding="utf-8")

    return _validated_write(Path(root), write)


def default_strategies(root: Path) -> dict[str, Strategy] | None:
    """The preserved original playbook, by id (None if strategies.default.yaml is missing or invalid)."""
    p = Path(root) / DEFAULT_STRATEGIES
    if not p.exists():
        return None
    issues: list[Issue] = []
    strats = load_strategies(p, issues)
    return None if strats is None or has_errors(issues) else {s.id: s for s in strats}


def modified_strategies(cfg: Config) -> dict[str, str]:
    """id → 'modified' / 'custom' for strategies that differ from strategies.default.yaml (technical or enabled)."""
    defaults = default_strategies(cfg.root)
    if defaults is None:
        return {}
    out = {}
    for s in cfg.strategies:
        d = defaults.get(s.id)
        if d is None:
            out[s.id] = "custom"
        elif s.technical != d.technical or s.enabled != d.enabled:
            out[s.id] = "modified"
    return out


def restore_default_strategies(root: Path) -> list[Issue]:
    """Copy strategies.default.yaml over strategies.yaml, after checking the whole config still loads."""
    root = Path(root)
    if not (root / DEFAULT_STRATEGIES).exists():
        return [Issue(DEFAULT_STRATEGIES, None, "", "missing — nothing to restore from")]
    return _validated_write(root, lambda d: shutil.copyfile(root / DEFAULT_STRATEGIES, d / STRATEGIES))
