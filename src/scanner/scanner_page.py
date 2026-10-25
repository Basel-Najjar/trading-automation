"""Scanner page of the Streamlit app. It scans everything for a date, then presents the result from high level to
detail:

1. Overview band: session, benchmark, counts, watch list, skipped symbols and a sector strip that filters the list.
2. Scan lists: a switcher picks Bullish, Non-bearish, Neutral, Non-bullish or Bearish; each lists the tickers that
   reach its threshold, ranked
   by the weighted % of its criteria met. A ticker can be in more than one list.
3. Setup pane beside the list: the scans that list the ticker, chart, scan switcher (with each scan's match %), key
   numbers, a checklist of the scan's criteria and a price ladder.
4. Evidence tabs in the pane (why, levels, about), plus a link to the scan's criteria.

Other tabs: the scan criteria (with editing) and the symbol universe.

Rendered by the navigation shell in app.py (run from the repository root:  uv run streamlit run src/scanner/app.py).
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from scanner.app_view import (
    DIRECTION_ICON,
    STATUS_ICON,
    best_scan,
    card_tooltip,
    change_pct,
    detail_view,
    humanize,
    listed_scans,
    overview,
    scan_lists,
    scan_recipe,
    symbol_chart,
    ticker_description,
    ticker_scans,
    trend_text,
)
from scanner.config import Config, Scan, load_config
from scanner.config_edit import (
    CHECK_CHOICES,
    COLUMNS,
    CRITERION_COLUMNS,
    DEFAULT_SCANS,
    SCANS,
    VALUE_HELP,
    criterion_rows,
    modified_scans,
    restore_default_scans,
    save_edits,
    save_scan,
    universe_table,
)
from scanner.criteria import global_criteria, scan_criteria
from scanner.data import Profile, fetch_profiles
from scanner.models import MAX_LOOKBACK_BARS, SIGNAL_NAMES, ScanResult, Tri
from scanner.pipeline import RunResult, run_scan
from scanner.ui import picked_rows, show_errors, table_height

CONFIG_DIR = Path(os.environ.get("SCANNER_CONFIG_DIR", "config"))
TABLE_ROWS = 18  # visible rows in the opportunity list before it scrolls
TAB_RESULTS, TAB_CRITERIA, TAB_CONFIG = "Results", "Criteria", "Config"
DIRECTION_BADGE = {"bullish": ":green-badge[▲ Bullish]", "non_bearish": ":blue-badge[△ Non-bearish]",
                   "neutral": ":gray-badge[↔ Neutral]", "non_bullish": ":orange-badge[▽ Non-bullish]",
                   "bearish": ":red-badge[▼ Bearish]"}
SHORT_NAME = {"bullish": "Bull", "non_bearish": "Non-bear", "neutral": "Neutral", "non_bullish": "Non-bull",
              "bearish": "Bear"}  # the scan switcher has five segments in a narrow column
STATUS_MD = {Tri.TRUE: ":green[**✓**]", Tri.FALSE: ":red[**✗**]", Tri.PENDING: ":orange[**⏳**]"}
TREND_COLOR = {"uptrend": "green", "downtrend": "red"}
DISCLAIMER = ("Automated technical screening based on the owner's predefined rules; not investment advice. "
              "Manual validation is required before any trade.")
CSS = """<style>
.scn-muted { opacity: .6; font-weight: 400; }
[data-testid="stMetricValue"] { font-size: 1.45rem; }
/* the opportunity list stays in view while the setup pane beside it scrolls */
[data-testid="stLayoutWrapper"]:has(> .st-key-opps_list) {
  position: sticky; top: var(--app-header-height, 3.75rem); z-index: 1;
}
</style>"""


def _load() -> Config | None:
    cfg, issues = load_config(CONFIG_DIR)
    if show_errors(f"Configuration in `{CONFIG_DIR.resolve()}` is invalid:", issues) or cfg is None:
        return None
    return cfg


def _clear_results() -> None:
    for key in list(st.session_state):
        if key in ("res", "cfg", "selected_symbol", "shown_group", "profiles", "charts") or \
                str(key).startswith(("scan_", "opps_", "_pick_", "watch_table")):
            st.session_state.pop(key, None)


def _run(cfg: Config, as_of: date, refresh: bool) -> None:
    _clear_results()
    with st.spinner("Loading prices and running the scans…"):
        res = run_scan(cfg, as_of=as_of, refresh=refresh)
    if not res.analyses:
        st.error("No symbols could be analysed:\n\n" + "\n".join(f"- {s}: {why}" for s, why in res.skipped)
                 + "".join(f"\n- {d.symbol}: {d.message}" for d in res.diagnostics if d.severity == "error"))
        return
    st.session_state["res"] = res
    st.session_state["charts"] = {}  # figures belong to a run
    st.session_state["cfg"] = cfg
    with st.spinner("Loading ticker descriptions…"):
        st.session_state["profiles"] = fetch_profiles(sorted(res.analyses), Path(cfg.settings.data.cache_dir),
                                                      refresh=refresh)


def _description(cfg: Config, symbol: str) -> tuple[str, str] | None:
    profiles: dict[str, Profile] = st.session_state.get("profiles", {})
    return ticker_description(cfg, profiles, symbol)


def _scan_title(name: str) -> str:
    return f"{DIRECTION_ICON.get(name, '')} {humanize(name)}"


def _pct_md(x: float | None, scale: float = 100.0, nd: int = 1) -> str:
    """Signed percentage in green/red markdown; `x` is a fraction unless `scale` is 1."""
    if x is None:
        return "n/a"
    v = x * scale
    return f":{'green' if v >= 0 else 'red'}[{v:+.{nd}f}%]"


def _trend_md(state: str) -> str:
    return f":{TREND_COLOR.get(state, 'gray')}[{trend_text(state)}]"


# ----------------------------------------------------------------------------------------------
# Selection (the list and the watch list both pick the symbol shown in the setup pane)
# ----------------------------------------------------------------------------------------------


def _select(symbol: str, scan: str | None = None, clear: str | None = None) -> None:
    """Show `symbol` (and `scan`, else its best scan) in the setup pane. `clear` is a table key whose row selection
    is dropped, so picking the same row there again counts as a new pick."""
    st.session_state["selected_symbol"] = symbol
    if scan:
        st.session_state[f"scan_{symbol}"] = scan
    else:
        st.session_state.pop(f"scan_{symbol}", None)
    if clear:
        st.session_state.pop(clear, None)
        st.session_state.pop(f"_pick_{clear}", None)


def _apply_table_pick(key: str, picks: list[tuple[str, str | None]]) -> None:
    """Apply a row picked in the dataframe `key` (read from its state before it renders, so everything drawn
    above it already shows the pick). A pick is applied once, so later picks elsewhere are not overridden."""
    rows = picked_rows(key)
    pick = picks[rows[0]] if rows and rows[0] < len(picks) else None
    if pick is not None and st.session_state.get(f"_pick_{key}") != pick:
        st.session_state[f"_pick_{key}"] = pick
        _select(*pick)


def _to_criteria(scan: str) -> None:
    st.session_state["criteria_focus"] = scan
    st.session_state["main_tab"] = TAB_CRITERIA


# ----------------------------------------------------------------------------------------------
# Results: overview band
# ----------------------------------------------------------------------------------------------


def _sector_label(e: dict) -> str:
    icon = {"uptrend": "▲", "downtrend": "▼"}.get(e["trend"], "·")
    r1 = (e.get("returns") or {}).get("1m")
    ret = f" {r1 * 100:+.1f}%" if r1 is not None else ""
    return f"{e['etf']} {icon}{ret} · {e['setups']}"


def _overview(ov: dict) -> None:
    m = ov["market"]
    with st.container(border=True):
        # columns, not a horizontal container: a wrapping horizontal container leaves a tall gap around popovers
        session, market, counts, watch, issues = st.columns([2.3, 3.4, 2.6, 1.4, 1.9], vertical_alignment="center")
        badges = " :orange-badge[provisional bar]" if ov["provisional"] else ""
        if any("not a trading day" in n for n in ov["notes"]):
            badges += " :gray-badge[prior session]"
        session.markdown(f"**Session** {ov['as_of']:%a %d %b %Y}{badges}", help="\n\n".join(ov["notes"]) or None)
        if m:
            r = m["returns"]
            market.markdown(f"**{m['symbol']}** {m['close']:,.2f} {_trend_md(m['trend'].state)} · "
                            f"1m {_pct_md(r.get('1m'))} · 3m {_pct_md(r.get('3m'))}", help=m["trend"].reason)
        counts.markdown(" · ".join(f"{DIRECTION_ICON[g]} **{n}**" for g, n in ov["counts"].items())
                        + f" of {ov['n_symbols']} symbols",
                        help="Tickers listed per scan: "
                             + ", ".join(f"{DIRECTION_ICON[g]} {humanize(g)}" for g in ov["counts"])
                             + f" ({ov['n_listed']} distinct). A ticker is listed when the "
                             "weighted % of the scan's criteria it meets reaches the scan's minimum; it can be in "
                             "more than one scan.")
        with watch:
            _watch_popover(ov)
        if ov["skipped"] or ov["warnings"]:
            label = f"{len(ov['skipped'])} skipped · {len(ov['warnings'])} warnings"
            with issues, st.popover(label, type="tertiary", icon=":material/warning:"):
                for sym, why in ov["skipped"]:
                    st.markdown(f"- **{sym}** skipped: {why}")
                for d in ov["warnings"]:
                    st.markdown(f"- {d.severity.upper()} **{d.symbol}**: {d.message}")
        if ov["provisional"]:
            st.caption(":orange[The latest bar is still forming: signals on it are pending and count as not met. "
                       "Re-run after the close.]")
        if ov["sectors"]:
            st.pills("Sector", [e["etf"] for e in ov["sectors"]], selection_mode="multi", key="f_sector",
                     format_func=lambda etf: _sector_label(next(e for e in ov["sectors"] if e["etf"] == etf)),
                     help="Sector ETF · trend · 1-month return · symbols listed in a scan. Pick to filter the list.")


def _watch_rows(ov: dict) -> tuple[list[dict], list[tuple[str, str | None]]]:
    data = [{"Symbol": w.symbol, "Near": humanize(w.near.signal), "ATR away": w.near.distance_atr,
             "Scans": ", ".join(humanize(n) for n in w.scans)} for w in ov["watch"]]
    return data, [(w.symbol, w.scans[0] if w.scans else None) for w in ov["watch"]]


def _watch_popover(ov: dict) -> None:
    if not ov["watch"]:
        return
    with st.popover(f"Watch list · {len(ov['watch'])}", type="tertiary", icon=":material/visibility:",
                    help="Below a scan's minimum, but a signal is near: if it fired, the ticker would be listed."):
        data, _ = _watch_rows(ov)
        st.caption("Pick a row to open it.")
        st.dataframe(pd.DataFrame(data), hide_index=True, on_select="rerun", selection_mode="single-row",
                     key="watch_table", width=640,
                     column_config={"ATR away": st.column_config.NumberColumn(format="%.2f")})


# ----------------------------------------------------------------------------------------------
# Results: opportunities
# ----------------------------------------------------------------------------------------------


def _filters(res: RunResult) -> dict:
    symbols = st.multiselect("Symbol", sorted(res.analyses), key="f_symbol", placeholder="All symbols", width=260)
    return {"symbols": symbols, "sectors": st.session_state.get("f_sector") or []}


def _direction_pick(groups: dict[str, list[ScanResult]]) -> str:
    """The scan switcher above the list. Defaults to the first scan with tickers."""
    if st.session_state.get("f_direction") not in groups:
        st.session_state["f_direction"] = next((g for g, rows in groups.items() if rows), next(iter(groups)))
    return st.segmented_control(
        "Scan", list(groups), key="f_direction", required=True, label_visibility="collapsed",
        format_func=lambda g: f"{DIRECTION_ICON.get(g, '')} {SHORT_NAME.get(g, g)} · {len(groups[g])}",
        help="Scans from most bullish to most bearish: " + " · ".join(_scan_title(g) for g in groups),
    ) or st.session_state["f_direction"]


def _list_rows(res: RunResult, rows: list[ScanResult]) -> tuple[list[dict], list[tuple[str, str | None]]]:
    """Table rows for one scan and, per row, the (symbol, scan) a pick opens."""
    data, picks = [], []
    for r in rows:
        sa = res.analyses[r.symbol]
        also = [humanize(n) for n in listed_scans(res, r.symbol) if n != r.scan]
        data.append({"Symbol": r.symbol, "Chg %": change_pct(sa), "Match": r.pct, "Met": f"{r.met}/{r.total}",
                     "Trend": trend_text(sa.trend.state), "Also in": ", ".join(also)})
        picks.append((r.symbol, r.scan))
    return data, picks


def _table_key(f: dict, group: str) -> str:
    sig = repr(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in f.items())) + group
    return "opps_" + hashlib.md5(sig.encode()).hexdigest()[:10]


def _opportunity_table(data: list[dict], picks: list[tuple[str, str | None]], group: str, key: str,
                       selected: str | None) -> None:
    if not data:
        st.info(f"No ticker reaches the {group} scan's minimum with these filters. The watch list shows tickers "
                "that are close.")
        return
    df = pd.DataFrame(data)
    df.insert(0, "", ["▶" if sym == selected else "" for sym, _ in picks])
    st.dataframe(
        df, hide_index=True, on_select="rerun", selection_mode="single-row", key=key,
        height=table_height(len(df), TABLE_ROWS),
        column_config={
            "": st.column_config.TextColumn(width=16),
            "Symbol": st.column_config.TextColumn(width=62),
            "Chg %": st.column_config.NumberColumn(format="%+.2f%%", width=64),
            "Match": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%", width=90,
                                                     help="Weighted % of this scan's criteria met."),
            "Met": st.column_config.TextColumn(width=44, help="Criteria met / criteria in the scan."),
            "Trend": st.column_config.TextColumn(width=100),
            "Also in": st.column_config.TextColumn(help="Other scans that also list this ticker."),
        },
    )
    st.caption(f"{len(df)} tickers · pick one to open it")


# ----------------------------------------------------------------------------------------------
# Results: setup pane
# ----------------------------------------------------------------------------------------------


def _scan_label(r: ScanResult) -> str:
    return f"{_scan_title(r.scan)} · {r.pct:.0f}%" + (" ✓" if r.passed else "")


def _ladder(d: dict) -> None:
    st.markdown("**Price ladder**" + ("" if d["result"].passed else " :orange-badge[below the scan's minimum]"),
                help="Alert prices for TradingView, with the last close for reference. Highest first.")
    df = pd.DataFrame(d["ladder"])

    def style(row: pd.Series) -> list[str]:
        css = {"close": "font-weight: 700; background-color: rgba(117,121,199,.18)",
               "invalidation": "color: #d03b3b; font-weight: 600", "level": "font-weight: 600"}.get(row["kind"], "")
        return [css] * len(row)

    styled = (df[["price", "dist_pct", "label", "meaning", "kind"]].style.apply(style, axis=1)
              .format({"price": "{:,.2f}", "dist_pct": "{:+.1f}%"}))
    st.dataframe(styled, hide_index=True, column_order=["price", "dist_pct", "label"],
                 column_config={"price": "Price", "dist_pct": "vs close", "label": "Alert"})


def _rules(d: dict) -> None:
    st.markdown("**Criteria**")
    for line in d["rules"]:
        weight = f" ×{line.weight:g}" if line.weight != 1 else ""
        st.markdown(f"{STATUS_MD[line.status]} **{line.label}**{weight}: {line.summary}")


def _evidence_tabs(d: dict, cfg: Config, symbol: str, scan: str) -> None:
    why, levels, about = st.tabs(["Why", "Levels", "About"])
    with why:
        if d["hits"]:
            st.markdown("**Signals that fired**")
            st.dataframe(pd.DataFrame(d["hits"]), hide_index=True,
                         column_config={"Price": st.column_config.NumberColumn(format="%.2f"),
                                        "Bars ago": st.column_config.NumberColumn(width=70)})
        st.markdown(f"**Trend**: {d['trend_reason']}")
        if d["flow"]:
            st.markdown("**Flow**: " + " · ".join(f"{k} {humanize(v).lower()}" for k, v in d["flow"]))
        with st.expander("Raw criterion evidence"):
            for line in d["rules"]:
                st.markdown(f"{STATUS_ICON[line.status]} `{line.check}`: {line.detail}")
                if line.evidence:
                    st.caption("  \n".join(line.evidence))
        st.button("How this scan works", icon=":material/arrow_forward:", type="tertiary",
                  key=f"why_crit_{symbol}_{scan}", on_click=_to_criteria, args=(scan,))
    with levels:
        st.dataframe(pd.DataFrame([{
            "Level": humanize(lv["type"]) + (" (manual)" if lv["manual"] else ""), "Role": humanize(lv["role"]),
            "Price": lv["level"], "Zone": f"{lv['zone_low']:,.2f} – {lv['zone_high']:,.2f}",
            "ATR away": lv["dist_atr"], "Touches": lv["touches"]} for lv in d["levels"]]),
            hide_index=True, column_config={"Price": st.column_config.NumberColumn(format="%.2f"),
                                            "ATR away": st.column_config.NumberColumn(format="%+.2f")})
    with about:
        desc = _description(cfg, symbol)
        if desc:
            st.markdown(f"**{desc[0]}**" if desc[0] else "")
            st.write(desc[1])
        st.caption(f"Sector ETF: {d['sector'] or 'n/a'} · ATR {d['atr']:.2f}")


def _setup(res: RunResult, cfg: Config, symbol: str) -> None:
    rows = ticker_scans(res, symbol)
    if not rows:
        st.info(f"No scans were evaluated for {symbol}.")
        return
    key = f"scan_{symbol}"
    ids = [r.scan for r in rows]
    if st.session_state.get(key) not in ids:
        st.session_state[key] = best_scan(res, symbol)
    by_id = {r.scan: r for r in rows}

    desc = _description(cfg, symbol)
    sa = res.analyses[symbol]
    close = float(sa.bars.close[sa.last_idx])
    listed = listed_scans(res, symbol)
    badges = " ".join(DIRECTION_BADGE[n] for n in listed) or ":gray-badge[not listed]"
    st.markdown(f"### {symbol} <span class='scn-muted'>{desc[0] if desc else ''}</span>",
                unsafe_allow_html=True, help=card_tooltip(desc))
    st.markdown(f"{badges} **{close:,.2f}** {_pct_md(change_pct(sa), scale=1, nd=2)} · RSI "
                f"{float(sa.bars.rsi[sa.last_idx]):.1f} · ATR {float(sa.bars.atr[sa.last_idx]):.2f}"
                + (f" · sector {sa.sector_etf}" if sa.sector_etf else ""),
                help="Badges: the scans that list this ticker. Every scan is evaluated; pick one below to see its "
                     "criteria.")
    # one chart per symbol, above the scan pills: switching scan re-renders only what follows.
    # Overlays (sector ETF, benchmark, ratio) toggle inside the chart, so they never rerun the app either.
    charts = st.session_state.setdefault("charts", {})
    if symbol not in charts:
        charts[symbol] = symbol_chart(res, symbol)
    st.plotly_chart(charts[symbol], width="stretch", theme="streamlit", key=f"chart_{symbol}",
                    config={"displaylogo": False})
    with st.container(horizontal=True, vertical_alignment="center"):
        st.pills("Scan", ids, key=key, format_func=lambda s: _scan_label(by_id[s]), label_visibility="collapsed",
                 required=True, width="stretch")
        scan = st.session_state.get(key) or ids[0]
        st.button("Criteria", icon=":material/rule:", type="tertiary", key=f"crit_{symbol}_{scan}",
                  on_click=_to_criteria, args=(scan,), help="How this scan works")
    d = detail_view(res, symbol, scan)
    r: ScanResult = d["result"]

    m = st.columns(4)
    m[0].metric("Match", f"{r.pct:.0f}%", border=True,
                help=f"Weighted % of the {scan} scan's criteria met; listed at {r.min_pct:g}% or more."
                     + (f" {r.pending_pct:.0f}% more is pending the close." if r.pending_pct else ""))
    m[1].metric("Criteria met", f"{r.met}/{r.total}", border=True)
    m[2].metric("Trend", trend_text(d["trend"]), border=True)
    m[3].metric("Flow", humanize(d["flow_label"]), border=True)

    left, right = st.columns([3, 2], gap="medium")
    with left:
        _rules(d)
    with right:
        _ladder(d)
    _evidence_tabs(d, cfg, symbol, scan)


def _results() -> None:
    res: RunResult | None = st.session_state.get("res")
    cfg: Config | None = st.session_state.get("cfg")
    if res is None or cfg is None:
        st.info("Pick a session date above and press **Run analysis**. Every run evaluates all enabled symbols in "
                "every enabled scan; filtering happens afterwards.", icon=":material/play_circle:")
        return
    if not res.scans:
        st.warning("No scan is enabled with criteria. Add criteria in the **Criteria** tab.")
        return
    ov = overview(res, cfg)
    _apply_table_pick("watch_table", _watch_rows(ov)[1])
    _overview(ov)

    f = _filters(res)
    groups = scan_lists(res, f["symbols"], f["sectors"])
    left, right = st.columns([5, 7], gap="large")
    with left, st.container(key="opps_list"):
        group = _direction_pick(groups)
        data, picks = _list_rows(res, groups[group])
        key = _table_key(f, group)
        if group != st.session_state.get("shown_group", group):  # switched group: open its top ticker
            if picks:
                _select(*picks[0])
            else:
                st.session_state.pop("selected_symbol", None)
        st.session_state["shown_group"] = group
        _apply_table_pick(key, picks)
        selected = st.session_state.get("selected_symbol")
        if selected not in res.analyses:
            selected = picks[0][0] if picks else None
            if selected:
                st.session_state["selected_symbol"] = selected
        _opportunity_table(data, picks, group, key, selected)
    with right:
        if selected:
            _setup(res, cfg, selected)
    st.caption(DISCLAIMER)


# ----------------------------------------------------------------------------------------------
# Criteria
# ----------------------------------------------------------------------------------------------


def _flash(key: str) -> None:
    """Show (once) the result of a save that triggered a rerun."""
    msg = st.session_state.pop(key, None)
    if msg is not None:
        text, warnings = msg
        st.success(text)
        if warnings:
            st.warning("\n".join(f"- {w}" for w in warnings))


def _after_scan_save(text: str, issues: list) -> None:
    _clear_results()
    st.session_state["scans_rev"] = st.session_state.get("scans_rev", 0) + 1  # re-seed the edit widgets
    st.session_state["scans_saved"] = (text, [str(i) for i in issues])
    st.rerun()


def _restore_defaults(modified: list[str]) -> None:
    has_default = (CONFIG_DIR / DEFAULT_SCANS).exists()
    with st.popover("Restore defaults", icon=":material/restore:", disabled=not has_default,
                    help=None if has_default else f"`{DEFAULT_SCANS}` not found in the config directory."):
        changed = ", ".join(f"`{n}`" for n in modified) or "none"
        st.markdown(f"Replace `{SCANS}` with the preserved original `{DEFAULT_SCANS}`. Scans that differ from it: "
                    f"{changed}.")
        if st.button("Restore all scans", type="primary"):
            issues = restore_default_scans(CONFIG_DIR)
            if not show_errors("Not restored:", issues):
                _after_scan_save("Scans restored to defaults. Previous results were cleared.", issues)


def _scan_editor(name: str, scan: Scan) -> None:
    rev = st.session_state.get("scans_rev", 0)
    with st.expander("Edit criteria", icon=":material/edit:"), st.form(f"edit_{name}_{rev}", border=False):
        c1, c2, c3 = st.columns(3)
        enabled = c1.toggle("Enabled", value=scan.enabled)
        min_pct = c2.number_input("Minimum match %", min_value=0.0, max_value=100.0, value=float(scan.min_pct),
                                  step=5.0, help="A ticker is listed when the weighted % of criteria met reaches "
                                                 "this.")
        lookback = c3.number_input("Lookback (bars)", min_value=1, max_value=MAX_LOOKBACK_BARS,
                                   value=int(scan.lookback_bars), step=1,
                                   help="A signal on any of the latest this-many bars counts (5 ≈ one week). A "
                                        "criterion's Within overrides it.")
        df = pd.DataFrame(criterion_rows(scan), columns=CRITERION_COLUMNS).astype({"weight": float,
                                                                                   "within": "Int64"})
        rows = st.data_editor(
            df, num_rows="dynamic", hide_index=True, width="stretch", key=f"criteria_{name}_{rev}",
            column_config={
                "check": st.column_config.SelectboxColumn("Check", options=CHECK_CHOICES, required=True),
                "value": st.column_config.TextColumn("Value", help=VALUE_HELP, required=True),
                "weight": st.column_config.NumberColumn("Weight", min_value=0.1, step=0.5, default=1.0,
                                                        format="%g", help="Blank = 1."),
                "within": st.column_config.NumberColumn("Within", min_value=1, max_value=MAX_LOOKBACK_BARS, step=1,
                                                        format="%d", help="Signal checks only: bars to look back "
                                                                          "over. Blank = the scan's lookback."),
                "label": st.column_config.TextColumn("Label", help="Optional; a readable name is generated."),
            })
        st.caption(f"Value: {VALUE_HELP}. Signals: {', '.join(f'`{n}`' for n in SIGNAL_NAMES)}.")
        if st.form_submit_button("Save", type="primary"):
            issues = save_scan(CONFIG_DIR, name, enabled, min_pct, rows.to_dict("records"), int(lookback))
            if not show_errors("Not saved:", issues):
                _after_scan_save(f"Saved the {name} scan. Previous results were cleared; run the analysis again.",
                                 issues)


def _criteria_logic(c) -> None:
    """The full criterion-by-criterion logic of one scan, with its signal parameters merged into one table."""
    params: dict[str, object] = {}
    for n, rule in enumerate(c.rules, 1):
        st.markdown(f"**{n}. {rule.label}** (`{rule.check}`, weight {rule.weight:g}): {rule.summary}")
        lines = list(rule.details) + [f"**`{sig.name}`** ({sig.kind}, {sig.direction}): {sig.description}"
                                      for sig in rule.signals]
        if lines:
            st.markdown("\n".join(f"- {x}" for x in lines))
        for sig in rule.signals:
            params.update(dict(sig.params))
        if rule.table:
            st.dataframe(pd.DataFrame(rule.table, columns=["Scan", "Agree", "Oppose"]), hide_index=True)
    if params:
        st.markdown("**Signal parameters**")
        st.dataframe(pd.DataFrame([{"Parameter": p, "Value": str(v)} for p, v in params.items()]), hide_index=True)
    if c.outcome:
        st.markdown("**Result**")
        st.markdown("\n".join(f"- {x}" for x in c.outcome))


def _criteria(cfg: Config) -> None:
    st.caption("What each scan checks, built from `scans.yaml` and `settings.yaml`. **Edit criteria** validates "
               "and rewrites `scans.yaml`.")
    _flash("scans_saved")
    modified = modified_scans(cfg)
    crits = scan_criteria(cfg)
    ids = ["all"] + [c.name for c in crits]
    if st.session_state.get("criteria_focus") not in ids:
        st.session_state["criteria_focus"] = "all"
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        focus = st.selectbox("Scan", ids, key="criteria_focus", width=240,
                             format_func=lambda s: "All scans" if s == "all" else _scan_title(s))
        _restore_defaults(modified)
    with st.expander("Shared definitions"):
        for term, text in global_criteria(cfg):
            st.markdown(f"**{term}**: {text}")
    for c in crits:
        if focus != "all" and c.name != focus:
            continue
        with st.container(border=True):
            badge = "" if c.active else " :gray-badge[inactive]"
            if c.name in modified:
                badge += " :orange-badge[modified]"
            st.markdown(f"#### {_scan_title(c.name)}{badge}")
            if c.rules:
                st.caption(f"Listed at **{c.scan.min_pct:g}%** match or more · signals count on the latest "
                           f"**{c.scan.lookback_bars}** bars")
                st.markdown("  ·  ".join(scan_recipe(c.scan)))
                with st.expander("Show logic", icon=":material/account_tree:", expanded=focus == c.name):
                    _criteria_logic(c)
            else:
                st.caption("No criteria: this scan is not evaluated.")
            _scan_editor(c.name, c.scan)


# ----------------------------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------------------------


def _config_editor(cfg: Config) -> None:
    st.caption(f"Symbols and sector ETFs. Saving rewrites `universe.csv` (the only symbol file) and the benchmark "
               f"line in `settings.yaml` under `{CONFIG_DIR.resolve()}`. Nothing is written if validation finds "
               f"errors.")
    df = pd.DataFrame(universe_table(cfg), columns=COLUMNS)
    edited = st.data_editor(
        df, num_rows="dynamic", hide_index=True, key="universe_editor",
        column_config={
            "symbol": st.column_config.TextColumn("Symbol", required=True),
            "type": st.column_config.SelectboxColumn("Type", options=["stock", "etf"], default="stock",
                                                     required=True),
            "sector_etf": st.column_config.TextColumn("Sector ETF", help="Blank: name-vs-sector flow is n/a."),
            "enabled": st.column_config.CheckboxColumn("Enabled", default=True),
            "notes": st.column_config.TextColumn("Notes", help="Also used as the ticker's display name."),
        },
    )
    benchmark = st.text_input("Benchmark", value=cfg.settings.data.benchmark, width=200)
    if st.button("Save config", type="primary"):
        issues = save_edits(CONFIG_DIR, edited.to_dict("records"), benchmark)
        if show_errors("Not saved:", issues):
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


# ----------------------------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------------------------


def _header(cfg: Config) -> None:
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        st.markdown("## Technical Opportunity Scanner", width="stretch")
        as_of = st.date_input("Session date", value=date.today(), max_value=date.today(), width=160,
                              help="A non-trading day uses the prior session. Today during market hours uses a "
                                   "provisional bar.")
        refresh = st.checkbox("Force refresh", help="Bypass the cache and re-download prices and descriptions.")
        run = st.button("Run analysis", type="primary", icon=":material/play_arrow:")
    if run:
        _run(cfg, as_of, refresh)


def render() -> None:
    st.html(CSS)
    cfg = _load()
    if cfg is None:
        return
    _header(cfg)
    results, criteria, config = st.tabs([TAB_RESULTS, TAB_CRITERIA, TAB_CONFIG], key="main_tab", on_change="rerun")
    with results:
        if results.open:
            _results()
    with criteria:
        if criteria.open:
            _criteria(cfg)
    with config:
        if config.open:
            _config_editor(cfg)
