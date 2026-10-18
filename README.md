# Technical Opportunity Scanner (v2)

A daily-bar technical scanner for a personal watchlist. It finds support/resistance holds and breaks, trend and range
states, and RSI extension, using the rules in [requirements.md](requirements.md). Each setup is matched to the strategies in
`config/strategies.yaml`. The output is a ranked shortlist in a self-contained HTML report, with charts, evidence, score
breakdowns and copy-ready TradingView alert prices.

The scanner is the **discovery layer only**. It never touches ORATS, option chains, fundamentals, earnings or macro data,
and the config holds technical parameters only. Data is free daily OHLCV from `yfinance`.

## Setup

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run scanner validate          # checks config/ and lists unknown tickers
uv run scanner run               # writes reports/<date>/scan.html
```

## Files you maintain (`config/`)

| File | Purpose |
|---|---|
| `universe.csv` | The only symbol file. Columns: `symbol` (required), `type` (`stock`/`etf`), `sector_etf`, `enabled`, `notes`. A blank `sector_etf` shows name-vs-sector as `n/a`. `universe.txt` and `sector_map.csv` are no longer read; if either exists, validation fails with a message to merge it into `universe.csv`. |
| `strategies.yaml` | Your playbook. See [docs/editing-strategies.md](docs/editing-strategies.md). |
| `strategies.default.yaml` | The preserved original playbook. The app's **Restore defaults** copies it over `strategies.yaml`. Never read by the scanner. |
| `settings.yaml` | All technical parameters, with defaults. Keys marked `[added]` are not in the spec's §6.9 (see Assumptions). |
| `manual_levels.yaml` | Optional owner-drawn levels (see `manual_levels.example.yaml`). |
| `expected_reads.csv` | Optional chart reads for `check-reads` (see `expected_reads.example.csv`). Separate multiple expected items with `;`. |
| `markets.yaml` | Lists for the app's **Markets** page, one per tool (`levels`, `rrg` with its `benchmark`, `pca`). Each holds tickers or `NUM/DEN` ratios. Also holds per-resolution parameters. Independent of `universe.csv`. |

The signal vocabulary and its exact definitions are in [docs/signals.md](docs/signals.md).

## Commands

```
scanner run                                  # full scan as of the latest completed bar
scanner run --symbol AAPL                    # deep dive: all strategies incl. non-matching rules → scan_AAPL.html
scanner run --strategy bull_put_spread       # only that strategy → scan_strategy_bull_put_spread.html
scanner run --as-of 2026-09-15               # replay one date (prior session if not a trading day)
scanner run --refresh                        # re-download instead of using the cache
scanner replay --start 2026-06-01 --end 2026-09-30   # signals_log.csv with forward outcomes
scanner explain --symbol AAPL --strategy bull_put_spread [--as-of DATE]
scanner check-reads                          # compare replay with config/expected_reads.csv
scanner validate [--offline]                 # also checks config/markets.yaml when present
scanner cache info | scanner cache refresh
```

### Streamlit app

```
uv run streamlit run src/scanner/app.py      # run from the repository root (reads .streamlit/config.toml)
```

The app has two pages, **Scanner** and **Markets**, in the top navigation. On the Scanner page, the Results tab goes
from high level to detail: overview, then the list, then one setup, then its evidence.

- **Run.** At the top, pick a session date (default today), optionally tick **Force refresh**, then press **Run
  analysis**. Every run scans all enabled symbols and all active strategies. All filtering happens after the scan.
- **Overview band.** It shows the session (with a *prior session* or *provisional bar* badge), the benchmark's trend and
  1m/3m returns, and how many tickers are bullish, bearish and neutral candidates. **Watch list** opens the near-setups; pick a row to open that symbol.
  **Skipped / warnings** lists what was left out. The **Sector** pills show each sector ETF's trend, 1-month return
  and number of symbols with a setup. Picking one filters the list.
- **Bullish / Bearish / Neutral candidates.** Every active strategy is still evaluated against every ticker. A
  ticker's direction comes from its matched setups, grouped by strategy bias (Bullish = bullish + not bearish,
  Bearish = bearish + not bullish, Neutral = neutral). It is **Bullish** or **Bearish** when its matches are on
  that side (a neutral match beside them does not change it). It is **Neutral** when it matches only neutral
  strategies or both bullish and bearish ones (two-sided, e.g. a bull put and a bear call spread in a range).
  Tickers without a full match are not grouped; the watch list covers the near ones.
- **The list.** A switcher picks the group (with its ticker count). The list has one row per ticker: its best setup
  in that direction and its score, plus the other strategies in that direction that also match. Tickers are ordered
  by that best score. Pick a row to open it in the setup pane beside the list, which stays in view while you scroll.
  **Strategy**, **Symbol** and the sector pills filter the list; a ticker's direction is taken from the strategies
  left after the strategy filter. A score (0–100) belongs to a symbol × strategy pair.
- **Ticker descriptions.** The name is your `notes` from `universe.csv`, or else the yfinance name. The summary is
  the yfinance business summary. Both are display only and cached in `cache/profiles.json` for 30 days; **Force
  refresh** refetches them.
- **Setup pane.** The header shows the symbol, name, its direction badge and its matched setups summarised by
  direction (each with its score), then the close, change, RSI, ATR and sector. Below it is one Plotly
  chart per symbol, opening on the last 60 bars, with the signal levels and markers of all its strategies. You can
  zoom, pan back through the full history, and use **Legend** to list or hide levels. Buttons beside it overlay
  the symbol's sector ETF, the benchmark and the ETF/benchmark ratio (the series sector flow is read from), each
  on its own right-hand axis in its own colour; they toggle inside the chart, so they never reload it or reset the
  zoom. Strategy pills under the chart
  switch between every strategy evaluated for that symbol (matches in its direction first, then its other
  matches, each with its score; then the rest with rules met). Switching
  leaves the chart and its zoom as they are and updates only what follows: score, rules met, trend and flow, one
  plain line per rule, and the **price ladder**, which lists alert prices (including invalidation) and the last
  close, highest first. A near miss shows its trigger prices, or the
  nearest zones if nothing is close. **Criteria** jumps to the strategy's logic; **Export** downloads the HTML report
  for this setup.
- **Evidence tabs.** **Why** has the score breakdown, the signals that fired, the trend and flow reasoning, and the
  raw rule evidence. **Levels** has the nearest levels. **About** has the full business summary.
- **Criteria tab.** Each strategy shows a one-line recipe (trend, required and trigger signals, extension, flow).
  **Show logic** has the exact logic it is scanned with, built from `strategies.yaml`, `settings.yaml` (per-strategy
  overrides applied) and the signal registry, with its signal parameters in one table. The **Strategy** picker shows
  one strategy; the setup pane's **Criteria** button opens it there. **Edit rules** on a strategy sets `enabled` and the technical rules
  (`trend`, `signals_all`, `signals_any`, `not_extended`, `alignment`) from dropdowns that offer only valid values.
  Saving validates first, then rewrites only that strategy's lines in `strategies.yaml`; comments and everything else
  are kept. Strategies that differ from `strategies.default.yaml` show a **modified** badge, and **Restore defaults**
  puts the original back. Saving or restoring clears the current results.
- **Report tab.** The full HTML report. Pick symbols and/or strategies to filter it. Picked symbols show every picked
  strategy, including rules that did not match. **Export report** downloads the report as shown. The app does not
  write to `reports/`.
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
- **Export HTML.** An offline snapshot of all three views at the current resolution, with plotly.js embedded once
  (about 7–8 MB with the default lists).

Use `uv run scanner …`, or activate `.venv`. All commands accept `--config-dir/-c`. Output commands also accept
`--reports-dir`.

**Exit codes:**
- `0` success;
- `1` hard failure (invalid config, no analysable symbols);
- `2` completed with data or symbol warnings (skipped symbols, quality warnings, failed downloads).

Each run writes `run.json` with its parameters, versions, counts, warnings, and hashes of the config and data. A
scheduler (cron, Task Scheduler) can run `scanner run` after the close and check the exit code.

### Outputs (`reports/<as-of>/`)

- `scan.html`: the report. It is a single file with no external requests and works offline and in print. Sections:
  1. Run summary
  2. Market context
  3. Ranked shortlist (sortable, with filters)
  4. Candidate cards
  5. Watch list
  6. Diagnostics
- `candidates.json`: the full audit trail (rules, evidence, score breakdown, levels, signals per symbol).
- `candidates.csv`: one row per (symbol, strategy).
- `tradingview_watchlist.txt`: comma-separated shortlist and watch-list symbols, ready to paste into a TradingView watchlist.
- `run.json` and `config_snapshot/`: the run manifest and copies of `settings.yaml` and `strategies.yaml`.

Replay writes to `reports/replay_<start>_<end>/signals_log.csv`. Its forward returns and MFE/MAE (in ATRs) are informational
only and **not a backtest**.

## Reading the report

- **Score** (0–100) is a transparent weighted sum, broken down on every card. The evidence is the source of truth;
  the score only orders the list.
- **PENDING_CLOSE** appears when you run during market hours: the signal would fire if the bar closed now. Re-run
  after the close; the latest bar becomes final 30 minutes after the session close (`close_buffer_minutes`).
- **Stretched** means a break that has already run more than `chase_atr` (1.5) ATRs beyond its zone.
- **Flow**:
  - `with_flow`: SPY, the sector vs SPY, and the name vs its sector agree with the strategy's bias;
  - `mixed`: no clear majority either way;
  - `countertrend`: more components oppose the bias than agree.
- **Watch list**: setups that are one bar or one move away from triggering, with the exact trigger, zone and
  invalidation prices to paste into TradingView alerts. For SMAs and diagonal lines the prices are projected one bar
  ahead.

## How it works (one paragraph)

The scanner loads prices for the universe, its sector ETFs and SPY, using a Parquet cache in `cache/` that is updated
incrementally. It cuts every series at the as-of date, computes ATR/SMA/RSI and pivots, builds levels (SMA50/200,
horizontal pivot clusters, diagonal trendlines, manual levels) with ATR zones, classifies trend and range from
pivots, and evaluates the signal registry on the latest bar and the bar before it. It then measures relative
strength on price ratios, evaluates each enabled strategy's rules with three-valued logic, scores and ranks the
matches, builds the watch list, and renders the report. The cut at the as-of date happens in one place
(`pipeline.scan`), so live runs and replays use identical code. Tests confirm that changing bars after the as-of date
does not change the output.

## Development

```
uv run pytest            # full test suite
uv run ruff check src tests
```

Modules (`src/scanner/`):
- `config` (validation);
- `sessions` (XNYS calendar);
- `data` (provider, cache, quality checks);
- `indicators`;
- `levels`;
- `trend`;
- `signals` (registry; add a signal here and in `docs/signals.md`);
- `flow`;
- `strategies`;
- `scoring`;
- `pipeline`;
- `replay`;
- `charts` (inline SVG, for the HTML report);
- `report`;
- `cli`;
- app only: `app`, `app_view`, `plotly_chart` (interactive chart), `criteria` (Criteria tab).

## Assumptions and decisions

The spec's stated defaults are implemented as written unless listed here. Everything below is configurable.

**Owner decisions:**
- **B1, trend vs range.** Pivot comparisons use the tolerance `cluster_atr_mult × ATR`: higher if > tol, equal if
  within ±tol, lower if < −tol. Uptrend (HH+HL), downtrend (LH+LL) and range (EH+EL) are therefore mutually exclusive.
  A rise smaller than the tolerance is "equal", not "higher".
- **B2, flow agreement.** The `settings.yaml → flow` table: `mixed`/`n/a` components are neutral; countertrend if
  opposing > agreeing; with_flow if none oppose and at least one agrees; otherwise mixed.
- **B3, engulfing colour.** Breaks allow same-colour engulfing (spec default). The standalone `bullish_engulfing`
  and `bearish_engulfing` patterns require opposite colours (`pattern_engulfing_require_opposite_color: true`).
- **B4, hold direction.** The hold test bar must come from the level's side: the previous close is ≥ `zone_low` for
  support holds and ≤ `zone_high` for resistance holds.
- **B5, 2-bar break (OQ-1, 2026-10-16).**
  - **The break.** Bars N-1 and N both close beyond the zone. Bar N is full-bodied and engulfs N-1. The
    `break_prior_side_bars` (3) closes before N-1 stayed on the level's side, so reclaiming a level price was
    already beyond is not a break.
  - **No double report.** A level that breaks is not also reported as an opposite-role hold on the same bar.
  - **Strategies.** Bull Call Spread and Bear Put Spread require the break (`signals_all`). A standalone engulfing
    no longer qualifies them.
- **Per-strategy overrides** are limited to `signals:` keys and the RSI thresholds.
- **Scoring normalisations** use the defaults listed in [docs/signals.md](docs/signals.md#score-0100), to be tuned
  with replay.
- **Scores belong to a (symbol, strategy) pair** (2026-10-16, owner request). The signal is scored against the best
  signal that strategy allows. `trend_fit` measures the strength of the trend state it asked for. `room` and RSI
  headroom follow its bias. A strategy's `scoring: weights:` in `strategies.yaml` replace the global weights.
  Defaults: directional debit, premium sold at a level, and range-bound profiles; PMCC/PMCP use the global weights.

**Interpretations of gaps in the spec:**
- Pivots: strictly greater than the left side, ≥ the right side, so the first bar of a flat top wins. Windows with
  missing bars produce no pivot.
- Horizontal clusters: greedy on sorted price, with a span ≤ `cluster_atr_mult × ATR` at the latest bar. Level =
  median. Pivot highs and lows are clustered separately. The role comes from the level's position against price.
  Lookback is the full window by default (`horizontal_lookback_bars`).
- Level quality: touches × 0.5^(bars since last touch / 60). SMAs have a fixed base quality.
- Trendlines: a close beyond the zone is a "material close through". The last `max_signal_age_bars` + 1 bars are
  excluded from invalidation so the breaking bar can still be evaluated as a break. Lines are indexed by trading
  bar, as on TradingView.
- Approaching: the close is outside the zone. "Moving toward" means closer to the level than
  `approach_lookback_bars` bars ago. The spec's "N bars ago (3)" became the setting `approach_lookback_bars`.
- RSI extension uses ≥ 70 / ≤ 30 (the §6.4 signal definition; TE-3's ">" differs).
- PENDING_CLOSE: only signals that would fire on the forming bar are reported, as PENDING_CLOSE. Signals that would
  not fire are absent rather than marked pending, so not every rule becomes pending during market hours. Pending
  candidates rank after confirmed ones.
- A bar is provisional until session close + 30 min (US/Eastern, exchange calendar incl. early closes).
- Shortlist: one row per symbol. The row score is the symbol's best candidate score. Per-strategy ranks show in
  chip tooltips and via the strategy filter.
- Watch items: every non-signal rule (trend, extension, alignment) passes and a required signal is "near". The
  definitions per signal are in `signals.py` (`register_near`).
- The relative volume denominator is the average of the 20 bars **before** the current bar.
- Benchmark failure does not abort the run: flow components become `n/a` and the run exits with code 2.
- Symbols live only in `universe.csv` (owner, 2026-10-16); a leftover `universe.txt` or `sector_map.csv` is an error.
  `expected_reads.csv` separates multiple items with `;`.
- `run --symbol` and `--strategy` write `scan_<SYMBOL>.html` and `scan_strategy_<id>.html` so the full report is not overwritten.
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
- Replays always use today's split basis: after a future split, absolute prices in older reports differ, but the
  signals do not (they are scale-invariant). `run.json` records a data fingerprint per symbol.

## Open items for the owner

- **OQ-2 to OQ-5, parameters:** tune `zone_atr_mult`, `pivot_window`, the range tolerance and trendline settings
  against your chart reads.
- **OQ-9/OQ-10, seeded technical rules:** these were inferred from the playbook; edit freely.

*Automated technical screening based on the owner's predefined rules; not investment advice. Manual validation
is required before any trade.*
