# Options Opportunity Scanner — Requirements

**Status:** Draft v2.0  
**Audience:** Coding agent implementing a local Python tool  
**Primary user:** Trader/end user  
**Primary output:** Ranked HTML report  
**Operating model:** Manual end-of-day scan; human performs all final review and trade execution

---

## 1. Purpose

Build a lightweight, local **technical opportunity scanner** that identifies symbols whose daily price action is consistent with the owner's documented trading approach, then maps those setups to potential options strategies.

The scanner is a **discovery and triage tool**, not an auto-trader, options-pricing engine, fundamentals screener, or event monitor. It should help answer:

1. Which symbols have a potentially actionable technical setup?
2. What evidence supports that setup (trend, support/resistance, confirmation, RSI, relative strength)?
3. Which of the owner's existing strategies are technically compatible with it?
4. What should the owner verify manually in TradingView, the ORATS dashboard, and Google Finance before deciding whether to trade?

### 1.1 Goals

- **G1. Technical discovery:** scan a user-maintained symbol universe using daily OHLCV data from the free `yfinance` Python library.
- **G2. Explainability:** show the exact rules, levels, candles, values, and dates behind each result. Do not present opaque signals.
- **G3. Strategy mapping:** map technical setups to potential strategies using editable configuration files, not hard-coded strategy logic.
- **G4. Human handoff:** produce a useful, ranked HTML report with strategy-specific **manual ORATS checklist** items. The scanner does not evaluate these items.
- **G5. Low cost and simplicity:** no paid API dependency and no additional subscription. Use the owner's existing TradingView and ORATS dashboard manually.
- **G6. Historical replay:** support rerunning the technical rules as of a past date for comparing signals with historical charts.
- **G7. Controlled extensibility:** keep the universe, sector ETF mapping, technical thresholds, ranking weights, and strategy definitions easy to edit.

### 1.2 Non-goals

The scanner must **not**:

- Place orders, connect to a broker, or manage open positions.
- Connect to the ORATS API or retrieve option chains, option quotes, Greeks, IV, IV rank, volatility forecasts, or option liquidity.
- Retrieve or evaluate fundamentals, valuation, analyst ratings, earnings dates, news, litigation, macro calendars, or economic events.
- Automatically configure TradingView alerts, call TradingView APIs, or generate Pine Script in v1.
- Select current option strikes/expirations or calculate candidate-specific credits, debits, max profit/loss, breakevens, or position sizes.
- Claim that historical replay is a backtest or that historical prices are point-in-time vendor snapshots.

The HTML report may display static strategy construction notes and static ORATS checklist thresholds from configuration. Those are reference material only; they are **not live option/fundamental/event data** and must not be described as checked or passed.

---

## 2. Tools, Data, and System Boundaries

### 2.1 Data sources

| Need | Source | Use |
|---|---|---|
| Daily price and volume history | `yfinance` | OHLCV, SMA, ATR, RSI, pivots, trendlines, confirmations, range classification, relative strength |
| Symbol universe | Owner-maintained CSV or TXT file | Source of symbols to scan |
| Symbol-to-sector ETF mapping | Owner-maintained config file | Relative-strength comparisons |
| Strategy definitions and manual ORATS checks | Local YAML files | Editable matching rules and report checklists |
| Chart review | TradingView, manually | Out of scope for application integration |
| Volatility/options review | ORATS dashboard, manually | Checklist displayed in report; no application/API access |
| Macro/fundamental research | Google Finance and other owner-selected resources, manually | Out of scope for application integration |

### 2.2 Data handling

- Fetch at least **three years of daily bars** by default. This allows warm-up for 200-day SMA, six-month relative strength, pivot confirmation, and range detection. Make history length configurable.
- Use one consistent, split-adjusted OHLC series for all price-based calculations. Prefer `yfinance`'s adjusted OHLC output (`auto_adjust=True`) and document the choice.
- Use completed daily bars by default. The normal run is intended to be performed after the US market close. If the latest bar is potentially incomplete, flag the run and affected signals as provisional; do not silently treat an incomplete daily candle as confirmed.
- Fetch the benchmark and any sector ETFs required by the mapping even when they are not present in the scan universe.
- Do not request fundamentals or options data from `yfinance`. Do not scrape TradingView, ORATS, or Google Finance.
- A failed download for one symbol must not abort the full scan. Record the symbol and reason in the run summary.
- Handle Yahoo/yfinance throttling with sensible retry/backoff and local caching. A `--refresh` option should bypass cached price data.
- Because Yahoo historical data can be revised and adjusted retrospectively, replay is **best-effort technical replay**, not a guaranteed point-in-time reconstruction.

### 2.3 Cost and privacy

- No paid service or API key is required.
- No credentials or broker/account information are needed.
- Keep cached data and generated reports local by default.

---

## 3. Run Modes and CLI

Provide a simple Python CLI, for example `scanner` (installed entry point) or `python -m scanner`.

| Command | Required behavior |
|---|---|
| `scanner run` | Scan the configured universe and generate the dated HTML report. |
| `scanner run --symbol AAPL` | Evaluate one symbol and all enabled strategies. |
| `scanner run --strategy bull_put_spread` | Scan all symbols for one strategy. |
| `scanner run --as-of 2026-09-15` | Replay the scan using bars no later than the requested date. |
| `scanner run --show-watch` | Include unconfirmed setups approaching a relevant level. Watch setups may also be included by default if clearly separated; this flag must at least guarantee they are included. |
| `scanner run --refresh` | Bypass local price cache and refetch price history. |
| `scanner validate` | Validate universe, sector mapping, global settings, and strategy YAML files. |

No scheduler is required in v1. Runs are initiated manually. The design should not prevent later scheduling, but do not implement a scheduler now.

### 3.1 As-of replay semantics

- `--as-of YYYY-MM-DD` means: all calculations must use daily bars dated on or before that date, and the report must identify that date as the analysis date.
- Do not use bars after the as-of date to determine pivots, trendlines, confirmations, or indicators.
- A pivot requires bars on its right side to be confirmed. In a replay, a pivot is only known once the requisite right-side bars have occurred on or before the as-of date. Do not backdate the signal to a date when it would not yet have been knowable.
- Clearly distinguish the **pivot's occurrence date** from the **date on which that pivot became confirmed**.
- Replay results can vary if the upstream historical price vendor revises adjusted data. Document this limitation in the README and report.

---

## 4. Configuration and Repository Layout

Keep owner-edited settings outside the Python implementation wherever practical.

Recommended layout:

```text
config/
  universe.csv             # preferred universe format
  symbols.txt              # optional alternative: one symbol per line
  sector_map.csv           # symbol -> sector ETF mapping, if not in universe.csv
  settings.yaml            # global thresholds and ranking weights
  strategies/
    bull_call_spread.yaml
    bear_put_spread.yaml
    bull_put_spread.yaml
    bear_call_spread.yaml
    neutral_calendar.yaml
    pmcc.yaml
    pmcp.yaml
    wheel.yaml
    ted_calendar.yaml
    flyagonal.yaml
    long_stock.yaml
reports/
  YYYY-MM-DD/
    scan.html
cache/                      # local price cache; can be regenerated
src/scanner/
tests/
README.md
```

### 4.1 Universe file

Accept either:

**Preferred CSV** (`config/universe.csv`):

```csv
symbol,asset_type,sector_etf,enabled,tags
AAPL,stock,XLK,true,large-cap
XLY,etf,,true,sector
SPY,etf,,true,benchmark
```

- Required: `symbol`.
- Optional: `asset_type` (`stock` or `etf`), `sector_etf`, `enabled`, `tags`, `notes`.
- `enabled` defaults to true when omitted.
- A `sector_etf` value may be provided directly in this file. If it is absent, look for the symbol in `sector_map.csv`.
- Ignore blank rows and lines beginning with `#` where the format permits comments.
- Normalize symbols to uppercase and de-duplicate them with a validation warning.

**Alternative TXT** (`config/symbols.txt`): one ticker per line. In this mode, read optional sector mappings from `sector_map.csv`.

### 4.2 Sector mapping

`config/sector_map.csv` should contain at minimum:

```csv
symbol,sector_etf
AAPL,XLK
COST,XLP
DAL,XLI
```

The owner maintains this mapping. Do not infer a mapping from a live fundamentals provider. Missing mappings must not block price/technical analysis; mark the relevant relative-strength comparisons `N/A` and explain why.

### 4.3 Global settings

`settings.yaml` should include the values listed in §6 and ranking weights in §8. Use typed validation (Pydantic or equivalent). Invalid configuration must report the file, key, expected type, and invalid value.

### 4.4 Strategy configuration

Each strategy YAML should separate:

- identity and status (`id`, `name`, `enabled`, `status`);
- technical bias and trigger rules;
- static construction reference (for human context only);
- manual ORATS checklist items;
- human-review notes and incomplete-definition warnings.

An owner should be able to enable/disable a strategy, change supported thresholds, edit checklist text, or change supported technical triggers without changing Python code. If a YAML uses an unsupported rule primitive, `scanner validate` must report it explicitly rather than silently ignoring it.

---

## 5. Analysis Pipeline

```text
Load and validate config
        |
        v
Download/load daily OHLCV for universe + required benchmarks/sector ETFs
        |
        v
Validate history and bar completeness
        |
        v
Calculate indicators, confirmed pivots, S/R zones, trendlines, range/trend state
        |
        v
Evaluate hold/break confirmations and RSI extension
        |
        v
Calculate name-vs-sector and sector-vs-SPY relative strength
        |
        v
Create technical setups + watch setups
        |
        v
Match setups to enabled strategy YAML rules
        |
        v
Rank and render HTML candidate cards, watchlist, and run summary
```

Do not add a hidden fundamentals/events/options stage. The scanner must be deterministic and explainable from price history plus configuration.

---

## 6. Technical Analysis Requirements

Daily bars are the only timeframe used by the scanner. Indicators and rules must be described in `docs/rules.md` with examples and tests.

### 6.1 Supported technical references

Potential support/resistance sources are limited to:

1. **50-day simple moving average (SMA50)**.
2. **200-day simple moving average (SMA200)**.
3. **Horizontal levels** derived from repeated confirmed swing-high or swing-low pivots.
4. **Diagonal trendlines** connecting confirmed swing-high pivots or confirmed swing-low pivots.

Do not add other indicators or pattern families unless separately approved. No Fibonacci, MACD, Bollinger Bands, candlestick catalog, machine learning, or multi-timeframe analysis in v1.

### 6.2 Swing pivots

Use a configurable symmetric pivot window; default **5 bars on each side**.

- A pivot high occurs when its high is greater than or equal to the highs in the configured number of bars on both sides, subject to a deterministic tie-breaking rule.
- A pivot low occurs when its low is less than or equal to the lows in the configured number of bars on both sides, subject to a deterministic tie-breaking rule.
- A pivot is usable only after all required right-side bars have closed.
- Store the pivot's occurrence date, confirmation date, price, and type (`high`/`low`).
- Handle equal highs/lows and missing bars consistently; document the tie-breaking rule and test it.

### 6.3 Support/resistance zones

#### 6.3.1 SMA levels

- Calculate SMA50 and SMA200 from the selected adjusted close series.
- Treat the current SMA value as a candidate level, not as an unconditional buy/sell signal.
- Use ATR to create an approximate zone around the SMA. Default zone half-width: **0.5 × ATR(14)** on either side of the level.
- Label each SMA level and show its value, zone, slope over the configured lookback, distance from current close, and distance in ATR units.

#### 6.3.2 Horizontal pivot levels

- Cluster confirmed pivot highs with other nearby pivot highs, and pivot lows with nearby pivot lows.
- Default clustering tolerance: **0.5 × ATR(14)** at the time of the current analysis. Keep tolerance configurable.
- A horizontal pivot level should normally have at least two touches to be considered a repeatedly observed level. A one-pivot level may be shown as a weaker reference but must be labelled accordingly.
- Report zone low/high, representative level price, touch count, most recent touch date, and the pivot dates contributing to the level.
- Avoid treating the same pivot as multiple independent touches.

#### 6.3.3 Diagonal trendlines

- Construct candidate trendlines from at least two confirmed pivot lows or two confirmed pivot highs.
- A low-pivot line is a potential support trendline; a high-pivot line is a potential resistance trendline.
- Prefer recent confirmed pivots. A line must not use a pivot that had not been confirmed as of the report's analysis date.
- Extend the line to the analysis date and calculate its current projected value and slope.
- Use the configured ATR tolerance to define the trendline zone and to determine whether price has touched or approached it.
- Keep fitting simple and inspectable: use the line through selected pivots or a basic linear fit, as declared in configuration. Do not silently switch methods between symbols.
- Display pivot anchors, line slope, projected value, zone, touch count, and distance from close.

#### 6.3.4 Level selection and approach

- Show the nearest three support references and three resistance references by default; `N` is configurable.
- Rank levels using recency, number of independent touches, and proximity. Display the ranking inputs.
- A level is `approaching` when the distance from close to its zone is at most **1.0 × ATR(14)** by default.
- For level-specific hold or break signals, preserve the specific level and zone used in the report. Do not substitute a different nearby level after evaluating the signal.

### 6.4 Trend state

Trend classification is based on confirmed pivot highs/lows, not on moving-average crossovers.

- **Uptrend:** the two most recent confirmed pivot highs are successively higher **and** the two most recent confirmed pivot lows are successively higher.
- **Downtrend:** the two most recent confirmed pivot highs are successively lower **and** the two most recent confirmed pivot lows are successively lower.
- **Range/neutral:** price is oscillating between identifiable support/resistance levels, meets the range criteria in §6.5, and neither directional trend rule is met.
- **Mixed/unclear:** insufficient pivots or conflicting structure that meets neither trend definition nor range definition.

Report the pivots that determined the classification. Do not infer a trend from SMA50/SMA200 alone.

### 6.5 Trading range detection

Use a default lookback of **60 completed daily bars**, configurable.

Flag `range_confirmed` when all are true:

1. A plausible horizontal support zone and horizontal resistance zone can be identified within the lookback.
2. Each boundary has at least two independent pivot/touch observations in the lookback.
3. Price has traded between the boundaries and there has not been a confirmed breakout under §6.7.
4. The latest price is not so far beyond either boundary that the boundaries no longer describe the active range.

The exact touch tolerance and lookback are configurable. Show the upper/lower levels, range width in dollars and percentage, number of touches per boundary, and lookback dates. If the data only weakly fits a range, label it `possible_range` rather than confirmed.

### 6.6 RSI and directional extension

- Calculate RSI with **period 14 and Wilder smoothing**.
- `extended_up = RSI >= 70`.
- `extended_down = RSI <= 30`.
- These thresholds are configurable globally and may be overridden by a strategy.
- Bullish strategies that require the price not to be extended must not qualify when `extended_up` is true.
- Bearish strategies that require the price not to be extended must not qualify when `extended_down` is true.
- Neutral strategies may still qualify when RSI is overbought/oversold unless their strategy configuration says otherwise.
- Display the RSI value, the applicable threshold, the extension state, and distance from the threshold. Do not add a separate moving-average-distance extension filter in v1.

### 6.7 Support/resistance confirmation logic

Evaluate only completed daily bars. Every result must include the exact zone, bar dates, bar OHLC values, and a status: `CONFIRMED`, `NOT_CONFIRMED`, `PENDING_CLOSE`, or `INSUFFICIENT_DATA`.

#### 6.7.1 One-bar hold confirmation

**Support hold:** a bar tests/overlaps the support zone, followed by a completed response bar that closes above the immediately preceding bar's high.

**Resistance hold:** a bar tests/overlaps the resistance zone, followed by a completed response bar that closes below the immediately preceding bar's low.

- The test bar and response bar must be identified explicitly.
- The response must occur after the test; a bar cannot confirm its own hold.
- Default setup detection uses the most recent qualifying test/response sequence. The search-back window is configurable (default 3 bars before the response bar).
- The primary hold definition is the close beyond the prior bar's high/low stated above. Do not require additional candle patterns for a hold unless the strategy YAML explicitly adds one.

#### 6.7.2 Two-bar breakout/breakdown confirmation

A break of a level requires:

1. Two consecutive completed daily closes beyond the relevant edge of the zone (above the upper edge for a resistance break; below the lower edge for a support break).
2. A directionally aligned engulfing candle on the latest bar in the two-close confirmation sequence.
3. The engulfing candle's real body must be **full-bodied**, defined by default as `abs(close - open) / (high - low) >= 0.70`. Make the ratio configurable. If `high == low`, the ratio is undefined and the candle cannot qualify as full-bodied.

The engulfing rule is defined in §6.8. For a bullish break, the latest candle must be a bullish engulfing candle; for a bearish break, it must be a bearish engulfing candle. Because this is a deliberately strict rule, the report must explain when a two-close break exists but fails the engulfing/full-body requirement.

Do not treat a single close outside a zone as a confirmed break. A single close outside a zone may create a watch item labelled `BREAK_PENDING_SECOND_CLOSE`.

### 6.8 Engulfing candle definition

Use conventional **real-body engulfing**, not full-range engulfing:

- **Bullish engulfing:** the prior candle is bearish (`prior close < prior open`); the current candle is bullish (`close > open`); the current real body fully contains the prior real body; and the current candle meets the configured full-body ratio when the rule requires a full-bodied candle.
- **Bearish engulfing:** the prior candle is bullish (`prior close > prior open`); the current candle is bearish (`close < open`); the current real body fully contains the prior real body; and the current candle meets the configured full-body ratio when the rule requires a full-bodied candle.
- Equality rules for open/close boundaries must be deterministic and tested.
- Output whether an engulfing pattern exists, direction, body-to-range ratio, the two candles' dates/OHLC, and which requirement passed or failed.

For the breakout rule, a matching engulfing pattern and the full-body ratio are required on the latest bar. Standalone strategy rules may require an engulfing pattern without a breakout; they should still use the same body-engulfing definition, but do not require the 0.70 ratio unless the strategy config requests a full-bodied candle.

### 6.9 Relative strength and market context

The scanner should show relative performance of:

1. The symbol versus its mapped sector ETF.
2. The sector ETF versus SPY.
3. SPY's own pivot-based trend state.

Do not use external sector classifications; use the mapping supplied by the owner.

For the symbol/sector ETF and sector ETF/SPY pairs:

- Calculate the relative-price ratio series using adjusted close prices.
- Display relative returns for approximately 1 month (21 sessions), 3 months (63 sessions), and 6 months (126 sessions).
- Classify the ratio trend transparently using the ratio versus its 20-session SMA and whether that SMA is rising/falling over a configurable lookback. Default rule: `rising` when the ratio is above its 20-session SMA and the SMA is rising; `falling` when the ratio is below its 20-session SMA and the SMA is falling; otherwise `mixed/flat`.
- If required history or a sector mapping is missing, report `N/A`; do not invent a mapping or treat missing data as neutral strength.
- For ETFs without a meaningful mapped sector ETF, symbol-vs-sector may be `N/A`; sector-vs-SPY remains available when the ETF itself is the relevant sector reference.

Compute an alignment label for each directional candidate:

- `WITH_FLOW`: directional thesis agrees with both symbol-vs-sector and sector-vs-SPY trends, where both are available.
- `MIXED`: the components disagree, or one component is flat/mixed.
- `COUNTERTREND`: both comparisons oppose the candidate's directional thesis.
- `PARTIAL`: one comparison is unavailable; classify using the available component and state what is missing.

Alignment affects ranking by default; it is not a hard gate unless a strategy explicitly sets `alignment_required: true`. Report all components separately. For neutral/range strategies, show relative strength for context but do not force a bullish/bearish alignment label.

---

## 7. Technical Setups and Strategy Matching

The strategy matcher evaluates **technical criteria only**. ORATS checklist items and static construction notes do not affect whether a candidate is emitted or its rank.

The matcher should create reusable setup labels, which strategy YAML files can reference. Suggested supported labels:

- `bullish_engulfing`
- `bearish_engulfing`
- `resistance_break_up_confirmed`
- `support_break_down_confirmed`
- `support_hold_confirmed`
- `resistance_hold_confirmed`
- `uptrend`
- `downtrend`
- `range_confirmed`
- `approaching_support`
- `approaching_resistance`
- `not_extended_up`
- `not_extended_down`
- `relative_strength_rising`
- `relative_strength_falling`

A strategy can require one or more of these using `all`, `any`, and `not` combinations. Keep the expression schema simple and safe; do not use Python `eval` on configuration expressions.

### 7.1 Default technical mapping for the strategy set

These are initial defaults derived from the owner's existing strategy notes. Keep them editable in each YAML file.

| Strategy | Default technical trigger(s) | RSI extension rule | Notes |
|---|---|---|---|
| Bull Call Spread | Bullish engulfing **or** confirmed resistance break up | Must not be `extended_up` | Directional bullish debit spread |
| Bear Put Spread | Bearish engulfing **or** confirmed support break down | Must not be `extended_down` | Directional bearish debit spread |
| Bull Put Spread | Confirmed support hold | No `extended_up` by default; configurable | Non-bearish credit spread at support |
| Bear Call Spread | Confirmed resistance hold | No `extended_down` by default; configurable | Non-bullish credit spread at resistance |
| Neutral Calendar | Confirmed range, with no confirmed breakout in the latest setup | None by default | Strategy can be considered only when range classification is sufficiently clear |
| PMCC | Uptrend **or** confirmed support hold; rising symbol-vs-sector strength is preferred | Must not be `extended_up` by default | Non-bearish, investment-style structure; no fundamental screening in this tool |
| PMCP | Downtrend **or** confirmed resistance hold; falling symbol-vs-sector strength is preferred | Must not be `extended_down` by default | Non-bullish structure; no fundamental screening in this tool |
| Wheel | Uptrend **or** confirmed support hold; rising symbol-vs-sector strength preferred | Must not be `extended_up` by default | Display the preferred technical context; no fundamental screening in this tool |
| TED Calendar | Neutral/range context only | None | Event eligibility is not checked; must be clearly labelled as requiring independent manual event validation before use |
| Flyagonal | No verified trigger in the source strategy notes | Not defined | Include an editable stub, disabled until the owner defines a technical trigger |
| Long Stock | Uptrend **or** confirmed support hold, preferably with rising symbol-vs-sector strength | Must not be `extended_up` by default | Technical candidate only; no valuation/fundamental gate |

Interpretation rules:

- “Or” conditions must be displayed as separate reasons, not combined into a vague label.
- A strategy with missing required technical data cannot pass its condition. Report `INSUFFICIENT_DATA` and the reason.
- Missing relative-strength mapping does not block a strategy unless that strategy explicitly requires relative strength; otherwise it is informational and ranking may omit that component.
- A neutral/range setup must not be relabelled bullish or bearish just to create strategy matches.
- For the TED Calendar, the scanner may show technical compatibility but must prominently state that the tool does not identify or validate an earnings event. Do not fetch earnings data.
- Keep Flyagonal visible in the configuration and run summary, but do not emit it as a qualified candidate until it has an owner-approved technical trigger.

---

## 8. Ranking and Candidate Status

### 8.1 Candidate versus watch

- **Confirmed candidate:** at least one enabled strategy's technical entry rules pass.
- **Watch:** price is approaching a relevant S/R zone or a partial confirmation exists, but no enabled strategy's full technical trigger passes.
- **Not matched:** no strategy trigger passes and no meaningful watch condition exists; such symbols appear only in the run summary unless `--show-all` is added later.
- Do not use ORATS/manual checklist state to promote, demote, suppress, or rank candidates.

### 8.2 Ranking score

Use a transparent, configurable 0–100 score. The default score should prioritize a clear technical setup and then use trend/relative strength as supporting context.

Suggested default weights:

| Component | Weight |
|---|---:|
| Technical trigger quality and confirmation recency | 25 |
| Alignment with pivot-based trend state | 20 |
| Symbol versus sector ETF relative strength | 15 |
| Sector ETF versus SPY relative strength | 15 |
| Quality of S/R reference (touch count and recency) | 10 |
| Proximity to relevant zone in ATR units, where appropriate to the setup | 10 |
| RSI headroom before extension threshold | 5 |
| **Total** | **100** |

Implementation notes:

- The weights must be configurable and validated to sum to 100 (or normalized consistently if the implementation intentionally supports weights that do not sum to 100).
- Proximity scoring must depend on setup type: approaching a level is useful for a hold/watch setup, while a breakout score should reward a recent confirmed break rather than penalize price for moving away from the zone.
- If a component is unavailable, mark it `N/A` and renormalize the score across the available components. Show the score components so the rank can be audited.
- A `COUNTERTREND` setup can still appear, but receives a lower alignment contribution unless the strategy config says alignment is required.
- The score is a sorting aid, not a probability of profit or recommendation.

---

## 9. Manual ORATS Checklist and Strategy Reference

The ORATS dashboard remains a manual validation step. The scanner must not access ORATS or calculate any ORATS values.

### 9.1 Checklist behavior

- Each strategy YAML may provide a list of static manual checks.
- Render them on a matched strategy's HTML card as unchecked items with threshold/target text.
- Label the section **“Manual ORATS review — not evaluated by scanner.”**
- Do not show `pass`, `fail`, or `near miss` for these items. A printed threshold is a reminder, not evidence that current ORATS data meets it.
- Preserve `D/F/S` as a manual edge check where specified: **D = Distribution (Monte Carlo), F = Forecast (volatility forecast), S = Smooth-market volatilities (theoretical pricing)**. No calculations are performed by this tool.
- Preserve `wld` as **win/lose/draw** in any applicable static strategy management note; the scanner does not evaluate it.
- Since fundamentals, earnings, macro events, and live options data are explicitly out of scope, do not include their data, filters, or calculations in the scanner. In particular, omit fundamental metrics, earnings gates, and macro-event scanning from YAML matching. Strategy notes that inherently depend on events must be labelled for independent manual verification.

### 9.2 Default manual ORATS checklist

Use the following volatility/pricing checks transcribed from the owner's existing strategy criteria, omitting criteria that require the excluded fundamental, earnings, event, or automatic price-momentum screens. Store every line in the corresponding strategy YAML so the owner can edit it.

| Strategy | Default manual ORATS checks |
|---|---|
| Bull Call Spread | IV Rank (1 year) `< 65` (avoid buying expensive premium) |
| Bear Put Spread | IV Rank (1 year) `< 65` |
| Bull Put Spread | IV Rank (1 year) `> 50`; `ivHvXernRatio > 1`; `orFcst20d / orIvXern20d < 1`; ORATS `confidence > 80` |
| Bear Call Spread | IV Rank (1 year) `> 50`; `ivHvXernRatio > 1`; `orFcst20d / orIvXern20d < 1`; ORATS `confidence > 80` |
| Neutral Calendar | `exErnIv20d / fexErn30_20 > 1`; `orFcst20d / orIvXern20d < 1`; `10 < ivRank1y < 60`; `avgOptVolu20d > 1000`; ORATS `confidence > 80` |
| PMCC | `exErnIv30d / fexErn90_30 > 1`; `10 < ivRank1y < 60`; ORATS `confidence > 80`; manually confirm D/F/S edge |
| PMCP | `exErnIv30d / fexErn90_30 > 1`; manually confirm D/F/S edge |
| Wheel | `ivRank1y > 35`; `orFcst20d / orIvXern20d < 1.02`; `orIvFcst20d / orIvXern20d < 1.02`; `orHvXern20d / orIvXern20d < 1.02`; ORATS `confidence > 70`; manually confirm D/F/S edge |
| TED Calendar | No criteria were defined in the source notes; checklist should state **“ORATS criteria not defined — manual review required.”** Also state the scanner does not identify earnings events. |
| Flyagonal | No criteria were defined; status stub |
| Long Stock | No ORATS volatility check defined in the source notes |

Checklist labels are static strategy references and may reflect the owner's original terminology. Do not make ORATS field calls or infer that these thresholds are available in an API.

### 9.3 Static strategy construction and management reference

Each YAML may also preserve the following as read-only context on the report card. The scanner does **not** construct a live trade or decide whether a specific listed contract satisfies it.

| Strategy | Static construction reference | Existing management notes to display |
|---|---|---|
| Bull Call Spread | Buy 0.4–0.6 delta call, 10–45 DTE; sell call 1–4 strikes higher, same expiration | Historical notes: profit target 10–200% of debit; stop 10–50%; exit on a two-bar close below pre-defined support; exit no later than Wednesday before expiration |
| Bear Put Spread | Buy 0.4–0.6 delta put, 10–45 DTE; sell put 1–4 strikes lower, same expiration | Historical notes: profit target 10–200% of debit; stop 10–50%; exit on a two-bar close above pre-defined resistance; exit no later than Wednesday before expiration |
| Bull Put Spread | Sell approximately 0.3 delta put, 5–20 DTE; buy put one strike lower, same expiration; target credit near one-third of width | Historical notes: target 30–75% of credit; stop 35–100% of credit; exit on two-bar close below short strike; exit no later than Wednesday before expiration; time-based exits (`wld`) |
| Bear Call Spread | Sell approximately 0.3 delta call, 5–20 DTE; buy call one strike higher, same expiration; target credit near one-third of width | Historical notes: target 30–75% of credit; stop 35–100% of credit; exit on two-bar close above short strike; exit no later than Wednesday before expiration; time-based exits (`wld`) |
| Neutral Calendar | Sell a 0.45–0.55 delta put or call, 10–30 DTE; buy same strike/right with later expiration, historically specified as about +7 DTE | Historical notes: target 20–100%; stop 10–50%; time-based exit; exit on two-bar close through the pre-defined support/resistance level |
| PMCC | Long call approximately 0.5–0.6 delta, around 60 DTE; short call 7–14 DTE, same or slightly further OTM strike | Historical notes: manage short call around 50–75% of its maximum premium profit; close the full position at 15–35% profit on initial debit; stop 20–50%; roll short call no later than Wednesday of expiration week |
| PMCP | Long put approximately 0.5–0.6 delta, around 60 DTE; short put slightly further OTM, 7–14 DTE | Historical notes: roll short put around 50–75% of its maximum premium profit; close full position at 15–35% profit on initial debit; stop 20–50%; roll no later than Wednesday of expiration week |
| Wheel | Sell a cash-secured 0.3–0.5 delta put, 7–21 DTE; source strategy also specifies a protective put 30–60 DTE. If assigned, sell a 0.3–0.5 delta call, 7–21 DTE | Display the strategy's mandatory human choice: `Skate (collect premium)` or `Trade (assignment intended)`. Historical exit notes differ by objective; scanner does not calculate collateral or option risk. |
| TED Calendar | Sell a roughly 0.5 delta put for the expiration after earnings; buy same-strike put for following expiration | Source note: sell the day after earnings. Not actionable from scanner output without independent event/contract review. |
| Flyagonal | Not sufficiently specified | Display as an incomplete strategy stub only |
| Long Stock | Buy shares outright | Owner-defined management. Technical scanner does not provide valuation or position sizing. |

Static management notes are not trade advice and are not evaluated against market data. Preserve them in configuration so the owner can amend them without editing Python.

---

## 10. HTML Report Requirements

Generate `reports/YYYY-MM-DD/scan.html` and, for convenience, a `latest.html` copy or pointer. Use a standalone HTML report with minimal CSS; it should open locally without a server or external dependencies.

### 10.1 Report structure

1. **Header and run metadata:** report date, as-of date, data source, history range, settings/config version or hash, and whether the last bar was considered complete.
2. **Run summary:** symbols requested, symbols successfully analyzed, symbols skipped and reasons, confirmed candidate count, watch count, and enabled/disabled/stub strategies.
3. **Market context:** SPY pivot-based trend, SPY close, RSI, nearest relevant S/R zones, and date of analysis. Do not add macro or breadth data that requires a separate source.
4. **Ranked confirmed candidates:** grouped primarily by rank, with all matching strategies shown on each symbol card. Also allow filtering/grouping by strategy if uncomplicated.
5. **Watchlist:** approaching S/R zones, a single close outside a zone awaiting confirmation, or a hold/break attempt missing a stated rule.
6. **Strategy stubs and limitations:** show disabled or incomplete strategy definitions and why they were not matched.
7. **Run diagnostics:** missing mappings/history, data errors, and notable assumptions.

### 10.2 Candidate card contents

Every confirmed candidate card should include:

- Symbol, latest close, analysis date, rank, score, and candidate status.
- One or more matching strategies and a plain-English explanation for each technical match.
- **Trend structure:** trend state and the pivot highs/lows supporting it, including dates and prices.
- **Support/resistance:** relevant SMA, horizontal levels, or diagonal trendlines; zone bounds; touch count; current distance in dollars, percentage, and ATR units; source/anchor pivots; approach status.
- **Confirmation details:** hold or break status, exact level and zone used, involved bar dates/OHLC, close conditions, engulfing result, and full-body ratio when relevant.
- **RSI:** current value, 70/30 state, whether an extension rule blocks a strategy, and distance from the applicable threshold.
- **Relative strength:** symbol-vs-sector and sector-vs-SPY trend labels, 1m/3m/6m relative returns, SPY trend, and `WITH_FLOW`/`MIXED`/`COUNTERTREND`/`PARTIAL` alignment where applicable.
- **Scoring explanation:** component values, weights, missing components, and normalized total score.
- **Static strategy reference:** construction summary and management notes, clearly marked as static reference, not a built trade.
- **Manual ORATS review:** unchecked checklist from §9.2; clearly labelled as not evaluated by scanner.
- **Warnings/unknowns:** missing history, missing sector mapping, incomplete strategy definition, and any requirement needing manual review.

### 10.3 Watch card contents

Each watch item should include:

- Symbol, close, relevant level/zone, distance in ATR units, and reason it is a watch.
- The unmet condition, such as “second close beyond resistance not yet observed” or “support test occurred but response bar did not close above previous bar's high.”
- Relevant technical indicator values and possible strategy families, without presenting it as a confirmed candidate.

### 10.4 Report interaction and links

- Keep the report readable on a standard desktop screen and usable on a narrow screen.
- Optional client-side filtering by symbol, strategy, and status is acceptable but not required.
- A static link to a TradingView chart may be included if generated reliably from the symbol, but there is no requirement for live TradingView integration.
- The report must work offline after generation. No external JavaScript libraries are required.

### 10.5 Disclaimer

Embed this disclaimer in every report:

> This report is automated decision support derived from historical daily price data and the owner's configured rules. It is not investment advice and does not evaluate fundamentals, events, earnings, or current option prices/volatility. Manually verify all chart details and ORATS criteria before making a trading decision. The report does not estimate or guarantee trade outcomes.

---

## 11. Technical Design and Suggested Stack

Use a small, maintainable Python implementation (Python 3.11+ recommended).

Suggested dependencies:

- `yfinance` for daily OHLCV
- `pandas` and `numpy` for calculations
- `pydantic` (or equivalent) for config validation
- `PyYAML` for YAML strategy/settings files
- `Jinja2` for HTML templating
- `pytest` for tests
- Optional: `diskcache`, SQLite, or Parquet for local caching
- Optional: `typer` for the CLI

Suggested structure:

```text
src/scanner/
  cli.py
  config.py
  data/
    prices_yf.py
    cache.py
  technicals/
    indicators.py       # SMA, ATR, RSI
    pivots.py
    levels.py
    trendlines.py
    trend.py
    ranges.py
    candles.py
    confirmation.py
  relative_strength.py
  strategies/
    loader.py
    matcher.py
    models.py
  ranking.py
  reporting/
    html.py
    templates/
      report.html.j2
tests/
config/
reports/
cache/
docs/
  rules.md
README.md
```

Keep calculation functions separate from report rendering. A candidate should be representable as structured data before it is rendered to HTML. Use typed models for levels, pivots, confirmations, technical setups, strategy matches, scores, and run diagnostics.

---

## 12. Reliability, Determinism, and Documentation

- **Per-symbol error isolation:** one ticker failing must not fail the run.
- **No silent omissions:** every skip or unknown should have a reason in diagnostics.
- **Determinism:** identical bars, config, and software version should yield identical calculations and ranking, aside from report timestamps and upstream data changes.
- **Local reproducibility:** save the configuration snapshot or its hash with each report. Save enough metadata to identify the date range and exact as-of date used.
- **Caching:** cache daily price data locally to reduce repeat downloads and avoid unnecessary Yahoo requests. Document cache invalidation and implement `--refresh`.
- **No lookahead leakage:** pivot/line/confirmation logic may only use data known by the analysis date, including in historical replay.
- **No network in reporting:** rendering the HTML report must not require network access.
- **Documentation:** README must explain installation, configuration, running a normal scan, replay mode, cache refresh, and known limitations. `docs/rules.md` must give exact definitions and worked examples for all technical rules.
- **Logging:** provide concise normal output and a verbose/debug mode. Do not log unrelated data or personal information.

---

## 13. Tests and Acceptance Criteria

### 13.1 Unit tests

Write fixture-based tests with known daily OHLCV outcomes for:

- RSI(14) using Wilder smoothing and boundary conditions.
- SMA and ATR calculations.
- Pivot high/low identification, tie handling, and confirmation date.
- Horizontal pivot clustering and touch counts.
- SMA and trendline zone calculations.
- Trendline anchors, projected values, and prevention of future-pivot leakage.
- Higher-high/higher-low uptrend and lower-high/lower-low downtrend rules.
- Range detection, including a range with a false single-bar excursion and a confirmed breakout.
- Support/resistance one-bar hold rules.
- Two-close breakout/breakdown and the engulfing/full-body requirements.
- Bullish/bearish real-body engulfing detection and body-to-range ratio.
- RSI extension boundaries at exactly 70 and 30.
- Relative ratio returns/trend classification and missing sector mappings.
- Strategy matching for all enabled strategy YAML definitions.
- Ranking weights, absent ranking components, and repeatable sorting.
- As-of replay: verify that bars after the selected date cannot affect results, and that a pivot is not known before its right-side confirmation bars exist.
- Config validation: malformed fields, unknown strategy primitive, duplicate symbols, and unsupported strategy status.
- Report rendering: candidate card, watch card, manual ORATS checklist, disclaimers, and missing-data diagnostics.

No live network calls should be required in automated CI tests; mock or fixture price data.

### 13.2 Acceptance criteria

The v1 implementation is complete when:

1. `scanner run` loads the configured universe, retrieves/loads daily data, computes the specified technical signals, matches strategies, ranks matches, and creates a standalone HTML report.
2. `scanner validate` catches malformed universe/mapping/settings/strategy files with actionable messages.
3. All strategies in §7.1 have editable configuration files; Flyagonal is visibly marked as a stub and disabled until defined.
4. The report differentiates confirmed candidates from watch items and explains the evidence and missing conditions.
5. The report includes relative strength where mappings/data are available and clearly marks unavailable values.
6. The report includes static manual ORATS checklist items without fetching or evaluating ORATS data.
7. No fundamentals, earnings dates, macro events, option chains, option prices, Greeks, or order-placement functionality is implemented.
8. `scanner run --as-of YYYY-MM-DD` produces a best-effort historical technical replay without using post-date bars in any calculation.
9. The unit test suite passes, including the confirmation and no-lookahead tests.
10. README and `docs/rules.md` accurately explain the implemented definitions, defaults, config, limitations, and run commands.

---

## 14. Defaults and Explicit Assumptions

The following defaults should be configurable, rather than treated as immutable truths:

| Item | Default |
|---|---|
| Bar timeframe | Daily |
| History fetched | 3 years |
| Pivot window | 5 bars on each side |
| ATR | 14 periods |
| S/R and trendline zone half-width | 0.5 × ATR(14) |
| S/R approach threshold | 1.0 × ATR(14) from zone |
| Nearest levels displayed | 3 supports and 3 resistances |
| Range lookback | 60 completed bars |
| Range boundary observations | At least 2 independent touches at each boundary |
| RSI | 14 periods, Wilder smoothing |
| RSI overbought/oversold | 70 / 30 |
| Full-bodied candle threshold | Real-body size / total range >= 0.70 |
| Hold search-back window | Up to 3 bars between test and response evaluation, configurable |
| Relative strength returns | 21 / 63 / 126 sessions |
| Relative-ratio trend | Ratio vs. 20-session SMA and SMA direction |
| Ranking weights | 25 / 20 / 15 / 15 / 10 / 10 / 5 as in §8.2 |
| Output | Standalone HTML report |
| Run mode | Manual; default assumes after-close daily bars |

These assumptions should be listed in the README and surfaced in configuration. They are implementation defaults intended to make the first version concrete and testable. The owner can refine them after comparing the output with manual TradingView chart reads.

---

## Appendix A — Legacy ORATS Acronyms for Manual Review

- **D/F/S edge:** D = Distribution (Monte Carlo); F = Forecast (volatility forecast); S = Smooth-market volatilities (theoretical pricing). These are dashboard checks performed by the owner, not by this program.
- **wld:** win/lose/draw. Retained only in static strategy management notes where relevant; not evaluated by the scanner.
- **IVR:** IV Rank, historically defined against a one-year period in the supplied strategy notes. The scanner does not calculate it.
- **DTE:** days to expiration. Any DTE values shown are static construction guidance from the strategy config, not selected current expirations.
- **Delta:** options delta. Any delta targets shown are static strategy guidance, not calculated from an option chain.

## Appendix B — Scope Boundary Summary

**Automated:** daily price download/cache, technical indicators, support/resistance zones, pivot trend structure, confirmations, RSI extension, relative strength, technical strategy matching, ranking, replay, HTML reporting.

**Manual only:** review chart context in TradingView; review ORATS volatility/options criteria and D/F/S; use Google Finance or other sources for macro/fundamental research; validate event-specific strategy prerequisites; determine contract selection, pricing, risk, and whether to trade.

**Explicitly excluded from v1:** ORATS API, option chain data, current option pricing, fundamentals, earnings/event feeds, macro calendars, broker integration, automated TradingView alerts/Pine Script, scheduling, position tracking, automated trade management, and performance backtesting.
