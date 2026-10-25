# Technical Opportunity Scanner (v2)

A daily-bar technical scanner for a personal watchlist. It finds support/resistance holds and breaks, trend and range
states, and RSI extension, using the rules in [requirements.md](requirements.md). Five scans, from **bullish**
through **non-bearish**, **neutral** and **non-bullish** to **bearish**, each with configurable criteria and its own
lookback window in `config/scans.yaml`, rank tickers by the weighted % of their criteria met. A Streamlit app shows the lists with charts, a criterion checklist and copy-ready TradingView
alert prices. The app is the only interface: there is no CLI, and nothing is written to files except the price cache
and the config you edit.

The scanner is the **discovery layer only**. It never touches ORATS, option chains, fundamentals, earnings or macro data,
and the config holds technical parameters only. Data is free daily OHLCV from `yfinance`.

## Setup

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run streamlit run src/scanner/app.py      # run from the repository root (reads .streamlit/config.toml)
```

The app validates `config/` when it loads and before every save; an invalid file shows its errors on the page.

## Files you maintain (`config/`)

| File | Purpose |
|---|---|
| `universe.csv` | The only symbol file. Columns: `symbol` (required), `type` (`stock`/`etf`), `sector_etf`, `enabled`, `notes`. A blank `sector_etf` shows name-vs-sector as `n/a`. `universe.txt` and `sector_map.csv` are no longer read; if either exists, validation fails with a message to merge it into `universe.csv`. |
| `scans.yaml` | The five scans: criteria, weights, minimum match % and lookback (how many recent bars a signal may come from). See [docs/editing-scans.md](docs/editing-scans.md). |
| `scans.default.yaml` | The preserved original scans. The app's **Restore defaults** copies it over `scans.yaml`. Never read by the scanner. |
| `settings.yaml` | All technical parameters, with defaults. Keys marked `[added]` are not in the spec's §6.9 (see Assumptions). |
| `manual_levels.yaml` | Optional owner-drawn levels (see `manual_levels.example.yaml`). |
| `markets.yaml` | Lists for the app's **Markets** page, one per tool (`levels`, `rrg` with its `benchmark`, `pca`). Each holds tickers or `NUM/DEN` ratios. Also holds per-resolution parameters. Independent of `universe.csv`. |

The signal vocabulary and its exact definitions are in [docs/signals.md](docs/signals.md).

## The app

The app has two pages, **Scanner** and **Markets**, in the top navigation. On the Scanner page, the Results tab goes
from high level to detail: overview, then the list, then one setup, then its evidence.

- **Run.** At the top, pick a session date (default today), optionally tick **Force refresh**, then press **Run
  analysis**. Every run evaluates all enabled symbols in every enabled scan. All filtering happens after the scan.
- **Overview band.** It shows the session (with a *prior session* or *provisional bar* badge), the benchmark's trend and
  1m/3m returns, and how many tickers each scan lists. **Watch list** opens the near-setups; pick a row to open that symbol.
  **Skipped / warnings** lists what was left out. The **Sector** pills show each sector ETF's trend, 1-month return
  and number of symbols listed in a scan. Picking one filters the list.
- **Five lists, most bullish to most bearish.** ▲ Bullish (an uptrend and/or a 2-bar close through resistance, not
  extended), △ Non-bearish (horizontal support or a rising line held), ↔ Neutral (a horizontal channel), ▽
  Non-bullish (horizontal resistance or a falling line held) and ▼ Bearish (the mirror of bullish). Each scan has its
  own criteria (trend, signals, RSI, flow, channel), each with a weight, and its own lookback: a signal on any of its
  latest `lookback_bars` bars counts (10 bars ≈ two weeks for the middle three). A ticker's **match %** for a scan is
  the weight of the criteria it meets ÷ the total weight. It is listed in every scan whose minimum match (`min_pct`)
  it reaches, so a ticker can be in more than one list. Tickers below every minimum are not listed; the watch list
  covers the near ones.
- **The list.** A switcher picks the scan (with its ticker count). The list has one row per ticker: its change, match
  %, criteria met, trend, and the other scans that also list it, highest match first. Pick a row to open it in the
  setup pane beside the list, which stays in view while you scroll. **Symbol** and the sector pills filter the list.
- **Ticker descriptions.** The name is your `notes` from `universe.csv`, or else the yfinance name. The summary is
  the yfinance business summary. Both are display only and cached in `cache/profiles.json` for 30 days; **Force
  refresh** refetches them.
- **Setup pane.** The header shows the symbol, name, a badge per scan that lists it, then the close, change, RSI, ATR
  and sector. Below it is one Plotly
  chart per symbol, opening on the last 60 bars, with the signal levels and markers of all its scans. You can
  zoom, pan back through the full history, and use **Legend** to list or hide levels. Buttons beside it overlay
  the symbol's sector ETF, the benchmark and the ETF/benchmark ratio (the series sector flow is read from), each
  on its own right-hand axis in its own colour; they toggle inside the chart, so they never reload it or reset the
  zoom. Scan pills under the chart switch between the five scans, each with the ticker's match % (✓ when listed).
  Switching leaves the chart and its zoom as they are and updates only what follows: match %, criteria met, trend
  and flow, a ✓/✗/⏳ checklist with one plain line per criterion (and its weight), and the **price ladder**, which
  lists alert prices (including invalidation) and the last close, highest first. Without a fired signal it shows the
  trigger prices of near signals, or the nearest zones if nothing is close. **Criteria** jumps to the scan's logic.
- **Evidence tabs.** **Why** has the signals that fired, the trend and flow reasoning, and the raw criterion
  evidence. **Levels** has the nearest levels. **About** has the full business summary.
- **Criteria tab.** Each scan shows its minimum match and a one-line recipe (one label per criterion, with its
  weight). **Show logic** has the exact logic, built from `scans.yaml`, `settings.yaml` and the signal registry, with
  its signal parameters in one table. The **Scan** picker shows one scan; the setup pane's **Criteria** button opens
  it there. **Edit criteria** sets `enabled`, the minimum match %, the lookback in bars and the criteria (check,
  value, weight, an optional per-criterion **Within** lookback, label) in a table. Saving validates first and then rewrites `scans.yaml`. Scans that differ from `scans.default.yaml` show a
  **modified** badge, and **Restore defaults** puts the original back. Saving or restoring clears the current
  results.
- **Config tab.** Edit symbols, type, sector ETF, enabled, notes and the benchmark. **Save config** validates the
  edit first and writes nothing if there are errors. It then rewrites `universe.csv` and the `benchmark:` line in
  `settings.yaml`.
- **Another config directory.** Set `SCANNER_CONFIG_DIR` to point the app at it.

### Markets page

This is a weekly market-structure view. It is separate from the scanner: it has its own lists (`config/markets.yaml`)
and its own **Refresh prices** button, and it never runs or clears a scan. It shares the Parquet cache, so symbols in
both are downloaded once. Opening the page reads only the cache. One refresh covers the longest history any resolution
needs (8 years by default), so switching resolution never downloads.

- **Resolution.** **Daily (1Y)** or **Weekly (5Y)**. Weekly bars are built from daily bars, with weeks ending Friday.
  Each bar is dated by its last session, so a holiday Friday's week is dated Thursday. A week still in progress is
  drawn faded, and its signals are *pending*, like the scanner's provisional bar. Each resolution analyses more history
  than it shows, to warm up SMA200, ATR and pivots. The weekly `settings:` block scales the bar-count parameters
  (pivot window, recency half-life, trendline lookback). Everything else comes from `settings.yaml`.
- **Structure.** Each `levels` series runs through the scanner's own engine: trend state (B1), SMA/horizontal/
  diagonal levels, B5 2-bar breaks, holds and engulfing patterns. The table shows trend, nearest support and
  resistance (and their distance in ATRs), the latest fired signal and the closest near-signal. *1 of 2 closes* means
  one close beyond the zone, with the confirming close still due. Pick a row to see the scanner's chart for it.
  Ratios use body-only candles: open and close are exact ratios, and high/low are the larger/smaller of the two.
- **Rotation (RRG).** Each member is plotted against the benchmark using the standard z-score approximation of
  JdK's proprietary formulas. RS-Ratio = 100 + z-score of price/benchmark over N bars. RS-Momentum = 100 + z-score
  of RS-Ratio's rate of change. Quadrants are Leading, Weakening, Lagging and Improving. The table gives each
  member's quadrant now and at the start of the tail, its heading and its speed. A slider shortens the tail.
- **Correlation (PCA).** This is a rolling PCA of the correlation matrix of log returns (a ratio's return is the
  difference of its legs'). It shows PC1..k's share of variance over time (the top of the stack is the absorption
  ratio, which rises when everything moves together), the average pairwise correlation, loadings now vs one window
  earlier, and correlation heatmaps now and changed. Loading signs are aligned window to window, so a sign flip is not
  read as a shift.
- **Lists.** Edit the three lists and the RRG benchmark. Saving validates first, writes nothing on errors, and keeps
  the `resolutions:` block as written. Press **Refresh prices** after adding tickers.

## Reading the results

- **Match %** is the weight of a scan's criteria met ÷ its total weight; the checklist in the setup pane shows each
  one. The evidence is the source of truth; the match % only orders the list.
- **PENDING_CLOSE** appears when you run during market hours: the criterion would be met if the bar closed now. It
  counts as not met. Re-run after the close; the latest bar becomes final 30 minutes after the session close
  (`close_buffer_minutes`).
- **Stretched** means a break that has already run more than `chase_atr` (1.5) ATRs beyond its zone.
- **Flow** (per scan direction):
  - `with_flow`: SPY, the sector vs SPY, and the name vs its sector agree with the scan's direction;
  - `mixed`: no clear majority either way;
  - `countertrend`: more components oppose the direction than agree.
- **Watch list**: tickers below a scan's minimum that would reach it if a near signal fired, with the exact trigger,
  zone and invalidation prices to paste into TradingView alerts. For SMAs and diagonal lines the prices are projected
  one bar ahead.

## How it works (one paragraph)

The scanner loads prices for the universe, its sector ETFs and SPY, using a Parquet cache in `cache/` that is updated
incrementally. It cuts every series at the as-of date, computes ATR/SMA/RSI and pivots, builds levels (SMA50/200,
horizontal pivot clusters, diagonal trendlines, manual levels) with ATR zones, classifies trend and range from
pivots, and evaluates the signal registry on each of the latest bars the enabled scans look back over, each bar
with the structure it had. It then measures relative
strength on price ratios, evaluates each enabled scan's criteria with three-valued logic, ranks each scan's tickers
by weighted % met and builds the watch list. The cut at the as-of date happens in
one place (`pipeline.scan`), so a scan of today and a scan of a past session use identical code. Tests confirm that changing bars after the as-of date
does not change the output.

## Development

```
uv run pytest            # full test suite
uv run ruff check src tests
```

Modules (`src/scanner/`), in layers; each imports only from the layers above it:
- **Inputs:** `config` (loading and validation), `sessions` (XNYS calendar), `data` (provider, Parquet cache,
  quality checks).
- **Engine:** `models` (value types), `indicators`, `levels`, `trend`, `signals` (registry; add a signal here and in
  `docs/signals.md`), `flow`, `alerts` (price-ladder alerts), `scans` (criteria, ranking, watch list), and
  `pipeline` (`scan` is pure; `run_scan` loads prices first).
- **Views, framework-free and unit-tested:** `labels` (display vocabulary), `views` (level and market-context
  dicts), `plotly_chart` (the interactive chart), `criteria` (Criteria tab), `app_view` (Scanner page view model),
  `markets/` (config, series, structure, RRG, PCA, figures, run).
- **Streamlit:** `app` (navigation), `scanner_page`, `markets/page`, `ui` (helpers both pages share), and
  `config_edit` (validated writes to `config/`).

## Assumptions and decisions

The spec's stated defaults are implemented as written unless listed here. Everything below is configurable.

**Owner decisions:**
- **B1, trend vs range.** Pivot comparisons use the tolerance `cluster_atr_mult × ATR`: higher if > tol, equal if
  within ±tol, lower if < −tol. Uptrend (HH+HL), downtrend (LH+LL) and range (EH+EL) are therefore mutually exclusive.
  A rise smaller than the tolerance is "equal", not "higher".
- **B2, flow agreement.** The `settings.yaml → flow` table: `mixed`/`n/a` components are neutral; countertrend if
  opposing > agreeing; with_flow if none oppose and at least one agrees; otherwise mixed.
- **B3, engulfing colour.** Breaks in `engulf` mode allow same-colour engulfing (spec default). The standalone
  `bullish_engulfing` and `bearish_engulfing` patterns require opposite colours
  (`pattern_engulfing_require_opposite_color: true`).
- **B4, hold direction.** The hold test bar must come from the level's side: the previous close is ≥ `zone_low` for
  support holds and ≤ `zone_high` for resistance holds.
- **B5, 2-bar break (OQ-1, 2026-10-16).**
  - **The break.** Bars N-1 and N both close beyond the zone. The `break_prior_side_bars` (3) closes before N-1
    stayed on the level's side, so reclaiming a level price was already beyond is not a break.
  - **Confirmation (revised 2026-10-24 from the owner's diagrams).** Both bars are full-bodied candles in the break
    direction and N closes beyond N-1's high (bullish) or low (bearish): `break_confirm: prior_extreme`. The original
    rule (N full-bodied and engulfing N-1) remains as `break_confirm: engulf`.
  - **No double report.** A level that breaks is not also reported as an opposite-role hold on the same bar.
- **Five scans and lookback windows (2026-10-24, owner request).** Bullish, non-bearish, neutral, non-bullish,
  bearish. Each scan sets `lookback_bars` (a signal on any of its latest N bars counts) and a signal criterion can
  narrow it with `within:`; this replaced the global `max_signal_age_bars` (now an error that says what to do).
  Every bar in a window is evaluated with only the data it had.
  - **Held signals.** `support_held` / `resistance_held` (horizontal levels) and `rising_line_held` /
    `falling_line_held` (sloped lines) fire on a pullback test of a level that existed before it and has held since.
  - **Slope decides (owner).** A rising line that held counts toward non-bearish and a falling one toward non-bullish,
    whether price held above it (support) or below it (resistance).
  - **Neutral is horizontal only (owner).** The `channel:` check needs a horizontal support and resistance holding
    every close of the window.
- **Directional scans (2026-10-24, owner request).** The options playbook was removed (named
  setups, one-sided biases, per-setup setting overrides and the nine-component score). Each scan has
  configurable criteria with optional weights, and ranks by the weighted % of criteria met. A ticker is listed in
  every scan whose `min_pct` it reaches. The watch list is kept, per scan. Settings are global only. A leftover
  `strategies.yaml`, a `scoring:` section, or old bias keys in `settings.yaml → flow` are errors that say what to do.
- **App-only (2026-10-18, owner request).** The CLI, the static HTML/CSV/JSON report, the TradingView list file, the
  run manifest, replay (`signals_log.csv`), check-reads (`expected_reads.csv`) and the HTML exports were removed.
  The spec's §8 (replay) and §10 (outputs and CLI) are retired. Past sessions stay reachable through the app's session
  date. A leftover `report:` or `replay:` section in `settings.yaml`, or an `expected_reads.csv`, is an error that says
  what to do (`report:` becomes `display:`).

**Interpretations of gaps in the spec:**
- Pivots: strictly greater than the left side, ≥ the right side, so the first bar of a flat top wins. Windows with
  missing bars produce no pivot.
- Horizontal clusters: greedy on sorted price, with a span ≤ `cluster_atr_mult × ATR` at the latest bar. Level =
  median. Pivot highs and lows are clustered separately. The role comes from the level's position against price.
  Lookback is the full window by default (`horizontal_lookback_bars`).
- Level quality: touches × 0.5^(bars since last touch / 60). SMAs have a fixed base quality.
- Trendlines: a close beyond the zone is a "material close through". The last 2 bars are excluded from invalidation
  so the 2-bar break of a line can still be evaluated as a break. Lines are indexed by trading
  bar, as on TradingView.
- Approaching: the close is outside the zone. "Moving toward" means closer to the level than
  `approach_lookback_bars` bars ago. The spec's "N bars ago (3)" became the setting `approach_lookback_bars`.
- RSI extension uses ≥ 70 / ≤ 30 (the §6.4 signal definition; TE-3's ">" differs).
- PENDING_CLOSE: only signals that would fire on the forming bar are reported, as PENDING_CLOSE. Signals that would
  not fire are absent rather than marked pending, so not every criterion becomes pending during market hours. An RSI
  criterion met on the forming bar is pending. At equal match, tickers with less pending weight rank first.
- A bar is provisional until session close + 30 min (US/Eastern, exchange calendar incl. early closes).
- Watch items: a ticker below a scan's minimum whose unmet signal criteria have "near" signals, where firing them
  would reach the minimum. The definitions per signal are in `signals.py` (`register_near`).
- The relative volume denominator is the average of the 20 bars **before** the current bar.
- Benchmark failure does not abort the run: flow components become `n/a` and a warning is shown.
- Symbols live only in `universe.csv` (owner, 2026-10-16); a leftover `universe.txt` or `sector_map.csv` is an error.
- Data-quality volume checks cover the charted period (`chart_bars`); missing-bar checks cover the whole window.
- **Recent gaps (owner, 2026-10-16).** A symbol is skipped if any open in the last `gap_lookback_bars` (15,
  about 3 weeks) gapped more than `gap_skip_atr` (3) × ATR from the prior close. ATR is measured as of the prior
  bar. Older gaps are ignored. A gapped benchmark or sector ETF still feeds flow.

**Price basis (DS-3).** `price_adjustment: split_only` uses yfinance `auto_adjust=False` OHLC. This was verified
to be split-adjusted (NVDA before its June 2024 10:1 split shows ≈ $120, not ≈ $1,200) and not dividend-adjusted
(XOM `Close` ≠ `Adj Close` before the Aug 2026 ex-date), which matches TradingView's default chart.

Residual differences against TradingView:
- Yahoo's daily bar for the latest session can be revised for a short time after the close, hence the 30-minute
  buffer.
- Volume is consolidated and differs slightly from some TradingView feeds.
- Past sessions always use today's split basis: after a later split, absolute prices for an older session differ
  from what that day showed, but the signals do not (they are scale-invariant).

## Open items for the owner

- **OQ-2 to OQ-5, parameters:** tune `zone_atr_mult`, `pivot_window`, the range tolerance and trendline settings
  against your chart reads.
- **Default scans:** the seeded criteria, weights, `min_pct` and lookbacks are starting points; tune them in the
  Criteria tab. Bullish/bearish (min 65, 5 bars) list a ticker with the trend **or** the 2-bar break (both = 100%),
  since breaks are rare and usually come before the pivot trend turns. Non-bearish/non-bullish (min 70, 10 bars)
  need a held level. Neutral (min 75, 10 bars) needs the 10-bar channel. On 2026-10-23 that listed 29 / 114 / 61 /
  89 / 42 of 218 tickers; non-bearish is the broadest, and `held_swing_bars` or a higher `min_pct` tightens it.
- **Run time:** each lookback bar rebuilds pivots, levels and trend, so a run takes about 3× as long as a 2-bar
  lookback (about 25 s for 225 symbols with the 12-bar default).

*Automated technical screening based on the owner's predefined rules; not investment advice. Manual validation
is required before any trade.*
