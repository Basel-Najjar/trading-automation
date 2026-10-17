# Technical Opportunity Scanner (v2) — Requirements

**Status:** Draft v2.0 (supersedes the ORATS-based v1) · **Audience:** Coding agent · **Owner:** Trader / end user

---

## 1. Purpose

Build a **daily-bar technical scanner** that identifies *technical* trading opportunities across a user-maintained watchlist and maps each one to the options strategies (from the owner's strategy playbook) that are consistent with the technical setup.

The scanner is the **discovery layer only**. The owner takes the ranked shortlist and manually validates each name with:

- **TradingView** (charts, company info; alerts are configured manually by the owner),
- **ORATS dashboard** (volatility analysis and scanning, including the D/F/S edge checks),
- **Google Finance** (macro research).

### 1.1 Goals

- G1. Encode the owner's simple technical process (S/R from SMAs and pivot-based lines, pivot-based trend, 1-bar/engulfing confirmations, RSI extension) as deterministic, inspectable rules.
- G2. Produce a **ranked shortlist** with the candidate **strategies** each setup supports, with the technical evidence, score breakdown and alert prices behind each one.
- G3. Make every result **explainable**: which levels, which bars, which rule fired, and why a candidate ranked where it did.
- G4. Give the owner **concrete price levels** per candidate to copy into TradingView alerts (trigger prices, zone edges, invalidation levels).
- G5. Keep the strategy playbook in a **single, human-editable file** so the owner can add, remove, or tune strategies without code changes.
- G6. Support **historical replay** to check scanner output against chart reads the owner already knows.
- G7. Cost: **$0 in data and subscriptions** (free `yfinance` only).

### 1.2 Non-goals

- No ORATS API usage and no option-chain/option-pricing data of any kind.
- No fundamentals, earnings dates, or macro/event data in the scanner. (These stay manual; see §9.)
- No order placement, brokerage connection, or position tracking.
- No Pine Script generation (explicitly deferred).
- No pushing alerts into TradingView (owner configures manually).
- No scheduling or notifications in v1 (design must allow adding them later; see §10.3).
- No ML or "fancy" indicators beyond those specified here.

---

## 2. Definitions

| Term | Meaning |
|---|---|
| **Bar** | One daily candle (OHLCV). All analysis uses daily bars only. |
| **Pivot high / low** | Local extreme: a bar whose high (low) is the highest (lowest) among `pivot_window` bars on each side. Confirmed only after `pivot_window` subsequent bars exist (no look-ahead). |
| **Level** | A support or resistance reference line: an SMA, a horizontal line, or a diagonal trendline. |
| **Zone** | A level ± `zone_atr_mult × ATR(14)`. Support/resistance is treated as a zone, not a single price. |
| **Trend state** | `uptrend`, `downtrend`, `range`, or `mixed`, based on pivots (§6.3). |
| **Signal** | A named, boolean technical event evaluated on the latest completed bar (e.g., `support_hold`). Vocabulary in §6.4. |
| **Setup / candidate** | A (symbol, strategy) pair whose technical conditions are satisfied. |
| **Bias** | Strategy direction label: `bullish`, `bearish`, `non_bearish`, `non_bullish`, `neutral`. |
| **Watch item** | A symbol that is *near* a setup but not yet triggered, with the trigger price. |
| **wld** | Win/lose/draw (used in the owner's "time-based exits" notes). Informational text on strategy cards. |

---

## 3. Data

### 3.1 Source

- DS-1. **Only `yfinance`** (daily OHLCV). No other data providers in v1.
- DS-2. Wrap in a `PriceProvider` interface so another provider can be swapped in later without changing downstream code.
- DS-3. **Price basis:** use **split-adjusted, not dividend-adjusted** OHLC (`auto_adjust=False`; use Open/High/Low/Close, ignore `Adj Close`) so SMAs, pivots, and levels match TradingView's default chart. Make this a setting (`price_adjustment: split_only | total_return`). The agent should verify against a few known symbols and document any residual differences.
- DS-4. Download in batches (`yf.download` with multiple tickers) to minimize requests; retry with backoff; one failing ticker never aborts the run.

### 3.2 Local cache

- DS-5. Cache daily bars locally (Parquet or SQLite/DuckDB), updated incrementally.
- DS-6. Cache is required for **replay** (stable historical data) and for resilience when yfinance is flaky.
- DS-7. Detect corporate-action revisions (e.g., split changes prior closes) by comparing overlapping cached vs. fresh bars; on mismatch, re-download that symbol's full history.
- DS-8. `--refresh` bypasses the cache for the run.

### 3.3 History depth and data quality

- DS-9. Default history: **3 years** (setting). Minimum bars required: 250 (SMA200 + pivot warm-up). Symbols with insufficient history are skipped with a visible reason (or SMA200-dependent logic marked `n/a` if a setting allows partial analysis).
- DS-10. Data-quality checks per symbol, reported in the run summary: missing bars vs. the trading calendar, stale last bar, zero/NaN volume. Recent extreme gaps: a symbol whose open gapped more than `gap_skip_atr` (default 3) × ATR from the prior close within the last `gap_lookback_bars` (default 15, ~3 weeks) is **skipped** with the gap as the reason; older gaps are ignored. Benchmark/sector ETFs still feed flow. (Revised 2026-10-16 by the owner; previously a >10% warning only.)
- DS-11. **Timing:** default expectation is an **end-of-day run after the close** on completed bars. If the last bar is still forming (run during market hours), mark it `provisional`, and mark every signal that depends on it `PENDING_CLOSE` instead of true/false.
- DS-12. Use a US equity trading calendar for "latest completed bar" and replay date logic.

---

## 4. Inputs (Owner-Maintained Files)

```
config/
  universe.csv                    # watchlist + sector ETF per symbol (the only symbol file)
  strategies.yaml                 # the editable strategy playbook (§7, seed in Appendix A)
  strategies.default.yaml         # preserved original playbook; the app's "restore defaults" copies it back
  settings.yaml                   # global technical parameters (§6.9)
  manual_levels.yaml              # OPTIONAL: owner-drawn levels per symbol (see §6.2.5)
  expected_reads.csv              # OPTIONAL: owner's known chart reads for replay validation (§8.2)
```

- IN-1. **Universe:** `universe.csv` (`symbol` required; optional `type` (`etf|stock`), `sector_etf`, `enabled`, `notes`). Symbols are normalized (uppercase, whitespace stripped); duplicates warned and de-duplicated. (Changed 2026-10-16: `universe.txt` and `sector_map.csv` were consolidated into this one file.)
- IN-2. **Sector mapping:** the `sector_etf` column of `universe.csv`. Symbols with no mapping skip the name-vs-sector relative-strength check (reported as `n/a`, not as a failure).
- IN-3. **Benchmark:** SPY by default (`settings.yaml`).
- IN-4. All input files are validated by `scanner validate` with specific, human-readable errors (file, row/key, problem). Unknown tickers (no yfinance data) are listed.

---

## 5. Pipeline

```
Universe + mapping + strategies.yaml + settings.yaml
   │
   ▼
[1] Load/refresh price data (cache) for universe + sector ETFs + benchmark
   │
   ▼
[2] Per-symbol indicators: ATR(14), SMA50, SMA200, RSI(14), pivots
   │
   ▼
[3] Levels: SMA zones, horizontal zones, diagonal trendlines → ranked, with confluence
   │
   ▼
[4] Trend state, range detection, proximity to levels
   │
   ▼
[5] Signals on the latest completed bar (holds, breaks, engulfing, approaching, extension)
   │
   ▼
[6] Relative strength / flow alignment (name vs sector ETF, sector vs SPY, SPY trend)
   │
   ▼
[7] Strategy matching: evaluate each enabled strategy's technical rules from strategies.yaml
   │
   ▼
[8] Score + rank; build watch list (near-setups with trigger prices)
   │
   ▼
[9] Render HTML report (+ optional CSV/JSON); every item carries its audit trail
```

---

## 6. Technical Engine

All calculations are on **daily bars**. The owner's process is intentionally simple; do not add indicators beyond those below. All numeric parameters live in `settings.yaml` (§6.9) with the defaults stated here.

### 6.1 Indicators

- TE-1. **ATR(14)** (Wilder). Used for zone width, proximity, and "stretched" measures.
- TE-2. **SMA(50)** and **SMA(200)** of close.
- TE-3. **RSI(14)** (Wilder smoothing), standard thresholds: **> 70 overbought/extended up; < 30 oversold/extended down**.
- TE-4. Relative volume (volume ÷ 20-day average volume): **informational only**, shown on cards; never a gate or score input in v1.

### 6.2 Support & Resistance (the owner's definition)

Support/resistance is any of:

1. **50 SMA or 200 SMA**
2. **Horizontal line** approximately connecting previous pivot highs or lows
3. **Diagonal trendline** approximately connecting previous pivot highs or lows

with the **ATR-based zone** around each.

#### 6.2.1 Pivots

- TE-5. Pivots are detected on highs (pivot highs) and lows (pivot lows) with `pivot_window` bars on each side (default **5**). Pivot lists record date, price, and bar index.
- TE-6. **No look-ahead:** a pivot only exists once `pivot_window` later bars have closed. This must hold in replay (§8).

#### 6.2.2 Zones

- TE-7. Zone for any level at bar *t*: `[level_t − k·ATR_t, level_t + k·ATR_t]` where `k = zone_atr_mult` (default **0.25**, i.e., total zone width 0.5 ATR). For SMAs and diagonal lines, `level_t` is the line's value at bar *t* (the zone moves with the line).
- TE-8. Zone edges are named consistently: `zone_low`, `zone_high`, `level`.

#### 6.2.3 SMA levels

- TE-9. SMA50 and SMA200 are always evaluated as dynamic levels. An SMA acts as **support when price is above it** and **resistance when price is below it** (role is re-evaluated each bar).

#### 6.2.4 Horizontal and diagonal lines

- TE-10. **Horizontal levels:** cluster pivot highs (resistance) and pivot lows (support) whose prices fall within `cluster_atr_mult × ATR` of each other (default 0.5). A horizontal level requires ≥ `min_touches_horizontal` pivots (default **2**). Level price = mean (or median) of the cluster; record touch count and most recent touch date. Include a **role reversal** flag when a former resistance cluster has been broken and is now below price (and vice versa).
- TE-11. **Diagonal trendlines:** support lines through ≥ `min_touches_diagonal` pivot lows (default **2**; prefer 3), resistance lines through pivot highs. Candidate lines are formed from pairs of recent pivots (look back `trendline_lookback_bars`, default 250) and scored by: touches within the zone tolerance, absence of material closes through the line between anchor points, and recency. Keep the best line(s) per side (default top 2).
  - Price scale for the fit is a setting (`trendline_scale: linear | log`, default **linear**).
  - A trendline is invalidated for use as *support* if any close since its second anchor is below its zone (and vice versa for resistance), unless that event is the current signal.
- TE-12. Each line/level carries: type (`sma50 | sma200 | horizontal | diagonal`), role (`support | resistance`), current `level`, `zone_low/high`, touches, anchor pivots (dates + prices), slope (diagonals), and a **quality** value (touches × recency factor).

#### 6.2.5 Confluence and optional manual levels

- TE-13. **Confluence:** levels of different types whose zones overlap on the latest bar are merged into a *confluence zone* with `sources` listing every contributing level (e.g., `[sma50, horizontal]`). Confluence raises score (§6.8).
- TE-14. **Nearest levels:** for each symbol, report the nearest N supports below and N resistances above the latest close (default N = 3), with distance in % and ATR.
- TE-15. *(Optional, should-have)* `manual_levels.yaml` lets the owner add or override horizontal levels and trendline anchor points per symbol (`type`, `role`, `price` or anchors `[(date, price), (date, price)]`, `note`). Manual levels are tagged `manual` and always participate. The core scanner must work without this file.

### 6.3 Trend and Range

- TE-16. **Trend state from pivots** (the owner's rule: higher pivot highs/lows):
  - `uptrend`: the last two confirmed pivot highs are ascending **and** the last two confirmed pivot lows are ascending (higher highs + higher lows).
  - `downtrend`: last two pivot highs descending **and** last two pivot lows descending.
  - `range`: pivot highs approximately equal **and** pivot lows approximately equal (each pair within `cluster_atr_mult × ATR`), price currently between the range boundaries, with ≥ 2 touches on each boundary (reusing TE-10 clusters).
  - `mixed`: anything else (e.g., higher highs but lower lows, or too few pivots).
- TE-17. Report the pivots used (dates/prices) so the owner can see exactly why the trend state was assigned.
- TE-18. **Range geometry** (when `range`): `range_high`, `range_low`, `range_width_atr`, position within range as a 0–1 value, and `range_mid` flag (position within the middle band, default 0.35–0.65).
- TE-19. **Approaching a level:** flag `approaching_support/resistance` when price is within `approach_atr` (default **1.0 ATR**) of a level's zone edge and moving toward it (close nearer to the level than N bars ago, default 3).

### 6.4 Signals (evaluated on the latest completed bar `N`; bar `N-1` is the prior bar)

Each signal returns `true | false | PENDING_CLOSE` and, when true, the evidence: level used, bars involved (dates and OHLC), and measured values.

**Candle helpers**

- `body = |close − open|`; `range = high − low`.
- **Full-bodied bar:** `body / range ≥ full_body_ratio` (default **0.70**; i.e., small wicks). Direction: bullish if close > open, bearish if close < open.
- **Engulfing:** bar N's body fully covers bar N-1's body (`max(open,close)_N ≥ max(open,close)_{N-1}` and `min(open,close)_N ≤ min(open,close)_{N-1}`), with strict containment tolerance in settings. Same-colour engulfing is allowed by default (`engulfing_require_opposite_color: false`); see OQ-1.

**Hold signals (1-bar confirmation)**

- `support_hold`: for a support level L, within the last `hold_test_lookback` bars (default 3, including bar N-1 and N), a bar's **low entered the zone** (`low ≤ zone_high`) **and closed back at or above `zone_low`**; and bar N **closes above bar N-1's high**.
- `resistance_hold`: mirror — a bar's high entered the zone (`high ≥ zone_low`) and closed at or below `zone_high`; bar N **closes below bar N-1's low**.

**Break signals (2-bar break with engulfing confirmation)**

The owner's rule: *a break is a 2-bar close beyond the level; an engulfing candle is required and the second bar must be full-bodied.* Bar N-1 is the first close beyond the zone; bar N is the "second bar". (Revised 2026-10-16, decision B5; the original single-bar definition fired on reclaims of levels price was already beyond.)

- `support_break` for support level L, all of:
  1. Bar N-1 **closes below `zone_low`** (first close).
  2. Bar N **closes below `zone_low`** (second close).
  3. Bar N is a **full-bodied bearish** bar.
  4. Bar N **engulfs** bar N-1 (body).
  5. Price came from the support side: each of the `break_prior_side_bars` (default 3) closes before bar N-1 was ≥ `zone_low`.
- `resistance_break`: mirror (N-1 and N close above `zone_high`, N full-bodied bullish and engulfs N-1, the `break_prior_side_bars` closes before N-1 all ≤ `zone_high`).
- A level that breaks on a bar is not also reported as a hold of the opposite role on that bar.

**Pattern signals**

- `bullish_engulfing` / `bearish_engulfing`: bar N engulfs bar N-1 and closes bullish/bearish. Report `at_level` (bar's range touched any support/resistance zone) as an attribute; `at_level` is a **score bonus**, not a requirement.

**Context signals**

- `approaching_support`, `approaching_resistance` (TE-19).
- `range_bound` (trend state = `range`) and `range_mid`.
- `extended_up` (RSI14 ≥ 70) and `extended_down` (RSI14 ≤ 30). Report the RSI value and its distance to the threshold. A strategy's `not_extended` rule uses these (see §7.3).

**Signal age**

- SG-1. Signals on bar N have age 0. Signals formed on N-1, N-2 (up to `max_signal_age_bars`, default **1**) are reported as "recent" (lower score). Older signals are not reported.

### 6.5 Relative Strength and Flow Alignment

The owner favors going with the flow over countertrading.

- RS-1. **Market trend:** trend state (TE-16) for the benchmark (SPY).
- RS-2. **Sector vs. market:** ratio series `sector_ETF / SPY`; trend state computed on the ratio with the same pivot logic.
- RS-3. **Name vs. sector:** ratio series `symbol / sector_ETF`; same trend logic. Skipped (`n/a`) when no mapping.
- RS-4. For ratio series, pivots are computed on the ratio's close series (using the ratio of closes for both highs and lows is acceptable for v1; document the choice).
- RS-5. **Informational returns:** 1m/3m/6m return of symbol, its sector ETF, and SPY, and symbol-minus-ETF spreads.
- RS-6. **`with_flow` classification per candidate**, relative to the strategy's direction:
  - `with_flow`: market, sector-vs-market, and name-vs-sector trends all agree with the bias (or are `range`/`n/a` for non-directional biases).
  - `mixed`: some agree, some oppose.
  - `countertrend`: the majority oppose.
  - Report each component (e.g., "SPY: uptrend; XLY/SPY: downtrend; AMZN/XLY: uptrend").
- RS-7. Alignment is **soft by default** (affects score and is labeled), and strategies may set it to `required` or `ignore` in `strategies.yaml`.

### 6.6 Strategy Matching

- SM-1. For every symbol × every **enabled** strategy in `strategies.yaml`, evaluate the strategy's technical rules (§7.3). A symbol can match multiple strategies; show all matches.
- SM-2. Strategies with `enabled: false` are skipped; the run summary lists them as skipped.
- SM-3. A match requires: trend state in the strategy's allowed set (if specified), the required signals (`signals_all`) and at least one of `signals_any` (if specified), the `not_extended` rule (if specified), and flow alignment per its setting.
- SM-4. The match result lists each rule evaluated with `pass | fail | PENDING_CLOSE` and the evidence, to support `scanner explain` (§10).

### 6.7 Watch List (near-setups)

- WL-1. Report symbols that are **close to** triggering any enabled strategy's rules but have not triggered, such as:
  - `approaching_support/resistance` with the strategy-relevant trend,
  - a support/resistance **test** has occurred but the 1-bar confirmation hasn't printed yet,
  - range symbols near a range edge,
  - price in the zone but the break's engulfing/full-body condition not met yet.
- WL-2. Each watch item carries **trigger prices** the owner can copy into TradingView alerts, e.g.:
  - Support-hold trigger: *"close above $X (prior bar's high)"*
  - Support-break trigger: *"close below $Y (zone low)"* with the note that the engulfing/full-body conditions must be checked on the bar.
  - Zone edges and the level's current value (and, for diagonals/SMAs, the slope or value projected one bar ahead).
- WL-3. Cap watch items per symbol (default 3) and in total (default 30), ranked by proximity/quality.

### 6.8 Scoring and Ranking

- SC-1. **Score** (0–100) is a transparent weighted sum; weights in `settings.yaml`. Components and defaults:
  - **Signal type & quality** — break with engulfing+full-body (highest), hold (1-bar), standalone engulfing, context-only (lowest).
  - **Level quality** — touches, recency, and role-reversal bonus.
  - **Confluence** — number of distinct level types overlapping the signal level.
  - **Trend fit** — trend state matches the strategy's preferred states.
  - **Flow alignment** — `with_flow` > `mixed` > `countertrend`.
  - **RSI headroom** — distance from the extension threshold for directional strategies.
  - **Proximity/quality of entry** — for holds, distance from the level (closer is better); for breaks, how far price already is beyond the zone (flag `stretched` if > `chase_atr`, default 1.5 ATR).
  - **Signal age** — age 0 > age 1.
- SC-2. Each candidate stores the **component breakdown** so the owner can see and challenge the ranking. The score is a convenience; the evidence is the source of truth.
- SC-3. Rank candidates (a) globally and (b) within each strategy. Ties break by level quality, then symbol alphabetical.

### 6.9 `settings.yaml` (defaults)

```yaml
data:
  history_years: 3
  price_adjustment: split_only        # split_only | total_return
  benchmark: SPY
indicators:
  atr_period: 14
  rsi_period: 14
  rsi_overbought: 70
  rsi_oversold: 30
pivots:
  pivot_window: 5
levels:
  zone_atr_mult: 0.25                 # zone half-width in ATRs
  cluster_atr_mult: 0.5
  min_touches_horizontal: 2
  min_touches_diagonal: 2
  trendline_lookback_bars: 250
  trendline_scale: linear             # linear | log
  nearest_levels_per_side: 3
signals:
  full_body_ratio: 0.70
  engulfing_require_opposite_color: false
  hold_test_lookback: 3
  break_prior_side_bars: 3
  approach_atr: 1.0
  max_signal_age_bars: 1
  chase_atr: 1.5
range:
  mid_band: [0.35, 0.65]
scoring:
  weights: {signal: 30, level_quality: 20, confluence: 10, trend_fit: 10, flow: 15, rsi_headroom: 5, entry_quality: 5, age: 5}
report:
  max_watch_items: 30
  chart_bars: 150
```

---

## 7. Strategy Playbook (`strategies.yaml`) — Editable by Design

### 7.1 Requirements

- SP-1. **One file** holds all strategies. It is the owner's playbook and must be easy to read and edit by hand: plain-language keys, comments allowed, **no code changes** needed to add, disable, or tune a strategy.
- SP-2. Technical rules reference only the **named vocabulary** in §6.4 and §7.3 (signals, trend states, `not_extended`, flow alignment). The agent must provide `docs/signals.md` listing every vocabulary item, its meaning, and its parameters.
- SP-3. A strategy holds technical parameters only: `id`, `name`, `enabled`, `bias`, `technical` rules and optional `scoring.weights`. Non-technical playbook content (trade construction, ORATS/fundamental/earnings/macro checklists, profit targets, stop losses, management, position size, completeness `status`) is not part of the config; those keys are rejected by `scanner validate` (removed 2026-10-17).
- SP-4. `scanner validate` lints the file: unknown signal names, invalid trend states, wrong types, duplicate IDs, with line numbers where feasible, and suggestions ("did you mean `support_hold`?").
- SP-5. The file is reloaded every run (no caching of parsed strategies across runs). Other unknown keys are preserved and shown verbatim, never evaluated (the owner may add fields like `notes`).
- SP-6. A strategy can be turned off with `enabled: false` without deleting it.

### 7.2 Strategy entry schema

```yaml
- id: bull_put_spread              # unique, snake_case
  name: Bull Put Spread
  enabled: true
  bias: non_bearish                # bullish | bearish | non_bearish | non_bullish | neutral
  description: "Short-dated premium-selling spread, entered on a support reversal."

  technical:                       # evaluated by the scanner
    trend: [uptrend, range]        # allowed trend states; omit or [any] for no restriction
    signals_all: []                # every listed signal must be true
    signals_any: [support_hold]    # at least one must be true (omit if none)
    not_extended: auto             # auto | none | up | down  (auto: bullish→not extended up, bearish→not extended down, others→none)
    alignment: soft                # soft | required | ignore

  scoring:                         # optional; replaces settings.yaml → scoring.weights keys for this strategy
    weights: { level_quality: 25, entry_quality: 15 }
```

### 7.3 Technical rule semantics

- `trend`: the symbol's trend state (§6.3) must be in the list.
- `signals_all`, `signals_any`: signals from §6.4 evaluated on the latest completed bar (age ≤ `max_signal_age_bars`).
- `not_extended`:
  - `up` → fail if `extended_up`; `down` → fail if `extended_down`; `none` → ignore; `auto` → derived from `bias` (bullish→`up`, bearish→`down`, others→`none`).
- `alignment`: `soft` (label + score), `required` (candidate dropped if `countertrend`), `ignore`.
- Optional per-strategy overrides (nice-to-have): `overrides: {rsi_overbought: 75, full_body_ratio: 0.8}` for any `settings.yaml` value, applied only to that strategy's evaluation.

### 7.4 Candidate-card behavior for display-only fields

Removed 2026-10-17. Cards show technical content only (rules, evidence, score, alert prices). The plan checklists and the `{nearest_support}`/`{nearest_resistance}` plan placeholders no longer exist; the alert helper gives the same level prices.

### 7.5 Scope of seeded strategies

All strategies from the owner's playbook are seeded in Appendix A with technical rules inferred from the technical cues in the playbook. Strategies excluded or limited:

- **TED Calendar:** seeded but `enabled: false` (no technical rules).
- **Flyagonal:** seeded with `enabled: false` (no technical rules).
- All other strategies are enabled. Their fundamental/ORATS/event conditions are outside the scanner and the config.

---

## 8. Historical Replay and Validation

### 8.1 Replay

- RP-1. `--as-of YYYY-MM-DD` runs the full pipeline as if that date were "today": data truncated to bars ≤ that date, pivots/signals computed only from data available then (**no look-ahead**; TE-6 applies). Output goes to `reports/<as-of>/`.
- RP-2. Replay uses cached data when present; if cache lacks the needed history, fetch it. Replay on a non-trading day uses the prior trading day (noted in the report).
- RP-3. `scanner replay --start DATE --end DATE` runs each trading day in the range and writes a **signal log** (`signals_log.csv`): date, symbol, signal, strategy matches, score, level, trigger prices.
- RP-4. *(Should-have)* **Forward outcomes** in the signal log: for each logged signal, N-day forward return and max favorable/adverse excursion in ATRs for N in `[3, 5, 10, 20]` (default configurable). Purely informational to help the owner tune thresholds; clearly labeled as **not a backtest** (no options P&L, no sizing, no costs).
- RP-5. Replay runs must be deterministic: same inputs → same outputs.

### 8.2 Validation against the owner's chart reads (should-have)

- VR-1. `expected_reads.csv` (owner-maintained): `date, symbol, expected` where `expected` is one or more signals/trend states (e.g., `support_hold`, `resistance_break`, `uptrend`, `range`), plus optional `note`.
- VR-2. `scanner check-reads` replays each row's date and compares the scanner's output; prints a table of matches, misses, and extras, with the evidence (levels/bars) so the owner can decide whether the rule or his read should change.
- VR-3. This is the primary tool for tuning `zone_atr_mult`, `pivot_window`, `full_body_ratio`, and related parameters.

---

## 9. Explicit Exclusions (Manual by Design)

The scanner does **not** compute, fetch, or evaluate:

- Fundamentals (P/E, P/B, ROE, analyst ratings, valuation vs. peers)
- Earnings dates or event gating
- Macro calendars or news
- ORATS fields (IV rank, forecasts, ratios, D/F/S edge, etc.)
- Any option chain, strike, delta, credit, or pricing

These are not represented anywhere in the scanner, its config or its output; the owner checks them outside the tool.

---

## 10. Outputs and CLI

### 10.1 HTML Report (primary output)

- OUT-1. A **self-contained single HTML file** per run: `reports/YYYY-MM-DD/scan.html` (as-of date; inline CSS/JS/images; opens offline; no CDN dependencies required).
- OUT-2. Report sections, in order:
  1. **Run summary:** as-of date, bars through date, `provisional` warning if applicable, symbols scanned/skipped (with reasons), strategies enabled/skipped, settings hash, runtime.
  2. **Market context:** SPY trend and key levels; sector-ETF table (trend state of each ETF vs. SPY, 1m/3m/6m returns), covering ETFs referenced by the owner's mapping.
  3. **Ranked shortlist:** sortable and filterable table (client-side JS): rank, symbol, price, strategies matched (chips), primary signal, score, trend, flow alignment, RSI, nearest level + distance (ATR), age, copy-friendly alert level. Filters: by strategy, bias, signal, alignment.
  4. **Candidate cards** (one per symbol with all its matched strategies), see OUT-3.
  5. **Watch list:** near-setups with trigger prices.
  6. **Skipped/diagnostics:** data-quality issues, failed downloads, insufficient-history symbols, disabled strategies.
- OUT-3. **Candidate card contents:**
  - Header: symbol, last close, change, as-of, score, rank.
  - **Chart** (static, embedded as inline SVG or base64 PNG): last `chart_bars` daily candles, SMA50/SMA200, shaded zones for relevant levels, drawn trendlines, pivot markers, signal bar(s) highlighted, RSI subplot with 70/30 lines.
  - **Technical read:** trend state with the pivots used; levels table (type, role, level, zone, touches, confluence sources); signals fired with evidence (bar dates, OHLC, ratios); RSI and extension verdict; relative volume (info).
  - **Relative strength:** SPY trend, sector-vs-SPY, name-vs-sector with component trend states; `with_flow` label.
  - **Strategies matched:** for each, rules evaluated (pass/fail/pending) with evidence.
  - **Alert helper:** a plain-text block of suggested TradingView alert prices (e.g., trigger, invalidation, zone edges), each with a one-line meaning and a copy button.
  - **Score breakdown:** component table.
- OUT-4. Visual design: clean, readable, printable; light/dark friendly; works on desktop and phone-width screens.
- OUT-5. A footer disclaimer: *"Automated technical screening based on the owner's predefined rules; not investment advice. Manual validation is required before any trade."*

### 10.2 Secondary Outputs

- OUT-6. `reports/YYYY-MM-DD/candidates.csv` and `candidates.json` (full audit trail) — should-have, enables replay analysis and future automation.
- OUT-7. `reports/YYYY-MM-DD/tradingview_watchlist.txt` — comma-separated symbols of the ranked shortlist (and a second list for the watch list) that can be pasted into a TradingView watchlist import — nice-to-have.

### 10.3 CLI

```
scanner run                       # full scan as of latest completed bar
scanner run --symbol AAPL         # deep dive on one symbol (all strategies, incl. non-matching rules)
scanner run --strategy bull_put_spread
scanner run --as-of 2026-09-15    # replay a single date
scanner replay --start 2026-06-01 --end 2026-09-30   # signal log over a range
scanner explain --symbol AAPL --strategy bull_put_spread [--as-of DATE]
scanner check-reads               # compare replay vs. expected_reads.csv
scanner validate                  # lint universe, sector map, strategies, settings
scanner cache refresh|info
```

- CLI-1. Non-interactive: no prompts. Exit codes: 0 success, 1 hard failure, 2 completed with data/symbol warnings.
- CLI-2. Deterministic output paths and a machine-readable run manifest (`run.json`: parameters, versions, counts, warnings), so a **scheduler can be added later** (cron, Windows Task Scheduler, GitHub Actions, etc.) without code changes. Scheduling and notifications themselves are out of scope for v1.
- CLI-3. `scanner explain` prints every rule evaluated for a (symbol, strategy) with evidence and why it matched or not.

---

## 11. Non-Functional Requirements

- NF-1. **Language/stack (recommended):** Python ≥ 3.11; pandas, numpy, yfinance, pydantic (config validation), PyYAML, typer (CLI), Jinja2 (HTML), matplotlib or mplfinance (charts → inline SVG/PNG), `pandas_market_calendars` or `exchange_calendars`, pytest, ruff. Choices may be adjusted if the result is simpler.
- NF-2. **Cost:** zero paid services. No API keys required.
- NF-3. **Performance:** ~50 symbols (plus sector ETFs and SPY) completes in **< 2 minutes** warm cache, **< 5 minutes** cold, excluding yfinance throttling. Replay over a 3-month range for the full universe completes in a reasonable time (< 15 minutes warm).
- NF-4. **Determinism & reproducibility:** same data + config → identical results (excluding timestamps). Each run stores a snapshot/hash of settings and strategies with the report.
- NF-5. **Resilience:** per-symbol error isolation; clear summary of failures; safe to re-run.
- NF-6. **Transparency:** every candidate traceable to specific bars, levels, and parameter values.
- NF-7. **Maintainability:** each signal/primitive is a small, separately testable function; adding a new signal requires registering it in one place and documenting it in `docs/signals.md`.
- NF-8. **Documentation:** `README.md` (setup, files, running, interpreting the report), `docs/signals.md` (vocabulary + exact definitions), and a short "how to edit strategies.yaml" guide.
- NF-9. **Timezone:** market dates use US/Eastern; run timestamps shown in the owner's local time (America/Chicago).

---

## 12. Testing & Acceptance

### 12.1 Tests

- T-1. **Primitive unit tests** with hand-built OHLC fixtures with known answers: pivot detection (including no-look-ahead), ATR, RSI (vs. a reference implementation), SMA, horizontal clustering, trendline fitting, trend-state classification, range detection.
- T-2. **Signal tests** covering each definition in §6.4 exactly as written: positive, negative, and edge cases (e.g., break without engulfing = no signal; engulfing but not full-bodied = no signal; hold without prior test = no signal; `PENDING_CLOSE` on provisional bar).
- T-3. **Strategy engine tests:** each seeded strategy has fixtures that match, miss on one rule, and fail on trend; `alignment: required` drops countertrend; `not_extended` behaves per bias.
- T-4. **Config tests:** `scanner validate` catches typos in signal names, bad types, duplicate IDs, missing files; the `.txt` and `.csv` universe formats both load.
- T-5. **Replay tests:** running `--as-of D` equals the result of truncating data at D (no look-ahead); a pivot that would only be confirmed after D never appears.
- T-6. **Data layer tests:** mocked yfinance responses; cache incremental update; split-revision detection; provider failure isolation.
- T-7. **Report tests:** HTML renders without errors for zero candidates, many candidates, and missing sector mapping; file is self-contained (no external requests).

### 12.2 Acceptance criteria (v1 "done")

1. `scanner run` on the owner's universe produces `scan.html` (self-contained) with all sections in OUT-2, run-summary diagnostics, and cards with all items in OUT-3.
2. Levels (SMA50/200, horizontal, diagonal) with ATR zones are drawn on charts and reported in tables; confluence is shown.
3. Trend state uses higher/lower pivot highs and lows exactly as in TE-16 and shows the pivots used.
4. Hold, break (engulfing + full-bodied), engulfing, approaching, range, and RSI-extension signals behave per §6.4 and are unit-tested.
5. Every enabled strategy in `strategies.yaml` is evaluated; editing the YAML (e.g., changing `signals_any`, adding a new strategy, disabling one) changes results with **no code changes**; `scanner validate` catches mistakes.
6. (Removed 2026-10-17: cards no longer show manual checklists.)
7. Alert helper block shows trigger/invalidation prices for each candidate and watch item.
8. `--as-of` replay and `scanner replay` work without look-ahead; `signals_log.csv` is produced.
9. No code path touches ORATS, option chains, fundamentals, earnings, or macro data. (Exception, approved
   2026-10-16: the Streamlit app reads each ticker's name and business summary from yfinance for display only;
   nothing else from that profile is read or evaluated.)
10. `scanner explain` explains any (symbol, strategy) result.
11. Test suite passes.

### 12.3 Milestones

| Phase | Scope |
|---|---|
| **M0 – Foundations** | Repo scaffold, config loading/validation (`universe`, `sector_map`, `settings`, `strategies`), yfinance provider + cache, trading calendar, run manifest. |
| **M1 – Indicators, Pivots, Levels** | ATR/SMA/RSI, pivots, zones, horizontal + diagonal lines, confluence, nearest levels; unit tests. |
| **M2 – Trend, Range, Signals** | Trend state, range, hold/break/engulfing/approaching/extension signals; `explain` for signals; replay `--as-of`. |
| **M3 – Strategy Engine & Scoring** | `strategies.yaml` loader/linter, rule evaluation, flow alignment, scoring, watch list, alert helper. |
| **M4 – HTML Report** | Charts, shortlist table, cards, watch list, diagnostics, CSV/JSON; self-contained. |
| **M5 – Replay & Validation** | `scanner replay`, signal log, forward outcomes, `expected_reads.csv` + `check-reads`; docs. |
| **Later** | Scheduling, notifications (email/push), Pine Script export, optional `manual_levels.yaml` refinements, optional re-introduction of ORATS/fundamental data. |

---

## 13. Open Questions / Assumptions (agent: implement the stated default, keep it configurable, list unresolved items in the README)

| ID | Item | Assumed default |
|---|---|---|
| **OQ-1** | **Break definition.** Owner: "engulfing candle required for a break; the second bar must be full bodied." A bar that closes through support is usually bearish, and a classic bearish engulfing follows a *bullish* bar. | **Decided 2026-10-16 (B5):** 2-bar break — N-1 and N both close beyond the zone; N full-bodied and engulfs N-1 (same colour allowed); the 3 closes before N-1 on the level's side. See §6.4. |
| **OQ-2** | **Zone width.** "Use ATR to determine the zone." | Zone = level ± 0.25 × ATR(14). Tune with replay. |
| **OQ-3** | **Pivot window.** Not specified. | 5 bars each side (lagging confirmation); likely tuned to 3–5. |
| **OQ-4** | **Range definition.** Not specified. | ≥ 2 pivot highs and ≥ 2 pivot lows clustered within 0.5 ATR; price between; middle band for neutral calendars. |
| **OQ-5** | **Trendline construction.** "Approximately connects previous pivots." | Best pivot-pair lines scored by touches/recency; linear scale; 2 pivots minimum (3 preferred). |
| **OQ-6** | **Signal age.** Owner reviews manually after the close. | Report signals on the latest bar; "recent" if age 1; older not reported. |
| **OQ-7** | **Price basis vs. TradingView.** | Split-adjusted, not dividend-adjusted; verify against TradingView on sample symbols. |
| **OQ-8** | **Relative-strength trend on ratio series.** | Same pivot logic applied to ratio of closes. |
| **OQ-9** | **Strategy technical mappings** in Appendix A are inferred from the playbook's technical cues (e.g., Bull Call Spread entry = bullish engulfing or resistance break). | Seeded in `strategies.yaml`; the owner edits freely. |
| **OQ-10** | **PMCC/PMCP/Wheel/Long Stock technical rules.** The playbook leans on fundamentals/ORATS; technical cues are thin. | Trend/hold-based rules in Appendix A; fundamentals/ORATS stay outside the scanner. |

---

## Appendix A — Seed `strategies.yaml`

Seeded from the owner's playbook. Only the technical rules are kept (they reflect OQ-9/OQ-10); the playbook's non-technical text was removed on 2026-10-17.

```yaml
# strategies.yaml — the strategy playbook. Edit freely; run `scanner validate` after changes.
# Evaluated by the scanner:  bias, technical
# Any other key you add (e.g. notes:) is shown on cards as written and never evaluated.

- id: bull_call_spread
  name: Bull Call Spread
  enabled: true
  bias: bullish
  technical:
    trend: [any]
    signals_all: [resistance_break]
    not_extended: auto            # bullish → not extended up (RSI < 70)
    alignment: soft

- id: bear_put_spread
  name: Bear Put Spread
  enabled: true
  bias: bearish
  technical:
    trend: [any]
    signals_all: [support_break]
    not_extended: auto            # bearish → not extended down (RSI > 30)
    alignment: soft

- id: bull_put_spread
  name: Bull Put Spread
  enabled: true
  bias: non_bearish
  technical:
    trend: [uptrend, range]
    signals_any: [support_hold]
    not_extended: none
    alignment: soft

- id: bear_call_spread
  name: Bear Call Spread
  enabled: true
  bias: non_bullish
  technical:
    trend: [downtrend, range]
    signals_any: [resistance_hold]
    not_extended: none
    alignment: soft

- id: neutral_calendar
  name: Neutral Calendar
  enabled: true
  bias: neutral
  technical:
    trend: [range]
    signals_all: [range_bound, range_mid]
    signals_any: []
    not_extended: none
    alignment: ignore

- id: pmcc
  name: PMCC (Poor Man's Covered Call)
  enabled: true
  bias: non_bearish
  technical:
    trend: [uptrend]
    signals_any: [support_hold, bullish_engulfing]
    not_extended: up
    alignment: soft

- id: pmcp
  name: PMCP (Poor Man's Covered Put)
  enabled: true
  bias: non_bullish
  technical:
    trend: [downtrend]
    signals_any: [resistance_hold, bearish_engulfing]
    not_extended: down
    alignment: soft

- id: wheel
  name: Wheel (Cycle-Recycle)
  enabled: true
  bias: non_bearish
  technical:
    trend: [uptrend, range]
    signals_any: [support_hold]
    not_extended: none
    alignment: soft                # owner favors rising relative strength vs sector ETF

- id: ted_calendar
  name: TED Calendar
  enabled: false                   # no technical rules; not evaluated
  bias: neutral
  technical: {}                    # none

- id: flyagonal
  name: Flyagonal
  enabled: false
  bias: neutral
  technical: {}

- id: long_stock
  name: Long Stock
  enabled: true
  bias: bullish
  technical:
    trend: [uptrend]
    signals_any: [support_hold]
    not_extended: up
    alignment: soft                # owner favors rising relative strength vs sector ETF
```

## Appendix B — What Changed vs. v1

- **Removed:** ORATS API, all ORATS-field evaluation, option chains/strikes/credit calculations, fundamentals, earnings gates, macro event file, Pine Script, alert automation.
- **Removed 2026-10-17:** the manual-only text kept in v2.0 (ORATS criteria, fundamental/earnings/macro checks, construction, plan, `status`); the config is technical parameters only.
- **Added:** SMA/pivot-based support & resistance definitions, engulfing + full-body break rule, signal vocabulary, watch list with TradingView alert levels, HTML report with charts, replay + validation against owner's chart reads, editable single-file strategy playbook, scheduler-ready CLI.
