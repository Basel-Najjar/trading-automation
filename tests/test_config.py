"""T-4: configuration loading and linting."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ROOT
from scanner.config import Settings, load_config
from scanner.models import DIRECTIONS


def _issues(d: Path):
    cfg, issues = load_config(d)
    return cfg, [str(i) for i in issues if i.severity == "error"], [str(i) for i in issues if i.severity == "warning"]


def test_sample_config_is_valid():
    cfg, errors, _ = _issues(ROOT / "config")
    assert cfg is not None and not errors
    assert list(cfg.scans) == list(DIRECTIONS) and list(cfg.active_scans) == list(DIRECTIONS)
    assert all(s.criteria for s in cfg.scans.values())


def test_settings_yaml_matches_defaults():
    cfg, _, _ = _issues(ROOT / "config")
    assert cfg.settings == Settings()


def _edit_scans(d: Path, old: str, new: str) -> str:
    p = d / "scans.yaml"
    text = p.read_text(encoding="utf-8")
    assert old in text, old
    text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    return text


def test_signal_typo_reports_line_and_suggestion(config_dir):
    text = _edit_scans(config_dir, "[support_held, rising_line_held]", "[suport_held, rising_line_held]")
    line = next(n for n, ln in enumerate(text.splitlines(), 1) if "suport_held" in ln)
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None
    assert any(f"scans.yaml:{line}" in e and "did you mean 'support_held'" in e for e in errors), errors


@pytest.mark.parametrize(("old", "new", "message"), [
    ("bullish:\n", "bulish:\n", "unknown key 'bulish' — did you mean 'bullish'?"),
    ("{trend: [downtrend],", "{trend: [downtrend, sideways],", "invalid trend state 'sideways'"),
    ("{trend: [range, mixed],", "{trend: [range, mixed], rsi_below: 70,", "exactly one of"),
    ("{rsi_above: 30,", "{rsi_above: 30, weight: 0,", "weight"),
    ("{rsi_between: [40, 60]}", "{rsi_between: [60, 40]}", "rsi_between must be [low, high]"),
    ("{rsi_below: 70,", "{rsi_blow: 70,", "unknown key 'rsi_blow' — did you mean 'rsi_below'?"),
    ("  min_pct: 65\n", "  min_pct: 150\n", "min_pct"),
    ("{flow: not_countertrend}", "{flow: aligned}", "flow"),
    ("  lookback_bars: 5\n", "  lookback_bars: 0\n", "lookback_bars"),
    ("  lookback_bars: 10\n", "  lookback_bars: 31\n", "lookback_bars"),
    ("{channel: 10,", "{channel: 2,", "channel"),
    ("{trend: [uptrend], weight: 2}", "{trend: [uptrend], weight: 2, within: 2}", "`within` applies only"),
    ("{signal_any: [support_hold],", "{signal_any: [support_hold], within: 0,", "within"),
])
def test_bad_scans_values(config_dir, old, new, message):
    _edit_scans(config_dir, old, new)
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any(message in e and "scans.yaml" in e for e in errors), errors


def test_lookback_and_within(config_dir):
    _edit_scans(config_dir, "{signal_any: [support_hold],", "{signal_any: [support_hold], within: 2,")
    cfg, errors, _ = _issues(config_dir)
    assert not errors
    nb = cfg.scans["non_bearish"]
    assert [nb.window(c) for c in nb.criteria[:2]] == [10, 2]
    assert cfg.scans["bullish"].bars_needed == 5
    assert cfg.scans["neutral"].bars_needed == 12  # 10-bar channel + the bar its levels come from + a forming bar
    assert cfg.lookback_bars == 12


def test_scan_without_criteria_warns_and_is_inactive(config_dir):
    p = config_dir / "scans.yaml"
    text = p.read_text(encoding="utf-8")
    p.write_text(text[:text.index("neutral:")] + "neutral:\n  criteria: []\n\n" + text[text.index("non_bullish:"):],
                 encoding="utf-8")
    cfg, errors, warnings = _issues(config_dir)
    assert not errors and not cfg.scans["neutral"].active
    assert list(cfg.active_scans) == ["bullish", "non_bearish", "non_bullish", "bearish"]
    assert any("enabled scan has no criteria" in w for w in warnings)


def test_missing_scan_is_off_with_warning(config_dir):
    """A scans.yaml from before the five scans: the missing ones load disabled, nothing fails."""
    p = config_dir / "scans.yaml"
    text = p.read_text(encoding="utf-8")
    p.write_text(text[:text.index("non_bearish:")] + text[text.index("neutral:"):text.index("non_bullish:")]
                 + text[text.index("\nbearish:\n") + 1:], encoding="utf-8")
    cfg, errors, warnings = _issues(config_dir)
    assert not errors and list(cfg.scans) == list(DIRECTIONS)
    assert list(cfg.active_scans) == ["bullish", "neutral", "bearish"]
    assert any("no non_bearish, non_bullish scan" in w and "Restore defaults" in w for w in warnings), warnings


def test_legacy_strategies_and_bias_config_are_errors(config_dir):
    (config_dir / "strategies.yaml").write_text("- id: wheel\n", encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("strategies.yaml" in e and "scans.yaml" in e for e in errors)
    (config_dir / "strategies.yaml").unlink()
    p = config_dir / "settings.yaml"
    base = p.read_text(encoding="utf-8")
    p.write_text(base + "\nscoring:\n  weights: {signal: 100}\n", encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("weighted scores were removed" in e for e in errors)
    p.write_text(base.replace("    bullish: [uptrend]\n", "    bulish: [uptrend]\n", 1), encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("unknown direction 'bulish' — did you mean 'bullish'?" in e for e in errors)
    p.write_text(base.replace("  chase_atr: 1.5\n", "  chase_atr: 1.5\n  max_signal_age_bars: 3\n", 1),
                 encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("signals.max_signal_age_bars" in e and "lookback_bars" in e for e in errors), errors


def test_unknown_settings_key_suggests(config_dir):
    p = config_dir / "settings.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("zone_atr_mult:", "zone_atr_mul:"), encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None
    assert any("unknown key 'zone_atr_mul'" in e and "zone_atr_mult" in e and "settings.yaml:" in e for e in errors)


def test_bad_setting_type(config_dir):
    p = config_dir / "settings.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace("pivot_window: 5", "pivot_window: five"), encoding="utf-8")
    _, errors, _ = _issues(config_dir)
    assert any("pivots.pivot_window" in e for e in errors)


def test_universe_csv(config_dir):
    cfg, errors, _ = _issues(config_dir)
    assert [u.symbol for u in cfg.universe] == ["AAA", "BBB", "CCC"]
    assert cfg.sector_map == {"AAA": "XLK", "BBB": "XLK"}
    (config_dir / "universe.csv").write_text(
        "symbol,type,enabled,notes,sector_etf\n aaa ,stock,true,,xly\nbbb,etf,false,,\nAAA,stock,true,,\nddd,,,,\n",
        encoding="utf-8")  # column order does not matter
    cfg, errors, warnings = _issues(config_dir)
    assert not errors
    assert [u.symbol for u in cfg.universe] == ["AAA", "BBB", "DDD"]
    assert cfg.symbols == ["AAA", "DDD"]
    assert any("duplicate symbol" in w for w in warnings)
    assert cfg.sector_for("AAA") == "XLY" and cfg.sector_for("DDD") is None


def test_legacy_symbol_files_are_errors(config_dir):
    for name, text in (("universe.txt", "AAA\n"), ("sector_map.csv", "symbol,sector_etf\nAAA,XLK\n")):
        (config_dir / name).write_text(text, encoding="utf-8")
        cfg, errors, _ = _issues(config_dir)
        assert cfg is None and any(name in e and "no longer supported" in e for e in errors)
        (config_dir / name).unlink()
    (config_dir / "universe.csv").unlink()
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("missing config/universe.csv" in e for e in errors)


def test_missing_files(tmp_path):
    cfg, errors, _ = _issues(tmp_path)
    assert cfg is None
    assert any("scans.yaml" in e for e in errors) and any("universe" in e for e in errors)


def test_unmapped_symbol_warns(config_dir):
    _, _, warnings = _issues(config_dir)
    assert any("CCC" in w and "no sector_etf" in w for w in warnings)


def test_retired_report_and_replay_config_is_rejected(config_dir):
    p = config_dir / "settings.yaml"
    base = p.read_text(encoding="utf-8")
    p.write_text(base + "\nreport:\n  chart_bars: 150\n", encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("report" in e and "display:" in e for e in errors)
    p.write_text(base + "\nreplay:\n  forward_horizons: [3]\n", encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("replay was removed" in e for e in errors)
    p.write_text(base, encoding="utf-8")
    (config_dir / "expected_reads.csv").write_text("date,symbol,expected\n", encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None and any("expected_reads.csv" in e for e in errors)


def test_manual_levels_validation(config_dir):
    (config_dir / "manual_levels.yaml").write_text(
        "AAA:\n  - {type: horizontal, role: support, price: 100}\n"
        "  - {type: diagonal, role: support, anchors: [[2026-01-05, 90]]}\n", encoding="utf-8")
    _, errors, _ = _issues(config_dir)
    assert any("exactly two 'anchors'" in e for e in errors)
