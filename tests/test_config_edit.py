"""Config editor used by the Streamlit app: validate on a temp copy, write only when error-free."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import ROOT
from scanner.config import load_config
from scanner.config_edit import (
    ALIGNMENT_CHOICES,
    DEFAULT_STRATEGIES,
    NOT_EXTENDED_CHOICES,
    STRATEGIES,
    modified_strategies,
    restore_default_strategies,
    save_edits,
    save_strategy,
    set_strategy_text,
    technical_values,
    universe_table,
    validate_edits,
)


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
    return {n: (d / n).read_text(encoding="utf-8") for n in ("universe.csv", "settings.yaml", STRATEGIES)}


# ----------------------------------------------------------------------------------------------
# universe.csv
# ----------------------------------------------------------------------------------------------


def test_sample_config_has_no_legacy_symbol_files():
    assert not (ROOT / "config" / "sector_map.csv").exists() and not (ROOT / "config" / "universe.txt").exists()
    assert (ROOT / "config" / DEFAULT_STRATEGIES).exists()


def test_round_trip_is_byte_identical(csv_config):
    before = _files(csv_config)
    assert not [i for i in save_edits(csv_config, universe_table(_cfg(csv_config)), "SPY") if i.severity == "error"]
    assert _files(csv_config) == before


def test_add_symbol_and_change_benchmark(csv_config):
    rows = universe_table(_cfg(csv_config)) + [{"symbol": " pltr ", "type": "stock", "enabled": True,
                                                "notes": None, "sector_etf": "xlk"}]
    save_edits(csv_config, rows, "qqq")
    cfg = _cfg(csv_config)
    assert "PLTR" in cfg.symbols and cfg.sector_for("PLTR") == "XLK"
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
# strategies.yaml
# ----------------------------------------------------------------------------------------------


def test_dropdown_options_match_model():
    assert NOT_EXTENDED_CHOICES == ["auto", "none", "up", "down"]
    assert ALIGNMENT_CHOICES == ["soft", "required", "ignore"]


def test_unchanged_save_is_byte_identical(config_dir):
    before = _files(config_dir)
    for s in _cfg(config_dir).strategies:
        if not s.technical.is_empty:
            issues = save_strategy(config_dir, s.id, technical_values(s), s.enabled)
            assert not [i for i in issues if i.severity == "error"], (s.id, [str(i) for i in issues])
    assert _files(config_dir) == before


def test_edit_touches_only_that_strategy(config_dir):
    p = config_dir / STRATEGIES
    before = p.read_text(encoding="utf-8").splitlines()
    new = {"trend": ["uptrend", "range"], "signals_all": [], "signals_any": ["support_hold", "bullish_engulfing"],
           "not_extended": "up", "alignment": "required"}
    assert not [i for i in save_strategy(config_dir, "long_stock", new, enabled=False) if i.severity == "error"]
    after = p.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert changed == [
        ("  enabled: true", "  enabled: false"),
        ("    trend: [uptrend]", "    trend: [uptrend, range]"),
        ("    signals_any: [support_hold]", "    signals_any: [support_hold, bullish_engulfing]"),
        ("    alignment: soft                # owner favors rising relative strength vs sector ETF",
         "    alignment: required                # owner favors rising relative strength vs sector ETF"),
    ]
    st = _cfg(config_dir).strategy("long_stock")
    assert technical_values(st) == new and st.enabled is False
    assert modified_strategies(_cfg(config_dir)) == {"long_stock": "modified"}


def test_edit_flow_style_technical_and_keeps_overrides(config_dir):
    p = config_dir / STRATEGIES
    p.write_text(p.read_text(encoding="utf-8").replace(
        "    alignment: soft\n  construction:\n    - \"Sell a ~0.3Δ put 5–20 DTE\"",
        "    alignment: soft\n    overrides: {full_body_ratio: 0.8}  # mine\n  construction:\n"
        "    - \"Sell a ~0.3Δ put 5–20 DTE\"", 1), encoding="utf-8")
    new = {"trend": ["range"], "signals_all": ["range_bound"], "signals_any": [], "not_extended": "none",
           "alignment": "ignore"}
    assert not [i for i in save_strategy(config_dir, "flyagonal", new) if i.severity == "error"]
    assert not [i for i in save_strategy(config_dir, "bull_put_spread", dict(new, trend=["uptrend"]))
                if i.severity == "error"]
    cfg = _cfg(config_dir)
    assert technical_values(cfg.strategy("flyagonal")) == new
    assert cfg.strategy("bull_put_spread").technical.overrides == {"full_body_ratio": 0.8}
    assert "overrides: {full_body_ratio: 0.8}  # mine" in p.read_text(encoding="utf-8")


def test_invalid_edit_writes_nothing(config_dir):
    before = _files(config_dir)
    bad = {"trend": ["any", "uptrend"], "signals_all": [], "signals_any": [], "not_extended": "auto",
           "alignment": "soft"}
    issues = save_strategy(config_dir, "long_stock", bad)
    assert any(i.severity == "error" and "'any' cannot be combined" in i.message for i in issues)
    assert _files(config_dir) == before


def test_empty_trend_means_any(config_dir):
    new = {"trend": [], "signals_all": [], "signals_any": ["support_hold"], "not_extended": "none",
           "alignment": "soft"}
    save_strategy(config_dir, "long_stock", new)
    assert _cfg(config_dir).strategy("long_stock").technical.trend == ["any"]


def test_restore_defaults(config_dir):
    original = (config_dir / STRATEGIES).read_text(encoding="utf-8")
    p = config_dir / STRATEGIES
    p.write_text(set_strategy_text(original, "wheel", {"trend": ["uptrend"], "signals_all": [], "signals_any": [
        "support_hold"], "not_extended": "none", "alignment": "soft"}, enabled=False), encoding="utf-8")
    assert modified_strategies(_cfg(config_dir)) == {"wheel": "modified"}
    assert not [i for i in restore_default_strategies(config_dir) if i.severity == "error"]
    assert p.read_text(encoding="utf-8") == original and modified_strategies(_cfg(config_dir)) == {}


def test_restore_without_default_file(config_dir):
    (config_dir / DEFAULT_STRATEGIES).unlink()
    before = _files(config_dir)
    assert any(i.severity == "error" for i in restore_default_strategies(config_dir))
    assert _files(config_dir) == before and modified_strategies(_cfg(config_dir)) == {}
