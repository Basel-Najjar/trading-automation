"""Streamlit front-end: scan everything for a date, rank symbols by % of strategy rules met, drill into a symbol and
strategy, deep-dive/export the HTML report for a subset, and edit symbols and sector ETFs.

Run from the repository root:  uv run streamlit run src/scanner/app.py
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from scanner.app_view import chart_page, detail_view, filtered_result, strategy_rows, symbol_rankings
from scanner.config import Config, load_config
from scanner.config_edit import COLUMNS, save_edits, universe_table
from scanner.models import Tri
from scanner.pipeline import RunResult, run_scan
from scanner.report import render_html, suffix_for

CONFIG_DIR = Path(os.environ.get("SCANNER_CONFIG_DIR", "config"))
REPORT_HEIGHT = 1600
CARDS_PER_ROW = 5
ICON = {Tri.TRUE: "✅", Tri.FALSE: "❌", Tri.PENDING: "⏳"}


def _load() -> Config | None:
    cfg, issues = load_config(CONFIG_DIR)
    errors = [i for i in issues if i.severity == "error"]
    if errors or cfg is None:
        st.error(f"Configuration in `{CONFIG_DIR.resolve()}` is invalid:\n\n" + "\n".join(f"- {i}" for i in errors))
        return None
    return cfg


def _clear_results() -> None:
    for key in ("res", "cfg", "selected_symbol", "report_cache"):
        st.session_state.pop(key, None)


def _run(cfg: Config, as_of: date, refresh: bool) -> None:
    _clear_results()
    with st.spinner("Loading prices and scanning every symbol and strategy…"):
        res = run_scan(cfg, as_of=as_of, refresh=refresh)
    if not res.analyses:
        st.error("No symbols could be analysed:\n\n" + "\n".join(f"- {s}: {why}" for s, why in res.skipped)
                 + "".join(f"\n- {d.symbol}: {d.message}" for d in res.diagnostics if d.severity == "error"))
        return
    st.session_state["res"] = res
    st.session_state["cfg"] = cfg
    st.session_state["report_cache"] = {}


# ----------------------------------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------------------------------


def _summary(res: RunResult) -> None:
    warnings = [d for d in res.diagnostics if d.severity != "info"]
    metrics = [("As of", res.as_of.isoformat()), ("Symbols", len(res.analyses)), ("Candidates", len(res.candidates)),
               ("Watch items", len(res.watchlist)), ("Skipped", len(res.skipped)), ("Warnings", len(warnings))]
    for col, (label, val) in zip(st.columns(len(metrics)), metrics, strict=True):
        col.metric(label, val)
    if res.provisional:
        st.warning("The latest bar is PROVISIONAL (market still open): signals on it are PENDING_CLOSE and count as "
                   "not met. Re-run after the close.")
    for note in res.notes:
        st.info(note)
    if res.skipped or warnings:
        with st.expander("Skipped symbols and warnings"):
            for sym, why in res.skipped:
                st.write(f"- skipped {sym}: {why}")
            for d in warnings:
                st.write(f"- {d.severity.upper()} {d.symbol}: {d.message}")


def _cards(res: RunResult) -> None:
    ranked = symbol_rankings(res)
    st.subheader("Top symbols")
    st.caption("Ranked by the best strategy's share of rules met (PENDING_CLOSE counts as not met); ties by "
               "candidate score.")
    if not ranked:
        st.info("No symbols with evaluated strategies.")
        return
    n = len(ranked) if len(ranked) <= 1 else st.slider("Cards", 1, len(ranked), min(10, len(ranked)))
    selected = st.session_state.get("selected_symbol")
    shown = ranked[:n]
    for start in range(0, len(shown), CARDS_PER_ROW):
        cols = st.columns(CARDS_PER_ROW)
        for col, row in zip(cols, shown[start:start + CARDS_PER_ROW], strict=False):
            b = row.best
            with col.container(border=True):
                st.markdown(f"**{row.symbol}** · {row.close:,.2f} "
                            f":{'green' if row.change_pct >= 0 else 'red'}[{row.change_pct:+.2f}%]")
                st.metric("Best % met", f"{b.fraction:.0%}", f"{b.met}/{b.total} rules", delta_color="off")
                st.caption(f"{b.ev.strategy.name}  \n{row.trend} · {row.n_full} full match"
                           f"{'' if row.n_full == 1 else 'es'}{' · ⏳ pending' if row.pending else ''}")
                if st.button("View", key=f"card_{row.symbol}", width="stretch",
                             type="primary" if row.symbol == selected else "secondary"):
                    st.session_state["selected_symbol"] = row.symbol
                    st.rerun()


def _strategies(res: RunResult, symbol: str) -> str | None:
    rows = strategy_rows(res, symbol)
    st.subheader(f"Strategies for {symbol}")
    st.caption("Click a row for details.")
    df = pd.DataFrame([{
        "Strategy": r.ev.strategy.name, "Bias": r.ev.strategy.bias, "% met": r.fraction * 100,
        "Rules met": f"{r.met}/{r.total}", "Pending": r.pending or None, "Flow": r.ev.flow_label,
        "Score": r.cand.score if r.cand else None,
    } for r in rows])
    event = st.dataframe(
        df, hide_index=True, on_select="rerun", selection_mode="single-row", key=f"strategies_{symbol}",
        column_config={"% met": st.column_config.ProgressColumn("% met", min_value=0, max_value=100, format="%.0f%%"),
                       "Score": st.column_config.NumberColumn("Score", format="%.1f")},
    )
    picked = event.selection.rows
    return rows[picked[0]].ev.strategy.id if picked else (rows[0].ev.strategy.id if rows else None)


def _details(res: RunResult, cfg: Config, symbol: str, strategy_id: str) -> None:
    d = detail_view(res, cfg, symbol, strategy_id)
    st_, row = d["strategy"], d["row"]
    st.subheader(f"{symbol} × {st_.name}")
    st.caption(f"{row.met}/{row.total} rules met · bias {st_.bias} · flow {d['flow_label']} · close {d['close']:,.2f}"
               f" · ATR {d['atr']:.2f} · RSI {d['rsi']:.1f}" + (f" · score {row.cand.score:.1f}" if row.cand else ""))
    if st_.description:
        st.write(st_.description)
    left, right = st.columns([3, 2])
    with left:
        st.iframe(chart_page(d["chart"]), height="content")
    with right:
        st.markdown("**Rules**")
        for rule, status, detail, evidence in d["rules"]:
            st.markdown(f"{ICON[status]} **{rule}**: {detail}")
            for e in evidence:
                st.caption(e)
        st.markdown(f"**Trend**: {d['trend_reason']}")
        if d["flow"]:
            st.markdown("**Flow**: " + "; ".join(f"{k}: {v}" for k, v in d["flow"]))
    if d["alerts"]:
        st.markdown("**Alert prices**" + ("" if row.cand else " (near-miss triggers)"))
        st.dataframe(pd.DataFrame(d["alerts"], columns=["Alert", "Price", "Meaning"]), hide_index=True,
                     column_config={"Price": st.column_config.NumberColumn(format="%.2f")})
    st.markdown("**Nearest levels**")
    st.dataframe(pd.DataFrame([{"Level": lv["id"], "Type": lv["type"], "Role": lv["role"], "Price": lv["level"],
                                "Zone low": lv["zone_low"], "Zone high": lv["zone_high"], "Dist (ATR)": lv["dist_atr"],
                                "Touches": lv["touches"]} for lv in d["levels"]]),
                 hide_index=True, column_config={c: st.column_config.NumberColumn(format="%.2f")
                                                 for c in ("Price", "Zone low", "Zone high", "Dist (ATR)")})
    if d["score"]:
        with st.expander("Score breakdown"):
            st.dataframe(pd.DataFrame(d["score"], columns=["Component", "Raw", "Weight", "Points", "Note"]),
                         hide_index=True)
    if d["plan"]:
        with st.expander("Trade plan"):
            for key, val in d["plan"]:
                st.markdown(f"**{key}**")
                for v in val if isinstance(val, list) else [val]:
                    st.markdown(f"- {v}")
    for title, items in (("Construction", d["construction"]), ("ORATS checklist", d["checklist"]),
                         ("Other manual checks", d["manual_checks"])):
        if items:
            with st.expander(title):
                st.markdown("\n".join(f"- {x}" for x in items))


def _deep_dive(res: RunResult, cfg: Config) -> None:
    st.subheader("Deep dive")
    st.caption("Pick symbols and/or strategies to filter the full HTML report. Picked symbols show every picked "
               "strategy, including rules that did not match. Leave both empty for the full report.")
    c1, c2 = st.columns(2)
    symbols = c1.multiselect("Symbols", sorted(res.analyses))
    strategies = c2.multiselect("Strategies", res.strategies_enabled,
                                format_func=lambda sid: cfg.strategy(sid).name if cfg.strategy(sid) else sid)
    key = (tuple(symbols), tuple(strategies))
    cache = st.session_state.setdefault("report_cache", {})
    if key not in cache:
        with st.spinner("Rendering report…"):
            view = filtered_result(res, symbols, strategies) if symbols or strategies else res
            cache[key] = render_html(view, cfg, deep=bool(symbols))
    html = cache[key]
    st.download_button("Export report", html.encode("utf-8"), mime="text/html", type="primary",
                       file_name=f"scan_{res.as_of.isoformat()}{suffix_for(symbols or None, strategies or None)}.html",
                       icon=":material/download:", on_click="ignore")
    st.iframe(html, height=REPORT_HEIGHT)


def _results() -> None:
    res: RunResult | None = st.session_state.get("res")
    cfg: Config | None = st.session_state.get("cfg")
    if res is None or cfg is None:
        st.info("Pick a date in the sidebar and press **Run analysis**.")
        return
    _summary(res)
    st.divider()
    _cards(res)
    symbol = st.session_state.get("selected_symbol")
    if symbol in res.analyses:
        st.divider()
        strategy_id = _strategies(res, symbol)
        if strategy_id:
            st.divider()
            _details(res, cfg, symbol, strategy_id)
    st.divider()
    _deep_dive(res, cfg)


# ----------------------------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------------------------


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
        _clear_results()
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
        refresh = st.checkbox("Force data refresh", help="Bypass the cache and re-download everything.")
        st.caption("Every run scans all enabled symbols and active strategies.")
        if st.button("Run analysis", type="primary", width="stretch"):
            _run(cfg, as_of, refresh)
    results, config = st.tabs(["Results", "Config"])
    with results:
        _results()
    with config:
        _config_editor(cfg)


main()
