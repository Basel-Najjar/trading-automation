"""Config editor used by the Streamlit app: validate on a temp copy, write only when error-free."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import ROOT
from scanner.config import load_config
from scanner.config_edit import (
    CHECK_CHOICES,
    DEFAULT_SCANS,
    FLOW_CHOICES,
    SCANS,
    criterion_rows,
    modified_scans,
    restore_default_scans,
    save_edits,
    save_scan,
    scans_text,
    universe_table,
    validate_edits,
)
from scanner.models import CHECKS


@pytest.fixture
def csv_config(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    shutil.copytree(ROOT / "config", d)
    return d


def _cfg(d: Path):
    cfg, issues = load_config(d)
    assert cfg is not None, [str(i) for i in issues]
    return cfg


def _files(d: Path) -> dict[str, str]:
    return {n: (d / n).read_text(encoding="utf-8") for n in ("universe.csv", "settings.yaml", SCANS)}


# ----------------------------------------------------------------------------------------------
# universe.csv
# ----------------------------------------------------------------------------------------------


def test_sample_config_has_no_legacy_symbol_files():
    assert not (ROOT / "config" / "sector_map.csv").exists() and not (ROOT / "config" / "universe.txt").exists()
    assert (ROOT / "config" / DEFAULT_SCANS).exists()
    assert not (ROOT / "config" / "strategies.yaml").exists()


def test_round_trip_is_byte_identical(csv_config):
    before = _files(csv_config)
    assert not [i for i in save_edits(csv_config, universe_table(_cfg(csv_config)), "SPY") if i.severity == "error"]
    assert _files(csv_config) == before


def test_add_symbol_and_change_benchmark(csv_config):
    # a ticker that cannot already be in the owner's universe (the fixture copies the real config)
    rows = universe_table(_cfg(csv_config)) + [{"symbol": " zzzt ", "type": "stock", "enabled": True,
                                                "notes": None, "sector_etf": "xlk"}]
    save_edits(csv_config, rows, "qqq")
    cfg = _cfg(csv_config)
    assert "ZZZT" in cfg.symbols and cfg.sector_for("ZZZT") == "XLK"
    assert cfg.settings.data.benchmark == "QQQ"
    text = (csv_config / "settings.yaml").read_text(encoding="utf-8")
    assert "  benchmark: QQQ\n" in text and "# settings.yaml — global technical parameters" in text
    assert (csv_config / "universe.csv").read_text(encoding="utf-8").startswith("symbol,type,sector_etf,enabled,notes")


@pytest.mark.parametrize("bad", [
    {"symbol": "AAPL", "type": "stock", "enabled": True, "notes": "", "sector_etf": "XLK"},  # duplicate
    {"symbol": "ZZZ", "type": "bond", "enabled": True, "notes": "", "sector_etf": ""},      # invalid type
])
def test_errors_write_nothing(csv_config, bad):
    before = _files(csv_config)
    issues = save_edits(csv_config, universe_table(_cfg(csv_config)) + [bad], "SPY")
    assert any(i.severity == "error" for i in issues)
    assert _files(csv_config) == before


def test_all_disabled_is_an_error(csv_config):
    rows = [dict(r, enabled=False) for r in universe_table(_cfg(csv_config))]
    assert any(i.severity == "error" for i in validate_edits(csv_config, rows, "SPY"))


# ----------------------------------------------------------------------------------------------
# scans.yaml
# ----------------------------------------------------------------------------------------------


def _errors(issues) -> list[str]:
    return [str(i) for i in issues if i.severity == "error"]


def test_choices_match_model():
    assert CHECK_CHOICES == list(CHECKS)
    assert FLOW_CHOICES == ["with_flow", "not_countertrend"]


def test_default_file_is_in_canonical_layout():
    cfg = _cfg(ROOT / "config")
    assert scans_text(cfg.scans) == (ROOT / "config" / DEFAULT_SCANS).read_text(encoding="utf-8")


def test_unchanged_save_is_byte_identical(config_dir):
    before = _files(config_dir)
    for name, sc in _cfg(config_dir).scans.items():
        assert not _errors(save_scan(config_dir, name, sc.enabled, sc.min_pct, criterion_rows(sc))), name
    assert _files(config_dir) == before


def test_edit_one_scan(config_dir):
    rows = [{"check": "trend", "value": "uptrend, range", "weight": None, "label": ""},
            {"check": "signal_all", "value": "[support_hold resistance_break]", "weight": 3, "label": "Two"},
            {"check": "rsi_between", "value": "45, 65", "weight": float("nan"), "label": None},
            {"check": None, "value": None, "weight": None, "label": None}]  # a blank row added in the editor
    assert not _errors(save_scan(config_dir, "bullish", False, 55, rows))
    cfg = _cfg(config_dir)
    sc = cfg.scans["bullish"]
    assert (sc.enabled, sc.min_pct) == (False, 55)
    assert [(c.check, c.value, c.weight, c.label) for c in sc.criteria] == [
        ("trend", ["uptrend", "range"], 1.0, ""), ("signal_all", ["support_hold", "resistance_break"], 3.0, "Two"),
        ("rsi_between", (45.0, 65.0), 1.0, "")]
    assert modified_scans(cfg) == ["bullish"]
    assert "    - {signal_all: [support_hold, resistance_break], weight: 3, label: \"Two\"}\n" in \
        (config_dir / SCANS).read_text(encoding="utf-8")


def test_edit_lookback_within_and_channel(config_dir):
    rows = [{"check": "channel", "value": "8", "weight": 2, "within": None, "label": ""},
            {"check": "signal_any", "value": "support_held", "weight": None, "within": 4.0, "label": ""},
            {"check": "signal_any", "value": "support_hold", "weight": None, "within": float("nan"), "label": ""}]
    assert not _errors(save_scan(config_dir, "neutral", True, 70, rows, 7))
    sc = _cfg(config_dir).scans["neutral"]
    assert sc.lookback_bars == 7 and [(c.check, c.value, c.within) for c in sc.criteria] == [
        ("channel", 8, None), ("signal_any", ["support_held"], 4), ("signal_any", ["support_hold"], None)]
    text = (config_dir / SCANS).read_text(encoding="utf-8")
    assert "  lookback_bars: 7\n" in text and "    - {signal_any: [support_held], within: 4}\n" in text
    assert [r["within"] for r in criterion_rows(sc)] == [None, 4, None]
    assert not _errors(save_scan(config_dir, "neutral", True, 70, rows))  # no lookback given: kept
    assert _cfg(config_dir).scans["neutral"].lookback_bars == 7


@pytest.mark.parametrize(("row", "message"), [
    ({"check": "trend", "value": "sideways"}, "invalid trend state 'sideways'"),
    ({"check": "rsi_below", "value": "high"}, "cannot read 'high'"),
    ({"check": "rsi_between", "value": "40"}, "cannot read '40'"),
    ({"check": "signal_any", "value": "suport_hold"}, "did you mean 'support_hold'"),
    ({"check": "flow", "value": "aligned"}, "flow"),
    ({"check": "rsi_above", "value": "30", "weight": "heavy"}, "weight must be a number"),
    ({"check": "", "value": "uptrend"}, "pick a check"),
    ({"check": "channel", "value": "2.5"}, "cannot read '2.5'"),
    ({"check": "channel", "value": "2"}, "channel"),
    ({"check": "signal_any", "value": "support_hold", "within": "1.5"}, "within must be a whole number"),
    ({"check": "trend", "value": "uptrend", "within": 3}, "`within` applies only"),
])
def test_invalid_edit_writes_nothing(config_dir, row, message):
    before = _files(config_dir)
    issues = save_scan(config_dir, "bearish", True, 70, [row])
    assert any(message in e for e in _errors(issues)), _errors(issues)
    assert _files(config_dir) == before


def test_restore_defaults(config_dir):
    original = (config_dir / SCANS).read_text(encoding="utf-8")
    save_scan(config_dir, "neutral", True, 50, [{"check": "trend", "value": "range"}])
    assert modified_scans(_cfg(config_dir)) == ["neutral"]
    assert not _errors(restore_default_scans(config_dir))
    assert (config_dir / SCANS).read_text(encoding="utf-8") == original and modified_scans(_cfg(config_dir)) == []


def test_restore_without_default_file(config_dir):
    (config_dir / DEFAULT_SCANS).unlink()
    before = _files(config_dir)
    assert any(i.severity == "error" for i in restore_default_scans(config_dir))
    assert _files(config_dir) == before and modified_scans(_cfg(config_dir)) == []
