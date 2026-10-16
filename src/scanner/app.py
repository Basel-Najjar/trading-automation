"""Streamlit front-end: pick a date, run the scan, view/export the HTML report, edit symbols and sector ETFs.

Run from the repository root:  uv run streamlit run src/scanner/app.py
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from scanner.config import Config, load_config
from scanner.config_edit import COLUMNS, save_edits, universe_table
from scanner.pipeline import run_scan
from scanner.report import render_html, suffix_for

CONFIG_DIR = Path(os.environ.get("SCANNER_CONFIG_DIR", "config"))
REPORT_HEIGHT = 1600


def _load() -> Config | None:
    cfg, issues = load_config(CONFIG_DIR)
    errors = [i for i in issues if i.severity == "error"]
    if errors or cfg is None:
        st.error(f"Configuration in `{CONFIG_DIR.resolve()}` is invalid:\n\n"
                 + "\n".join(f"- {i}" for i in issues if i.severity == "error"))
        return None
    return cfg


def _run(cfg: Config, as_of: date, symbols: list[str], strategies: list[str], refresh: bool) -> None:
    with st.spinner("Loading prices and running the scan…"):
        res = run_scan(cfg, as_of=as_of, refresh=refresh, symbols=symbols or None,
                       strategy_ids=strategies or None)
    if not res.analyses:
        st.session_state.pop("result", None)
        st.error("No symbols could be analysed:\n\n" + "\n".join(f"- {s}: {why}" for s, why in res.skipped)
                 + "".join(f"\n- {d.symbol}: {d.message}" for d in res.diagnostics if d.severity == "error"))
        return
    st.session_state["result"] = {
        "html": render_html(res, cfg, deep=bool(symbols)),
        "file_name": f"scan_{res.as_of.isoformat()}{suffix_for(symbols or None, strategies or None)}.html",
        "as_of": res.as_of, "provisional": res.provisional, "notes": list(res.notes),
        "symbols": len(res.analyses), "candidates": len(res.candidates), "watch": len(res.watchlist),
        "skipped": list(res.skipped), "runtime_s": res.runtime_s,
        "warnings": [f"{d.severity.upper()} {d.symbol}: {d.message}" for d in res.diagnostics
                     if d.severity != "info"],
    }


def _results() -> None:
    r = st.session_state.get("result")
    if r is None:
        st.info("Pick a date in the sidebar and press **Run analysis**.")
        return
    cols = st.columns(6)
    for col, (label, val) in zip(cols, [("As of", r["as_of"].isoformat()), ("Symbols", r["symbols"]),
                                        ("Candidates", r["candidates"]), ("Watch items", r["watch"]),
                                        ("Skipped", len(r["skipped"])), ("Warnings", len(r["warnings"]))],
                                 strict=True):
        col.metric(label, val)
    if r["provisional"]:
        st.warning("The latest bar is PROVISIONAL (market still open): signals on it are PENDING_CLOSE. "
                   "Re-run after the close.")
    for note in r["notes"]:
        st.info(note)
    if r["skipped"] or r["warnings"]:
        with st.expander("Skipped symbols and warnings"):
            for sym, why in r["skipped"]:
                st.write(f"- skipped {sym}: {why}")
            for w in r["warnings"]:
                st.write(f"- {w}")
    st.download_button("Export report", r["html"].encode("utf-8"), file_name=r["file_name"], mime="text/html",
                       type="primary", icon=":material/download:", on_click="ignore")
    st.caption(f"Scan took {r['runtime_s']:.1f}s.")
    st.iframe(r["html"], height=REPORT_HEIGHT)


def _config_editor(cfg: Config) -> None:
    st.subheader("Symbols and sector ETFs")
    st.caption(f"Saving rewrites `universe.csv` and `sector_map.csv`, and the benchmark line in `settings.yaml`, "
               f"under `{CONFIG_DIR.resolve()}`. Nothing is written if validation finds errors.")
    df = pd.DataFrame(universe_table(cfg), columns=COLUMNS)
    edited = st.data_editor(
        df, num_rows="dynamic", hide_index=True, key="universe_editor",
        column_config={
            "symbol": st.column_config.TextColumn("Symbol", required=True),
            "type": st.column_config.TextColumn("Type", default="stock", validate=r"^(stock|etf)$",
                                                help="stock or etf"),
            "enabled": st.column_config.CheckboxColumn("Enabled", default=True),
            "notes": st.column_config.TextColumn("Notes"),
            "sector_etf": st.column_config.TextColumn("Sector ETF", help="Blank: name-vs-sector flow is n/a."),
        },
    )
    benchmark = st.text_input("Benchmark", value=cfg.settings.data.benchmark)
    if st.button("Save config", type="primary"):
        issues = save_edits(CONFIG_DIR, edited.to_dict("records"), benchmark)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            st.error("Not saved:\n\n" + "\n".join(f"- {i}" for i in errors))
            return
        st.session_state.pop("result", None)
        st.session_state.pop("universe_editor", None)
        st.session_state["saved_warnings"] = [str(i) for i in issues]
        st.rerun()
    saved = st.session_state.pop("saved_warnings", None)
    if saved is not None:
        st.success("Config saved. Previous results were cleared; run the analysis again.")
        if saved:
            st.warning("\n".join(f"- {w}" for w in saved))


def main() -> None:
    st.set_page_config(page_title="Technical Scanner", page_icon=":material/candlestick_chart:", layout="wide")
    st.title("Technical Opportunity Scanner")
    cfg = _load()
    if cfg is None:
        return
    with st.sidebar:
        st.header("Run")
        as_of = st.date_input("Date", value=date.today(), max_value=date.today(),
                              help="A non-trading day uses the prior session. Today during market hours "
                                   "uses a provisional bar.")
        strategies = st.multiselect("Strategies", [s.id for s in cfg.strategies if s.active],
                                    format_func=lambda sid: cfg.strategy(sid).name,
                                    help="Empty: all active strategies.")
        symbols = st.multiselect("Deep dive symbols", cfg.symbols,
                                 help="Shows every strategy for these symbols, including non-matching rules.")
        refresh = st.checkbox("Force data refresh", help="Bypass the cache and re-download everything.")
        if st.button("Run analysis", type="primary", width="stretch"):
            _run(cfg, as_of, symbols, strategies, refresh)
    results, config = st.tabs(["Results", "Config"])
    with results:
        _results()
    with config:
        _config_editor(cfg)


main()
