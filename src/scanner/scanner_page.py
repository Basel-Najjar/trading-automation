"""Scanner page of the Streamlit app. It scans everything for a date, then presents the result from high level to
detail:

1. Overview band: session, benchmark, counts, watch list, skipped symbols and a sector strip that filters the list.
2. Candidates: tickers split into Bullish / Bearish / Neutral by their matched setups (a switcher picks the
   group), with filters. Every active strategy is still evaluated against every ticker.
3. Setup pane beside the list: direction, setups summarised by direction, chart, strategy switcher, key numbers,
   one line per rule and a price ladder.
4. Evidence tabs in the pane (why, levels, about), plus a link to the strategy's criteria.

Other tabs: the scan criteria (with rule editing) and the symbol universe.

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
    STATUS_ICON,
    SymbolRow,
    card_tooltip,
    change_pct,
    detail_view,
    direction,
    direction_groups,
    direction_setups,
    humanize,
    overview,
    pane_rows,
    setup_summary,
    strategy_recipe,
    symbol_chart,
    symbol_rankings,
    ticker_description,
    trend_text,
)
from scanner.config import Config, Strategy, load_config
from scanner.config_edit import (
    ALIGNMENT_CHOICES,
    COLUMNS,
    DEFAULT_STRATEGIES,
    NOT_EXTENDED_CHOICES,
    STRATEGIES,
    TREND_CHOICES,
    modified_strategies,
    restore_default_strategies,
    save_edits,
    save_strategy,
    technical_values,
    universe_table,
)
from scanner.criteria import global_criteria, strategy_criteria
from scanner.data import Profile, fetch_profiles
from scanner.models import SIGNAL_NAMES, Tri
from scanner.pipeline import RunResult, run_scan
from scanner.ui import picked_rows, show_errors, table_height

CONFIG_DIR = Path(os.environ.get("SCANNER_CONFIG_DIR", "config"))
TABLE_ROWS = 18  # visible rows in the opportunity list before it scrolls
TAB_RESULTS, TAB_CRITERIA, TAB_CONFIG = "Results", "Criteria", "Config"
DIRECTION_ICON = {"Bullish": "▲", "Bearish": "▼", "Neutral": "↔"}
DIRECTION_BADGE = {"Bullish": ":green-badge[▲ Bullish]", "Bearish": ":red-badge[▼ Bearish]",
                   "Neutral": ":gray-badge[↔ Neutral]"}
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
                str(key).startswith(("strat_", "opps_", "_pick_", "watch_table")):
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
    st.session_state["charts"] = {}  # figures belong to a run
    st.session_state["cfg"] = cfg
    with st.spinner("Loading ticker descriptions…"):
        st.session_state["profiles"] = fetch_profiles(sorted(res.analyses), Path(cfg.settings.data.cache_dir),
                                                      refresh=refresh)


def _description(cfg: Config, symbol: str) -> tuple[str, str] | None:
    profiles: dict[str, Profile] = st.session_state.get("profiles", {})
    return ticker_description(cfg, profiles, symbol)


def _name(cfg: Config, sid: str) -> str:
    stg = cfg.strategy(sid)
    return stg.name if stg else sid


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


def _select(symbol: str, strategy_id: str | None = None, clear: str | None = None) -> None:
    """Show `symbol` (and `strategy_id`, else its best match) in the setup pane. `clear` is a table key whose
    row selection is dropped, so picking the same row there again counts as a new pick."""
    st.session_state["selected_symbol"] = symbol
    if strategy_id:
        st.session_state[f"strat_{symbol}"] = strategy_id
    else:
        st.session_state.pop(f"strat_{symbol}", None)
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


def _to_criteria(strategy_id: str) -> None:
    st.session_state["criteria_focus"] = strategy_id
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
        dirs = ov["directions"]
        counts.markdown(" · ".join(f"{DIRECTION_ICON[g]} **{n}** {g.lower()}" for g, n in dirs.items())
                        + f" of {ov['n_symbols']} symbols",
                        help=f"{ov['n_candidates']} matched setups across every active strategy. A ticker is "
                             "bullish or bearish when its matches are on one side, neutral when they are neutral "
                             "or on both sides.")
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
                     help="Sector ETF · trend · 1-month return · symbols with a setup. Pick to filter the list.")


def _watch_rows(ov: dict) -> tuple[list[dict], list[tuple[str, str | None]]]:
    data = [{"Symbol": w.symbol, "Near": humanize(w.near.signal), "ATR away": w.near.distance_atr,
             "Strategies": ", ".join(name for _, name in w.strategies)} for w in ov["watch"]]
    return data, [(w.symbol, w.strategies[0][0] if w.strategies else None) for w in ov["watch"]]


def _watch_popover(ov: dict) -> None:
    if not ov["watch"]:
        return
    with st.popover(f"Watch list · {len(ov['watch'])}", type="tertiary", icon=":material/visibility:",
                    help="Close to a setup: a signal is near but has not fired yet."):
        data, _ = _watch_rows(ov)
        st.caption("Pick a row to open it.")
        st.dataframe(pd.DataFrame(data), hide_index=True, on_select="rerun", selection_mode="single-row",
                     key="watch_table", width=640,
                     column_config={"ATR away": st.column_config.NumberColumn(format="%.2f")})


# ----------------------------------------------------------------------------------------------
# Results: opportunities
# ----------------------------------------------------------------------------------------------


def _filters(res: RunResult, cfg: Config) -> dict:
    evaluated = sorted({ev.strategy.id for evs in res.evals.values() for ev in evs}, key=lambda s: _name(cfg, s))
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        strategies = st.multiselect("Strategy", evaluated, key="f_strategy", placeholder="All strategies",
                                    format_func=lambda s: _name(cfg, s), width=300)
        symbols = st.multiselect("Symbol", sorted(res.analyses), key="f_symbol", placeholder="All symbols",
                                 width=220)
    return {"strategy_ids": strategies, "symbols": symbols, "sectors": st.session_state.get("f_sector") or []}


def _direction_pick(groups: dict[str, list[SymbolRow]]) -> str:
    """The Bullish / Bearish / Neutral switcher above the list. Defaults to the first group with tickers."""
    if st.session_state.get("f_direction") not in groups:
        st.session_state["f_direction"] = next((g for g, rows in groups.items() if rows), next(iter(groups)))
    return st.segmented_control(
        "Direction", list(groups), key="f_direction", required=True, label_visibility="collapsed",
        format_func=lambda g: f"{DIRECTION_ICON[g]} {g} · {len(groups[g])}") or st.session_state["f_direction"]


def _list_rows(rows: list[SymbolRow], group: str) -> tuple[list[dict], list[tuple[str, str | None]]]:
    """Table rows for one direction group and, per row, the (symbol, strategy) a pick opens."""
    data, picks = [], []
    for s in rows:
        setups = direction_setups(s, group)
        best = setups[0]
        data.append({"Symbol": s.symbol, "Chg %": s.change_pct, "Best setup": best.ev.strategy.name,
                     "Score": best.score, "Also matches": ", ".join(r.ev.strategy.name for r in setups[1:])})
        picks.append((s.symbol, best.ev.strategy.id))
    return data, picks


def _table_key(f: dict, group: str) -> str:
    sig = repr(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in f.items())) + group
    return "opps_" + hashlib.md5(sig.encode()).hexdigest()[:10]


def _opportunity_table(data: list[dict], picks: list[tuple[str, str | None]], group: str, key: str,
                       selected: str | None) -> None:
    if not data:
        st.info(f"No {group.lower()} candidates with these filters. The watch list shows setups that are close.")
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
            "Best setup": st.column_config.TextColumn(width=150),
            "Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f", width=84,
                                                     help="Score (0–100) of the best setup in this direction."),
            "Also matches": st.column_config.TextColumn(help="Other strategies in this direction that also match."),
        },
    )
    st.caption(f"{len(df)} tickers · pick one to open it")


# ----------------------------------------------------------------------------------------------
# Results: setup pane
# ----------------------------------------------------------------------------------------------


def _strategy_label(r) -> str:
    if r.cand:
        return f"{r.ev.strategy.name} · {r.cand.score:.0f}"
    return f"{r.ev.strategy.name} · {r.met}/{r.total}"


def _setup_summary_md(row: SymbolRow) -> str:
    """One line with the ticker's matched setups by direction, e.g. "**▲ Bullish** Wheel 65 · **▼ Bearish** —"."""
    parts = []
    for g, rows in setup_summary(row).items():
        names = ", ".join(f"{r.ev.strategy.name} {r.score:.0f}" for r in rows) or "—"
        parts.append(f"**{DIRECTION_ICON[g]} {g}** {names}")
    return " · ".join(parts)


def _ladder(d: dict) -> None:
    st.markdown("**Price ladder**" + ("" if d["row"].cand else " :orange-badge[no full match]"),
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
    st.markdown("**Rules**")
    for line in d["rules"]:
        st.markdown(f"{STATUS_MD[line.status]} **{line.label}**: {line.summary}")


def _evidence_tabs(d: dict, cfg: Config, symbol: str, sid: str) -> None:
    why, levels, about = st.tabs(["Why", "Levels", "About"])
    with why:
        if d["score"]:
            total = sum(x[3] for x in d["score"])
            st.markdown(f"**Score breakdown** · {total:.1f} points")
            st.dataframe(pd.DataFrame([{"Component": humanize(n), "Fit": raw * 100, "Points": pts, "of": w,
                                        "Note": note} for n, raw, w, pts, note in d["score"]]), hide_index=True,
                         column_config={"Fit": st.column_config.ProgressColumn(min_value=0, max_value=100,
                                                                               format="%.0f%%"),
                                        "Points": st.column_config.NumberColumn(format="%.1f"),
                                        "of": st.column_config.NumberColumn(format="%.0f", width=40)})
        if d["hits"]:
            st.markdown("**Signals that fired**")
            st.dataframe(pd.DataFrame(d["hits"]), hide_index=True,
                         column_config={"Price": st.column_config.NumberColumn(format="%.2f"),
                                        "Bars ago": st.column_config.NumberColumn(width=70)})
        st.markdown(f"**Trend**: {d['trend_reason']}")
        if d["flow"]:
            st.markdown("**Flow**: " + " · ".join(f"{k} {humanize(v).lower()}" for k, v in d["flow"]))
        with st.expander("Raw rule evidence"):
            for line in d["rules"]:
                st.markdown(f"{STATUS_ICON[line.status]} `{line.rule}`: {line.detail}")
                if line.evidence:
                    st.caption("  \n".join(line.evidence))
        st.button("How this strategy is scanned", icon=":material/arrow_forward:", type="tertiary",
                  key=f"why_crit_{symbol}_{sid}", on_click=_to_criteria, args=(sid,))
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


def _setup(res: RunResult, cfg: Config, srow: SymbolRow | None, symbol: str) -> None:
    if srow is None:
        st.info(f"No strategies were evaluated for {symbol}.")
        return
    rows = pane_rows(srow)
    key = f"strat_{symbol}"
    ids = [r.ev.strategy.id for r in rows]
    if st.session_state.get(key) not in ids:
        st.session_state[key] = ids[0]
    by_id = {r.ev.strategy.id: r for r in rows}

    desc = _description(cfg, symbol)
    sa = res.analyses[symbol]
    close = float(sa.bars.close[sa.last_idx])
    d_group = direction(srow)
    badge = DIRECTION_BADGE[d_group] if d_group else ":gray-badge[no match]"
    st.markdown(f"### {symbol} <span class='scn-muted'>{desc[0] if desc else ''}</span>",
                unsafe_allow_html=True, help=card_tooltip(desc))
    st.markdown(f"{badge} {_setup_summary_md(srow)}",
                help="Matched setups by direction (score). Every active strategy is evaluated; pick one below "
                     "to see its rules.")
    st.markdown(f"**{close:,.2f}** {_pct_md(change_pct(sa), scale=1, nd=2)} · RSI "
                f"{float(sa.bars.rsi[sa.last_idx]):.1f} · ATR {float(sa.bars.atr[sa.last_idx]):.2f}"
                + (f" · sector {sa.sector_etf}" if sa.sector_etf else ""))
    # one chart per symbol, above the strategy pills: switching strategy re-renders only what follows.
    # Overlays (sector ETF, benchmark, ratio) toggle inside the chart, so they never rerun the app either.
    charts = st.session_state.setdefault("charts", {})
    if symbol not in charts:
        charts[symbol] = symbol_chart(res, symbol)
    st.plotly_chart(charts[symbol], width="stretch", theme="streamlit", key=f"chart_{symbol}",
                    config={"displaylogo": False})
    st.pills("Strategy", ids, key=key, format_func=lambda s: _strategy_label(by_id[s]),
             label_visibility="collapsed", required=True, wrap=True)
    sid = st.session_state.get(key) or ids[0]
    d = detail_view(res, cfg, symbol, sid)
    row, stg = d["row"], d["strategy"]

    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(f"**{stg.name}** · {humanize(stg.bias)}" + (f"  \n{stg.description}" if stg.description else ""),
                    width="stretch")
        st.button("Criteria", icon=":material/rule:", type="tertiary", key=f"crit_{symbol}_{sid}",
                  on_click=_to_criteria, args=(sid,), help="How this strategy is scanned")

    m = st.columns(4)
    m[0].metric("Score", f"{row.cand.score:.1f}" if row.cand else "—", border=True,
                help="Candidate score (0–100) for this strategy." if row.cand
                else "No full match, so no candidate score.")
    m[1].metric("Rules met", f"{row.met}/{row.total}", border=True)
    m[2].metric("Trend", trend_text(d["trend"]), border=True)
    m[3].metric("Flow", humanize(d["flow_label"]), border=True)

    left, right = st.columns([3, 2], gap="medium")
    with left:
        _rules(d)
    with right:
        _ladder(d)
    _evidence_tabs(d, cfg, symbol, sid)


def _results() -> None:
    res: RunResult | None = st.session_state.get("res")
    cfg: Config | None = st.session_state.get("cfg")
    if res is None or cfg is None:
        st.info("Pick a session date above and press **Run analysis**. Every run scans all enabled symbols and "
                "active strategies; filtering happens afterwards.", icon=":material/play_circle:")
        return
    rankings = symbol_rankings(res)  # once per rerun: the overview, the list and the pane all use it
    ov = overview(res, cfg, rankings)
    _apply_table_pick("watch_table", _watch_rows(ov)[1])
    _overview(ov)

    f = _filters(res, cfg)
    groups = direction_groups(res, f["symbols"], f["strategy_ids"], f["sectors"], rankings)
    left, right = st.columns([5, 7], gap="large")
    with left, st.container(key="opps_list"):
        group = _direction_pick(groups)
        data, picks = _list_rows(groups[group], group)
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
            _setup(res, cfg, next((r for r in rankings if r.symbol == selected), None), selected)
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


def _after_strategy_save(text: str, issues: list) -> None:
    _clear_results()
    st.session_state["strategy_rev"] = st.session_state.get("strategy_rev", 0) + 1  # re-seed the edit widgets
    st.session_state["strategy_saved"] = (text, [str(i) for i in issues])
    st.rerun()


def _restore_defaults(modified: dict[str, str]) -> None:
    has_default = (CONFIG_DIR / DEFAULT_STRATEGIES).exists()
    with st.popover("Restore defaults", icon=":material/restore:", disabled=not has_default,
                    help=None if has_default else f"`{DEFAULT_STRATEGIES}` not found in the config directory."):
        changed = ", ".join(f"`{sid}`" for sid in modified) or "none"
        st.markdown(f"Replace `{STRATEGIES}` with the preserved original `{DEFAULT_STRATEGIES}`. Strategies that "
                    f"differ from it: {changed}.")
        if st.button("Restore all strategies", type="primary"):
            issues = restore_default_strategies(CONFIG_DIR)
            if not show_errors("Not restored:", issues):
                _after_strategy_save("Strategies restored to defaults. Previous results were cleared.", issues)


def _strategy_editor(stg: Strategy) -> None:
    rev = st.session_state.get("strategy_rev", 0)
    vals = technical_values(stg)
    with st.expander("Edit rules", icon=":material/edit:"), st.form(f"edit_{stg.id}_{rev}", border=False):
        enabled = st.toggle("Enabled", value=stg.enabled)
        trend = st.multiselect("trend", TREND_CHOICES, default=vals["trend"],
                               help="Allowed trend states. `any` cannot be combined with others; empty means any.")
        signals_all = st.multiselect("signals_all", SIGNAL_NAMES, default=vals["signals_all"],
                                     help="Every listed signal must fire.")
        signals_any = st.multiselect("signals_any", SIGNAL_NAMES, default=vals["signals_any"],
                                     help="At least one listed signal must fire. Empty: no requirement.")
        c1, c2 = st.columns(2)
        not_extended = c1.selectbox("not_extended", NOT_EXTENDED_CHOICES,
                                    index=NOT_EXTENDED_CHOICES.index(vals["not_extended"]),
                                    help="`auto` follows the bias: bullish → up, bearish → down, others → none.")
        alignment = c2.selectbox("alignment", ALIGNMENT_CHOICES, index=ALIGNMENT_CHOICES.index(vals["alignment"]),
                                 help="`soft` labels and scores, `required` drops countertrend, `ignore` skips.")
        if st.form_submit_button("Save", type="primary"):
            new = {"trend": trend, "signals_all": signals_all, "signals_any": signals_any,
                   "not_extended": not_extended, "alignment": alignment}
            issues = save_strategy(CONFIG_DIR, stg.id, new, enabled)
            if not show_errors("Not saved:", issues):
                _after_strategy_save(f"Saved {stg.name}. Previous results were cleared; run the analysis again.",
                                     issues)


def _criteria_logic(c) -> None:
    """The full rule-by-rule logic of one strategy, with its signal parameters merged into one table."""
    params: dict[str, object] = {}
    for n, rule in enumerate(c.rules, 1):
        st.markdown(f"**{n}. {humanize(rule.rule)}** (`{rule.rule}`): {rule.summary}")
        lines = list(rule.details) + [f"**`{sig.name}`** ({sig.kind}, {sig.direction}): {sig.description}"
                                      for sig in rule.signals]
        if lines:
            st.markdown("\n".join(f"- {x}" for x in lines))
        for sig in rule.signals:
            params.update(dict(sig.params))
        if rule.table:
            st.dataframe(pd.DataFrame(rule.table, columns=["Bias", "Agree", "Oppose"]), hide_index=True)
    if params:
        st.markdown("**Signal parameters**")
        st.dataframe(pd.DataFrame([{"Parameter": p, "Value": str(v)} for p, v in params.items()]), hide_index=True)
    if c.outcome:
        st.markdown("**Result**")
        st.markdown("\n".join(f"- {x}" for x in c.outcome))


def _criteria(cfg: Config) -> None:
    st.caption("What the scanner checks for each strategy, built from `strategies.yaml` and `settings.yaml` "
               "(per-strategy overrides applied). **Edit rules** rewrites only that strategy's lines.")
    _flash("strategy_saved")
    modified = modified_strategies(cfg)
    crits = strategy_criteria(cfg)
    ids = ["all"] + [c.strategy.id for c in crits]
    if st.session_state.get("criteria_focus") not in ids:
        st.session_state["criteria_focus"] = "all"
    with st.container(horizontal=True, vertical_alignment="bottom", gap="medium"):
        focus = st.selectbox("Strategy", ids, key="criteria_focus", width=320,
                             format_func=lambda s: "All strategies" if s == "all" else _name(cfg, s))
        show_inactive = st.toggle("Show inactive strategies", value=False)
        _restore_defaults(modified)
    with st.expander("Shared definitions"):
        for term, text in global_criteria(cfg):
            st.markdown(f"**{term}**: {text}")
    for c in crits:
        stg = c.strategy
        if focus != "all" and stg.id != focus:
            continue
        if focus == "all" and not c.active and not show_inactive:
            continue
        with st.container(border=True):
            badge = "" if c.active else " :gray-badge[inactive]"
            if stg.id in modified:
                badge += f" :orange-badge[{modified[stg.id]}]"
            st.markdown(f"#### {stg.name}{badge}")
            st.caption(f"`{stg.id}` · {humanize(stg.bias)}"
                       + (f" · {stg.description}" if stg.description else ""))
            if c.rules:
                st.markdown("  ·  ".join(f"**{term}** {value}" for term, value in strategy_recipe(stg)))
            else:
                st.caption("No technical rules: the scanner does not evaluate this strategy.")
            if c.overrides:
                st.caption("Overrides: " + ", ".join(f"`{k}` = {v}" for k, v in c.overrides.items()))
            if c.rules:
                with st.expander("Show logic", icon=":material/account_tree:", expanded=focus == stg.id):
                    _criteria_logic(c)
            _strategy_editor(stg)


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
