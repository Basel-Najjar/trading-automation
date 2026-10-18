"""Streamlit Markets page: structure (trend, levels, breakouts), rotation (RRG) and correlation (rolling PCA) for the
lists in `config/markets.yaml`, at daily (1Y) or weekly (5Y) resolution.

Prices come from the shared cache when the page opens; only **Refresh prices** downloads, and it never touches a
scanner run. Session-state keys start with `mk_` so the scanner page's reset never clears them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from scanner.config import Issue, Settings, load_settings
from scanner.markets.config import LABELS, MARKETS, RESOLUTIONS, MarketsConfig, load_markets, save_markets
from scanner.markets.export import export_name, render_markets_html
from scanner.markets.figures import avg_corr_figure, corr_heatmap, explained_figure, loadings_figure, rrg_figure
from scanner.markets.run import MarketsResult, config_digest, run_markets
from scanner.markets.series import Prices, load_cached, prices_from, refresh_prices
from scanner.markets.structure import RETURN_BARS, detail_figure, level_rows, signal_rows

CONFIG_DIR = Path(os.environ.get("SCANNER_CONFIG_DIR", "config"))
TAB_STRUCTURE, TAB_ROTATION, TAB_CORRELATION, TAB_LISTS = "Structure", "Rotation", "Correlation", "Lists"
PLOTLY_CONFIG = {"displaylogo": False}
TABLE_ROWS = 14
CSS = "<style>.scn-muted { opacity: .6; font-weight: 400; font-size: .8em; }</style>"


def _issues_md(issues: list[Issue]) -> str:
    return "\n".join(f"- {i}" for i in issues)


def _load() -> tuple[MarketsConfig | None, Settings | None]:
    issues: list[Issue] = []
    base = load_settings(CONFIG_DIR / "settings.yaml", issues)
    if base is None:
        st.error("`settings.yaml` is invalid:\n\n" + _issues_md([i for i in issues if i.severity == "error"]))
        return None, None
    mc, m_issues = load_markets(CONFIG_DIR, base)
    if mc is None:
        st.error(f"`{MARKETS}` in `{CONFIG_DIR.resolve()}` is missing or invalid:\n\n"
                 + _issues_md([i for i in m_issues if i.severity == "error"]))
        return None, base
    return mc, base


def _clear() -> None:
    for key in list(st.session_state):
        if str(key).startswith(("mk_prices", "mk_results", "mk_figs", "mk_pick", "mk_struct_table")):
            st.session_state.pop(key, None)


def _store_prices(digest: str, prices: Prices) -> None:
    """Keep `prices` with a fresh token: results and figures are cached per token, so new prices recompute."""
    seq = st.session_state.get("mk_seq", 0) + 1
    st.session_state["mk_seq"] = seq
    st.session_state["mk_prices"] = (digest, prices, seq)


def _prices(mc: MarketsConfig, base: Settings) -> tuple[Prices, int]:
    digest = config_digest(mc)
    cached = st.session_state.get("mk_prices")
    if cached is None or cached[0] != digest:
        _store_prices(digest, load_cached(mc, base))
        cached = st.session_state["mk_prices"]
    return cached[1], cached[2]


def _result(mc: MarketsConfig, base: Settings, prices: Prices, token: int, res: str) -> MarketsResult:
    store = st.session_state.setdefault("mk_results", {})
    key = (res, token)
    if key not in store:
        with st.spinner(f"Analysing {LABELS[res].lower()}…"):
            store[key] = run_markets(mc, base, prices, res)
    return store[key]


# ----------------------------------------------------------------------------------------------
# Header
# ----------------------------------------------------------------------------------------------


def _header(mc: MarketsConfig, base: Settings) -> str:
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        st.markdown("## Markets", width="stretch")
        res = st.segmented_control("Resolution", list(RESOLUTIONS), default="weekly", key="mk_res", required=True,
                                   format_func=LABELS.get)
        force = st.checkbox("Force refresh", key="mk_force", help="Re-download full histories, bypassing the cache.")
        if st.button("Refresh prices", type="primary", icon=":material/refresh:",
                     help="Download the latest completed bars for every Markets symbol. Scanner runs are unaffected."):
            with st.spinner(f"Updating {len(mc.symbols)} symbols…"):
                dr = refresh_prices(mc, base, refresh=force)
            _clear()
            _store_prices(config_digest(mc), prices_from(dr))
    return res or "weekly"


def _status(r: MarketsResult, prices: Prices) -> None:
    with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
        when = f" · downloaded {prices.fetched_at[:16].replace('T', ' ')}" if prices.fetched_at else ""
        prov = " :orange-badge[current week still forming]" if any(sa.provisional for sa in r.analyses.values()) \
            else ""
        st.markdown(f"**Prices as of** {r.as_of:%a %d %b %Y}{when}{prov}" if r.as_of else
                    "**No cached prices yet**: press **Refresh prices**.", width="stretch")
        notes = [d for d in r.diagnostics if d.severity in ("warning", "error")]
        if notes:
            with st.popover(f"{len(notes)} data notes", type="tertiary", icon=":material/warning:"):
                for d in r.diagnostics:
                    st.markdown(f"- {d.severity.upper()} **{d.symbol}**: {d.message}")
        if r.analyses or r.rrg.tails:
            st.download_button("Export HTML", data=lambda: render_markets_html(r), file_name=export_name(r),
                               mime="text/html", icon=":material/download:", on_click="ignore", type="tertiary",
                               help="An offline snapshot of all three views at this resolution")


# ----------------------------------------------------------------------------------------------
# Structure
# ----------------------------------------------------------------------------------------------


def _structure(r: MarketsResult, token: int) -> None:
    if not r.summary:
        st.info("No series could be analysed. Check the data notes, or press **Refresh prices**.")
        return
    labels = [row["Series"] for row in r.summary]
    picked = ((st.session_state.get("mk_struct_table") or {}).get("selection") or {}).get("rows") or []
    if picked and picked[0] < len(labels):
        st.session_state["mk_pick"] = labels[picked[0]]
    sel = st.session_state.get("mk_pick")
    if sel not in labels:
        sel = labels[0]
    df = pd.DataFrame(r.summary).drop(columns=["Provisional"])
    df.insert(0, "", ["▶" if x == sel else "" for x in labels])
    pct = {name: st.column_config.NumberColumn(format="%+.1f%%") for name in RETURN_BARS[r.resolution]}
    st.dataframe(df, hide_index=True, on_select="rerun", selection_mode="single-row", key="mk_struct_table",
                 height=min(len(df), TABLE_ROWS) * 35 + 38,
                 column_config={"": st.column_config.TextColumn(width=16),
                                "Last": st.column_config.NumberColumn(format="%.4g"),
                                "Chg %": st.column_config.NumberColumn(format="%+.2f%%"), **pct,
                                "Support": st.column_config.NumberColumn(format="%.4g"),
                                "Resistance": st.column_config.NumberColumn(format="%.4g"),
                                "Sup ATR": st.column_config.NumberColumn(format="%+.2f", help="ATRs from the close"),
                                "Res ATR": st.column_config.NumberColumn(format="%+.2f", help="ATRs from the close"),
                                "Signal": st.column_config.TextColumn(width=240,
                                                                      help="Latest break / hold / pattern"),
                                "Near": st.column_config.TextColumn(width=220,
                                                                    help="Closest signal that has not fired")})
    st.caption("Pick a row to chart it. Trend, levels and breaks use the scanner's definitions at this resolution.")

    sa = r.analyses[sel]
    st.markdown(f"### {sel} <span class='scn-muted'>{sa.trend.reason}</span>", unsafe_allow_html=True)
    figs = st.session_state.setdefault("mk_figs", {})
    key = (r.resolution, sel, token)
    if key not in figs:
        figs[key] = detail_figure(sa, r.settings, r.resolution, r.display_bars)
    st.plotly_chart(figs[key], width="stretch", theme="streamlit", key=f"mk_chart_{sel}_{r.resolution}",
                    config=PLOTLY_CONFIG)
    left, right = st.columns(2, gap="medium")
    with left:
        st.markdown("**Nearest levels**")
        st.dataframe(pd.DataFrame(level_rows(sa, r.settings.levels.zone_atr_mult)), hide_index=True,
                     column_config={"Price": st.column_config.NumberColumn(format="%.4g"),
                                    "ATR away": st.column_config.NumberColumn(format="%+.2f")})
    with right:
        st.markdown("**Signals that fired**")
        rows = signal_rows(sa)
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True,
                         column_config={"Level": st.column_config.NumberColumn(format="%.4g")})
        else:
            st.caption("No break, hold or pattern on the latest bars.")


# ----------------------------------------------------------------------------------------------
# Rotation
# ----------------------------------------------------------------------------------------------


def _rotation(r: MarketsResult, mc: MarketsConfig) -> None:
    rrg = r.rrg
    if not rrg.tails:
        st.info("No RRG members could be computed. Check the data notes.")
        return
    full = mc.params(r.resolution).rrg.tail
    unit = "weeks" if r.resolution == "weekly" else "days"
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        st.markdown(f"**Rotation vs {rrg.benchmark}** · window {mc.params(r.resolution).rrg.window} {unit}",
                    width="stretch", help="RS-Ratio: z-scored relative strength vs the benchmark. RS-Momentum: "
                                          "z-scored rate of change of RS-Ratio. Both centre on 100; rotation "
                                          "normally runs clockwise.")
        tail = st.slider(f"Tail ({unit})", 1, full, full, key=f"mk_tail_{r.resolution}", width=240)
    st.plotly_chart(rrg_figure(rrg, r.rrg_order, tail), width="stretch", theme="streamlit",
                    key=f"mk_rrg_{r.resolution}", config=PLOTLY_CONFIG)
    st.dataframe(pd.DataFrame(rrg.table), hide_index=True,
                 column_config={"Was": st.column_config.TextColumn(help=f"Quadrant {full} bars ago"),
                                "RS-Ratio": st.column_config.NumberColumn(format="%.2f"),
                                "RS-Momentum": st.column_config.NumberColumn(format="%.2f"),
                                "Heading °": st.column_config.NumberColumn(
                                    format="%.0f", help="Direction of the last step: 0° right (gaining strength), "
                                                        "90° up (gaining momentum)"),
                                "Speed": st.column_config.NumberColumn(format="%.2f", help="Length of the last step"),
                                "Distance": st.column_config.NumberColumn(format="%.2f",
                                                                          help="Distance from the centre (100, 100)")})


# ----------------------------------------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------------------------------------


def _correlation(r: MarketsResult) -> None:
    p = r.pca
    if p.explained.empty:
        st.info("Not enough common history for PCA. Check the data notes.")
        return
    unit = "weeks" if r.resolution == "weekly" else "days"
    last = p.explained.iloc[-1]
    m = st.columns(3)
    m[0].metric("PC1 share", f"{last.iloc[0]:.0%}", border=True,
                delta=f"{(last.iloc[0] - p.explained.iloc[-1 - p.window, 0]) * 100:+.1f} pts"
                if len(p.explained) > p.window else None, delta_color="off",
                help=f"Variance explained by the first component in the last {p.window} {unit}, vs one window "
                     "earlier. Rising: the series are moving together.")
    m[1].metric(f"Top {p.components} share", f"{last.sum():.0%}", border=True, help="Absorption ratio")
    m[2].metric("Average correlation", f"{p.avg_corr.iloc[-1]:+.2f}", border=True)
    left, right = st.columns(2, gap="medium")
    left.plotly_chart(explained_figure(p), width="stretch", theme="streamlit", key=f"mk_expl_{r.resolution}",
                      config=PLOTLY_CONFIG)
    right.plotly_chart(avg_corr_figure(p), width="stretch", theme="streamlit", key=f"mk_avg_{r.resolution}",
                       config=PLOTLY_CONFIG)
    comp = st.segmented_control("Component", list(p.loadings_now.columns), default="PC1",
                                key=f"mk_pc_{r.resolution}", required=True)
    st.plotly_chart(loadings_figure(p, comp or "PC1"), width="stretch", theme="streamlit",
                    key=f"mk_load_{r.resolution}", config=PLOTLY_CONFIG)
    left, right = st.columns(2, gap="medium")
    left.plotly_chart(corr_heatmap(p.corr_now, f"Correlation · last {p.window} {unit}"), width="stretch",
                      theme="streamlit", key=f"mk_corr_{r.resolution}", config=PLOTLY_CONFIG)
    if p.corr_change is not None:
        right.plotly_chart(corr_heatmap(p.corr_change, f"Change vs the {p.window} {unit} before"), width="stretch",
                           theme="streamlit", key=f"mk_dcorr_{r.resolution}", config=PLOTLY_CONFIG)
        st.markdown("**Largest correlation shifts**")
        st.dataframe(pd.DataFrame(p.pair_shifts()), hide_index=True,
                     column_config={c: st.column_config.NumberColumn(format="%+.2f")
                                    for c in ("Now", "Before", "Change")})
    else:
        right.info("Too little history for a second window, so the change is n/a.")


# ----------------------------------------------------------------------------------------------
# Lists
# ----------------------------------------------------------------------------------------------


def _list_editor(title: str, items: list[str], key: str, help_text: str) -> list:
    st.markdown(f"**{title}**", help=help_text)
    df = st.data_editor(pd.DataFrame({"series": items}), num_rows="dynamic", hide_index=True, key=key,
                        column_config={"series": st.column_config.TextColumn("Ticker or NUM/DEN ratio")})
    return df["series"].tolist()


def _lists(mc: MarketsConfig, base: Settings) -> None:
    st.caption(f"Saving rewrites the three lists in `{MARKETS}` under `{CONFIG_DIR.resolve()}` and keeps its "
               "`resolutions:` block as written. Nothing is written if validation finds errors. A ratio is two "
               "tickers separated by `/`, e.g. `XLK/SPY`.")
    rev = st.session_state.get("mk_list_rev", 0)
    c1, c2, c3 = st.columns(3, gap="medium")
    with c1:
        levels = _list_editor("Structure", mc.levels, f"mk_ed_levels_{rev}", "Series charted with trend and levels.")
    with c2:
        bench = st.text_input("RRG benchmark", value=mc.rrg.benchmark, key=f"mk_ed_bench_{rev}", width=160)
        rrg = _list_editor("Rotation members", mc.rrg.members, f"mk_ed_rrg_{rev}", "Plotted against the benchmark.")
    with c3:
        pca = _list_editor("Correlation members", mc.pca.members, f"mk_ed_pca_{rev}", "At least two series.")
    if st.button("Save lists", type="primary"):
        issues = save_markets(CONFIG_DIR, levels, bench, rrg, pca, base)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            st.error("Not saved:\n\n" + _issues_md(errors))
            return
        _clear()
        st.session_state["mk_list_rev"] = rev + 1
        st.session_state["mk_saved"] = [str(i) for i in issues]
        st.rerun()
    saved = st.session_state.pop("mk_saved", None)
    if saved is not None:
        st.success("Lists saved. Press **Refresh prices** if you added new tickers.")
        if saved:
            st.warning("\n".join(f"- {w}" for w in saved))


# ----------------------------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------------------------


def render() -> None:
    st.html(CSS)
    mc, base = _load()
    if mc is None or base is None:
        return
    res = _header(mc, base)
    prices, token = _prices(mc, base)
    r = _result(mc, base, prices, token, res)
    _status(r, prices)
    structure, rotation, correlation, lists = st.tabs([TAB_STRUCTURE, TAB_ROTATION, TAB_CORRELATION, TAB_LISTS],
                                                      key="mk_tab", on_change="rerun")
    with structure:
        if structure.open:
            _structure(r, token)
    with rotation:
        if rotation.open:
            _rotation(r, mc)
    with correlation:
        if correlation.open:
            _correlation(r)
    with lists:
        if lists.open:
            _lists(mc, base)
