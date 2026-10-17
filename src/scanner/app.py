"""Streamlit front-end: scan everything for a date, rank symbols by % of strategy rules met, drill into a symbol and
strategy, deep-dive/export the HTML report for a subset, show and edit each strategy's technical rules (with a restore
to the preserved defaults), and edit the symbol universe.

Run from the repository root:  uv run streamlit run src/scanner/app.py
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from scanner.app_view import (
    card_rankings,
    card_tooltip,
    detail_view,
    filtered_result,
    score_color,
    split_summary,
    strategy_rows,
    ticker_description,
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
    for key in ("res", "cfg", "selected_symbol", "selected_strategy", "report_cache", "profiles", "card_symbols",
                "card_strategies"):
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
    with st.spinner("Loading ticker descriptions…"):
        st.session_state["profiles"] = fetch_profiles(sorted(res.analyses), Path(cfg.settings.data.cache_dir),
                                                      refresh=refresh)


def _description(cfg: Config, symbol: str) -> tuple[str, str] | None:
    profiles: dict[str, Profile] = st.session_state.get("profiles", {})
    return ticker_description(cfg, profiles, symbol)


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


def _cards(res: RunResult, cfg: Config) -> None:
    st.subheader("Top setups")
    st.caption("One card per symbol × strategy. Score (0–100) is that strategy's candidate score, colored red (0) → "
               "yellow (50) → green (100). Full matches come first, highest score first; combinations with no "
               "candidate show — and follow by share of rules met (PENDING_CLOSE counts as not met).")
    evaluated = sorted({ev.strategy.id for evs in res.evals.values() for ev in evs},
                       key=lambda sid: cfg.strategy(sid).name if cfg.strategy(sid) else sid)
    c1, c2 = st.columns(2)
    symbols = c1.multiselect("Filter by symbol", sorted(res.analyses), key="card_symbols",
                             placeholder="All symbols")
    strategies = c2.multiselect("Filter by strategy", evaluated, key="card_strategies", placeholder="All strategies",
                                format_func=lambda sid: cfg.strategy(sid).name if cfg.strategy(sid) else sid)
    ranked = card_rankings(res, symbols, strategies)
    if not ranked:
        st.info("No cards match the filters." if symbols or strategies else "No symbols with evaluated strategies.")
        return
    grid = st.container()  # filled after the slider below it is read
    n = len(ranked) if len(ranked) <= 1 else st.slider("Cards", 1, len(ranked), min(10, len(ranked)))
    symbol = st.session_state.get("selected_symbol")
    selected = (symbol, _picked_strategy(res, symbol)) if symbol in res.analyses else None
    shown = ranked[:n]
    for start in range(0, len(shown), CARDS_PER_ROW):
        cols = grid.columns(CARDS_PER_ROW)
        for col, (sym, r) in zip(cols, shown[start:start + CARDS_PER_ROW], strict=False):
            sid = r.ev.strategy.id
            with col.container(border=True):
                st.markdown(f"**{sym.symbol}** · {sym.close:,.2f} "
                            f":{'green' if sym.change_pct >= 0 else 'red'}[{sym.change_pct:+.2f}%]",
                            help=card_tooltip(_description(cfg, sym.symbol)))
                st.markdown(f"**{r.ev.strategy.name}**")
                if r.cand:
                    st.markdown(f"<span style='font-size:2rem;font-weight:600;color:{score_color(r.cand.score)}'>"
                                f"{r.cand.score:.1f}</span>", unsafe_allow_html=True)
                else:
                    st.markdown("<span style='font-size:2rem;font-weight:600;opacity:.5'>—</span>",
                                unsafe_allow_html=True,
                                help="This strategy does not fully match, so there is no candidate score.")
                st.caption(f"{r.met}/{r.total} rules met · {r.ev.strategy.bias} · {sym.trend}"
                           f"{' · ⏳ pending' if r.pending else ''}")
                if st.button("View", key=f"card_{sym.symbol}_{sid}", width="stretch",
                             type="primary" if selected == (sym.symbol, sid) else "secondary"):
                    st.session_state["selected_symbol"] = sym.symbol
                    st.session_state["selected_strategy"] = sid
                    st.session_state.pop(f"strategies_{sym.symbol}", None)  # the card's pick beats a stale row pick
                    st.rerun()


def _picked_strategy(res: RunResult, symbol: str) -> str | None:
    """The strategy picked in the table below the details (read from its state, since it renders later), else the
    one from the clicked card, else the best match."""
    rows = strategy_rows(res, symbol)
    state = st.session_state.get(f"strategies_{symbol}") or {}
    picked = (state.get("selection") or {}).get("rows") or []
    if picked and picked[0] < len(rows):
        return rows[picked[0]].ev.strategy.id
    card = st.session_state.get("selected_strategy")
    if any(r.ev.strategy.id == card for r in rows):
        return card
    return rows[0].ev.strategy.id if rows else None


def _strategies(res: RunResult, symbol: str) -> None:
    rows = strategy_rows(res, symbol)
    st.subheader(f"Strategies for {symbol}")
    st.caption("Click a row to show that strategy above. The best match is shown by default.")
    df = pd.DataFrame([{
        "Strategy": r.ev.strategy.name, "Bias": r.ev.strategy.bias, "% met": r.fraction * 100,
        "Rules met": f"{r.met}/{r.total}", "Pending": r.pending or None, "Flow": r.ev.flow_label,
        "Score": r.cand.score if r.cand else None,
    } for r in rows])
    st.dataframe(
        df, hide_index=True, on_select="rerun", selection_mode="single-row", key=f"strategies_{symbol}",
        column_config={"% met": st.column_config.ProgressColumn("% met", min_value=0, max_value=100, format="%.0f%%"),
                       "Score": st.column_config.NumberColumn("Score", format="%.1f")},
    )


def _details(res: RunResult, cfg: Config, symbol: str, strategy_id: str) -> None:
    d = detail_view(res, cfg, symbol, strategy_id)
    st_, row = d["strategy"], d["row"]
    st.subheader(f"{symbol} × {st_.name}")
    if desc := _description(cfg, symbol):
        name, summary = desc
        head, rest = split_summary(summary)
        st.caption(f"**{name}** · {head}" if name and head else (f"**{name}**" if name else head))
        if rest:
            with st.expander(f"About {symbol}"):
                st.write(rest)
    st.caption(f"{row.met}/{row.total} rules met · bias {st_.bias} · flow {d['flow_label']} · close {d['close']:,.2f}"
               f" · ATR {d['atr']:.2f} · RSI {d['rsi']:.1f}" + (f" · score {row.cand.score:.1f}" if row.cand else ""))
    if st_.description:
        st.write(st_.description)
    left, right = st.columns([3, 2])
    with left:
        st.plotly_chart(d["chart"], width="stretch", theme="streamlit", key=f"chart_{symbol}_{strategy_id}")
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
    _cards(res, cfg)
    symbol = st.session_state.get("selected_symbol")
    if symbol in res.analyses:
        strategy_id = _picked_strategy(res, symbol)
        if strategy_id:
            st.divider()
            _details(res, cfg, symbol, strategy_id)
        st.divider()
        _strategies(res, symbol)
    st.divider()
    _deep_dive(res, cfg)


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
            if any(i.severity == "error" for i in issues):
                st.error("Not restored:\n\n" + "\n".join(f"- {i}" for i in issues if i.severity == "error"))
            else:
                _after_strategy_save("Strategies restored to defaults. Previous results were cleared.", issues)


def _strategy_editor(stg: Strategy) -> None:
    rev = st.session_state.get("strategy_rev", 0)
    vals = technical_values(stg)
    with st.expander("Edit rules", icon=":material/edit:"), st.form(f"edit_{stg.id}_{rev}", border=False):
        enabled = st.toggle("Enabled", value=stg.enabled,
                            help="A strategy with status `stub` stays inactive even when enabled.")
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
            if any(i.severity == "error" for i in issues):
                st.error("Not saved:\n\n" + "\n".join(f"- {i}" for i in issues if i.severity == "error"))
            else:
                _after_strategy_save(f"Saved {stg.name}. Previous results were cleared; run the analysis again.",
                                     issues)


def _criteria(cfg: Config) -> None:
    st.subheader("Scan criteria")
    st.caption("Exactly what the scanner checks for each strategy, built from `strategies.yaml` and `settings.yaml` "
               "(per-strategy overrides applied). Rule names match the rule list in Results. **Edit rules** rewrites "
               "only that strategy's lines in `strategies.yaml`.")
    _flash("strategy_saved")
    modified = modified_strategies(cfg)
    left, right = st.columns([4, 1])
    with left.expander("Shared definitions"):
        for term, text in global_criteria(cfg):
            st.markdown(f"**{term}**: {text}")
    with right:
        _restore_defaults(modified)
    show_inactive = st.toggle("Show inactive strategies", value=False)
    for c in strategy_criteria(cfg):
        if not c.active and not show_inactive:
            continue
        stg = c.strategy
        with st.container(border=True):
            badge = "" if c.active else " :gray-badge[inactive]"
            if stg.id in modified:
                badge += f" :orange-badge[{modified[stg.id]}]"
            st.markdown(f"#### {stg.name}{badge}")
            st.caption(f"`{stg.id}` · bias **{stg.bias}** · status {stg.status}")
            if stg.description:
                st.write(stg.description)
            _strategy_editor(stg)
            if c.overrides:
                st.info("Overrides for this strategy: " + ", ".join(f"`{k}` = {v}" for k, v in c.overrides.items()))
            if not c.rules:
                st.caption("No technical rules: the scanner does not evaluate this strategy.")
            for n, rule in enumerate(c.rules, 1):
                st.markdown(f"**{n}. `{rule.rule}`**: {rule.summary}")
                for line in rule.details:
                    st.markdown(f"- {line}")
                for sig in rule.signals:
                    st.markdown(f"- **`{sig.name}`** ({sig.kind}, {sig.direction}): {sig.description}")
                if rule.signals:
                    params = {p: v for sig in rule.signals for p, v in sig.params}
                    st.dataframe(pd.DataFrame([{"Parameter": p, "Value": str(v)} for p, v in params.items()]),
                                 hide_index=True)
                if rule.table:
                    st.dataframe(pd.DataFrame(rule.table, columns=["Bias", "Agree", "Oppose"]), hide_index=True)
            if c.outcome:
                st.markdown("**Result**")
                st.markdown("\n".join(f"- {x}" for x in c.outcome))
            with st.expander("Display-only (not evaluated by the scanner)"):
                for title, items in (("Construction", stg.construction), ("ORATS checklist", stg.orats_checklist),
                                     ("Other manual checks", stg.other_manual_checks)):
                    if items:
                        st.markdown(f"**{title}**\n" + "\n".join(f"- {x}" for x in items))
                if stg.plan:
                    st.markdown("**Plan**\n" + "\n".join(
                        f"- {k}: " + ("; ".join(map(str, v)) if isinstance(v, list) else str(v))
                        for k, v in stg.plan.items()))


# ----------------------------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------------------------


def _config_editor(cfg: Config) -> None:
    st.subheader("Symbols and sector ETFs")
    st.caption(f"Saving rewrites `universe.csv` (the only symbol file) and the benchmark line in `settings.yaml`, "
               f"under `{CONFIG_DIR.resolve()}`. Nothing is written if validation finds errors.")
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
    results, criteria, config = st.tabs(["Results", "Criteria", "Config"])
    with results:
        _results()
    with criteria:
        _criteria(cfg)
    with config:
        _config_editor(cfg)


main()
