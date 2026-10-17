"""T-4: configuration loading and linting."""

from __future__ import annotations

from pathlib import Path

from conftest import ROOT
from scanner.config import Settings, load_config


def _issues(d: Path):
    cfg, issues = load_config(d)
    return cfg, [str(i) for i in issues if i.severity == "error"], [str(i) for i in issues if i.severity == "warning"]


def test_sample_config_is_valid():
    cfg, errors, _ = _issues(ROOT / "config")
    assert cfg is not None and not errors
    ids = [s.id for s in cfg.strategies]
    assert len(ids) == 11 and "bull_put_spread" in ids
    assert {s.id for s in cfg.strategies if not s.active} == {"ted_calendar", "flyagonal"}


def test_settings_yaml_matches_defaults():
    cfg, _, _ = _issues(ROOT / "config")
    assert cfg.settings == Settings()


def test_seeded_strategies_resolve_not_extended(cfg):
    assert cfg.strategy("bull_call_spread").not_extended_resolved == "up"
    assert cfg.strategy("bear_put_spread").not_extended_resolved == "down"
    assert cfg.strategy("bull_put_spread").not_extended_resolved == "none"


def test_signal_typo_reports_line_and_suggestion(config_dir):
    p = config_dir / "strategies.yaml"
    text = p.read_text(encoding="utf-8").replace("signals_any: [support_hold]\n", "signals_any: [suport_hold]\n", 1)
    p.write_text(text, encoding="utf-8")
    line = next(n for n, ln in enumerate(text.splitlines(), 1) if "suport_hold" in ln)
    cfg, errors, _ = _issues(config_dir)
    assert cfg is None
    assert any(f"strategies.yaml:{line}" in e and "did you mean 'support_hold'" in e for e in errors), errors


def test_duplicate_ids_and_bad_types(config_dir):
    p = config_dir / "strategies.yaml"
    text = p.read_text(encoding="utf-8")
    text = text.replace("- id: bear_put_spread", "- id: bull_call_spread", 1)
    text = text.replace("bias: non_bearish", "bias: bullsh", 1)
    text = text.replace("trend: [downtrend, range]", "trend: [downtrend, sideways]", 1)
    text = text.replace("trend: [uptrend]\n    signals_any: [support_hold, bullish_engulfing]",
                        "trend: [any, uptrend]\n    signals_any: [support_hold, bullish_engulfing]", 1)
    text = text.replace("max_bp_pct: 10\n", "max_bp_pct: 10\n", 1)
    text = text.replace("enabled: true", "enabled: maybe", 1)
    p.write_text(text, encoding="utf-8")
    _, errors, _ = _issues(config_dir)
    joined = "\n".join(errors)
    assert "duplicate id 'bull_call_spread'" in joined
    assert "invalid bias 'bullsh'" in joined and "did you mean 'bullish'" in joined
    assert "invalid trend state 'sideways'" in joined
    assert "'any' cannot be combined" in joined
    assert "enabled" in joined


def test_override_keys_restricted(config_dir):
    p = config_dir / "strategies.yaml"
    text = p.read_text(encoding="utf-8").replace(
        "    alignment: soft\n", "    alignment: soft\n    overrides: {pivot_window: 3}\n", 1)
    p.write_text(text, encoding="utf-8")
    _, errors, _ = _issues(config_dir)
    assert any("override 'pivot_window' not allowed" in e for e in errors)


def test_override_signal_keys_allowed(config_dir):
    p = config_dir / "strategies.yaml"
    text = p.read_text(encoding="utf-8").replace(
        "    alignment: soft\n", "    alignment: soft\n    overrides: {rsi_overbought: 75, full_body_ratio: 0.8}\n", 1)
    p.write_text(text, encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert not errors
    st = cfg.strategies[0]
    s2 = cfg.settings.with_overrides(st.technical.overrides)
    assert s2.indicators.rsi_overbought == 75 and s2.signals.full_body_ratio == 0.8
    assert cfg.settings.indicators.rsi_overbought == 70  # base untouched


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
    assert any("strategies.yaml" in e for e in errors) and any("universe" in e for e in errors)


def test_unmapped_symbol_warns(config_dir):
    _, _, warnings = _issues(config_dir)
    assert any("CCC" in w and "no sector_etf" in w for w in warnings)


def test_expected_reads_parsing(config_dir):
    (config_dir / "expected_reads.csv").write_text(
        "date,symbol,expected,note\n2026-09-15,aaa,uptrend;support_hold,ok\n2026-09-16,BBB,suport_hold,\n",
        encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert any("did you mean 'support_hold'" in e for e in errors)
    (config_dir / "expected_reads.csv").write_text(
        "date,symbol,expected,note\n2026-09-15,aaa,uptrend;support_hold,ok\n", encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert cfg.expected_reads[0].expected == ["uptrend", "support_hold"] and cfg.expected_reads[0].symbol == "AAA"


def test_manual_levels_validation(config_dir):
    (config_dir / "manual_levels.yaml").write_text(
        "AAA:\n  - {type: horizontal, role: support, price: 100}\n"
        "  - {type: diagonal, role: support, anchors: [[2026-01-05, 90]]}\n", encoding="utf-8")
    _, errors, _ = _issues(config_dir)
    assert any("exactly two 'anchors'" in e for e in errors)


def test_unknown_display_keys_preserved(config_dir):
    p = config_dir / "strategies.yaml"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "  name: Bull Call Spread\n", "  name: Bull Call Spread\n  notes: \"my note\"\n", 1), encoding="utf-8")
    cfg, errors, _ = _issues(config_dir)
    assert not errors and cfg.strategy("bull_call_spread").extra_fields == {"notes": "my note"}
