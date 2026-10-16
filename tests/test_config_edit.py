"""Config editor used by the Streamlit app: validate on a temp copy, write only when error-free."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import ROOT
from scanner.config import load_config
from scanner.config_edit import save_edits, universe_table, validate_edits


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
    return {n: (d / n).read_text(encoding="utf-8") for n in ("universe.csv", "sector_map.csv", "settings.yaml")}


def test_round_trip_keeps_universe_and_mapping(csv_config):
    before = _cfg(csv_config)
    rows = universe_table(before)
    assert not [i for i in save_edits(csv_config, rows, "SPY") if i.severity == "error"]
    after = _cfg(csv_config)
    assert after.universe == [type(u)(u.symbol, u.type, u.enabled, u.notes, None) for u in before.universe]
    assert after.sector_map == before.sector_map
    assert after.settings == before.settings


def test_add_symbol_and_change_benchmark(csv_config):
    rows = universe_table(_cfg(csv_config)) + [{"symbol": " pltr ", "type": "stock", "enabled": True,
                                                "notes": None, "sector_etf": "xlk"}]
    save_edits(csv_config, rows, "qqq")
    cfg = _cfg(csv_config)
    assert "PLTR" in cfg.symbols and cfg.sector_for("PLTR") == "XLK"
    assert cfg.settings.data.benchmark == "QQQ"
    text = (csv_config / "settings.yaml").read_text(encoding="utf-8")
    assert "  benchmark: QQQ\n" in text and "# settings.yaml — global technical parameters" in text


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


def test_universe_txt_is_refused(config_dir):
    issues = validate_edits(config_dir, [{"symbol": "AAA"}], "SPY")
    assert any("universe.txt" in i.file for i in issues)
