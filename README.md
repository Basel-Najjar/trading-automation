# Technical Opportunity Scanner (v2)

A daily-bar technical scanner for a personal watchlist. It finds support/resistance holds and breaks, trend and range
states, and RSI extension, using the rules in [requirements.md](requirements.md). Each setup is matched to the strategies in
`config/strategies.yaml`. The output is a ranked shortlist in a self-contained HTML report, with charts, evidence, manual
ORATS/fundamental checklists and copy-ready TradingView alert prices.

The scanner is the **discovery layer only**. It never touches ORATS, option chains, fundamentals, earnings or macro data;
those stay as manual checklist items on each card. Data is free daily OHLCV from `yfinance`.

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
| `universe.csv` or `universe.txt` | Watchlist. CSV columns: `symbol` (required), `type` (`stock`/`etf`), `enabled`, `notes`, optional `sector_etf`. TXT: one symbol per line, `#` comments allowed. Keep only one of the two files. |
| `sector_map.csv` | `symbol,sector_etf`. It wins over the universe's `sector_etf` column. Unmapped symbols show name-vs-sector as `n/a`. |
| `strategies.yaml` | Your playbook. See [docs/editing-strategies.md](docs/editing-strategies.md). |
| `settings.yaml` | All technical parameters, with defaults. Keys marked `[added]` are not in the spec's §6.9 (see Assumptions). |
| `manual_levels.yaml` | Optional owner-drawn levels (see `manual_levels.example.yaml`). |
| `expected_reads.csv` | Optional chart reads for `check-reads` (see `expected_reads.example.csv`). Separate multiple expected items with `;`. |

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
scanner validate [--offline]
scanner cache info | scanner cache refresh
```

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
- `charts` (inline SVG);
- `report`;
- `cli`.

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
- **Per-strategy overrides** are limited to `signals:` keys and the RSI thresholds.
- **Scoring normalisations** use the defaults listed in [docs/signals.md](docs/signals.md#score-0100), to be tuned
  with replay.

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
- Both `universe.csv` and `universe.txt` present is an error. `expected_reads.csv` separates multiple items with `;`.
- `run --symbol` and `--strategy` write `scan_<SYMBOL>.html` and `scan_strategy_<id>.html` so the full report is not overwritten.
- Data-quality volume/gap checks cover the charted period (`chart_bars`); missing-bar checks cover the whole window.

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

- **OQ-1, break definition:** confirm via `check-reads` and replay (conditions 4–5 are configurable).
- **OQ-2 to OQ-5, parameters:** tune `zone_atr_mult`, `pivot_window`, the range tolerance and trendline settings
  against your chart reads.
- **OQ-9/OQ-10, seeded technical rules:** these were inferred from the playbook; edit freely.
- **Wheel checklist:** the `orIvFcst20d` threshold is marked "assumed; confirm" in Appendix A.

*Automated technical screening based on the owner's predefined rules; not investment advice. Manual validation
(volatility, fundamentals, events, macro) is required before any trade.*
