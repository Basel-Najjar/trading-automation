# Signal vocabulary and exact definitions

This is the reference for every name you can use in `scans.yaml` (§6.4, §7.3 of the requirements). The
code is the single source of truth (`src/scanner/signals.py`, registry `REGISTRY`); a test fails if a signal
exists in code but not on this page.

All parameters live in `config/settings.yaml`. Notation:

- **Bar N** is the latest bar evaluated (the latest completed bar, or the forming bar during market hours);
  **N-1** is the bar before it.
- **ATR** is Wilder ATR(14) on the bar being tested. **k** is `levels.zone_atr_mult` (default 0.25).
- A level's **zone** on bar *t* is `[level_t − k·ATR_t, level_t + k·ATR_t]` (`zone_low`, `zone_high`).
  For SMAs and diagonal lines `level_t` is the line's value on that bar, so the zone moves with the line.
- Every signal returns `true`, `false` or `PENDING_CLOSE`, plus evidence (bars, prices, ratios) when it fires.

## Levels (what "a support or resistance level L" means)

| Type | How it is built | Parameters |
|---|---|---|
| `sma50`, `sma200` | Simple moving average of close. Acts as **support when price is above it, resistance when below** (re-evaluated every bar). | — |
| `horizontal` | Pivot highs are clustered together and pivot lows are clustered together (sorted by price; a cluster's max − min ≤ `cluster_atr_mult × ATR`). A cluster needs ≥ `min_touches_horizontal` pivots. Level = median price. **Role reversal** = the level's side of price differs from its origin (a broken resistance now below price acts as support). | `cluster_atr_mult`, `min_touches_horizontal`, `horizontal_lookback_bars` |
| `diagonal` | A line through a pair of confirmed pivot lows (support) or pivot highs (resistance) within `trendline_lookback_bars`. The line is **discarded** if any close from its first anchor up to bar N − 2 is beyond its zone, i.e. a close through the line between the anchors or since the second anchor. Excluding the two most recent bars means the 2-bar break of the line is still evaluated as a break. Touches are same-side pivots inside the zone. The best `max_trendlines_per_side` lines per side are kept, and near-duplicates are removed. | `trendline_lookback_bars`, `trendline_scale` (`linear`/`log`), `min_touches_diagonal`, `max_trendlines_per_side` |
| manual | From `manual_levels.yaml`, tagged `(manual)`. Always included. A diagonal whose anchors are dated after the as-of date is ignored. | `manual_quality` |

**Pivots.** A pivot high on bar *i* has a high strictly greater than each of the `pivot_window` bars before it and
greater than or equal to each of the `pivot_window` bars after it. If several bars share the top, the first one counts. Pivot lows mirror this. A pivot only
**exists once `pivot_window` later bars have closed** (no look-ahead). A forming bar never confirms a pivot.

**Quality** = touches × 0.5^(bars since last touch / `recency_half_life_bars`). SMAs have a fixed quality
(`sma_base_quality`).

**Slope.** Horizontal levels (pivot clusters, range boundaries, manual horizontals) are **flat**. Every other level
(diagonal trendline, SMA, manual diagonal) is **rising** or **falling** by its change into the bar being tested. The
slope, not the role, decides which held signal a sloped level gives: a rising line that held is `rising_line_held`
(non-bearish) and a falling one is `falling_line_held` (non-bullish), whether price held above or below it.

**Confluence.** Levels of different types whose zones overlap on a bar. The scanner merges overlapping zones
transitively and lists their `sources`. For scoring, a signal level's confluence is the number of distinct
level types whose zones overlap its zone on the signal bar.

## Candle helpers

- `body = |close − open|`, `range = high − low`.
- **Full-bodied**: `body / range ≥ full_body_ratio` (default 0.70). A bar with zero range is never full-bodied.
- **Engulfing**: bar N's body covers bar N-1's body, i.e. `max(open,close)_N ≥ max(open,close)_{N-1} − tol` and
  `min(open,close)_N ≤ min(open,close)_{N-1} + tol`, with `tol = engulfing_tolerance_atr × ATR` (default 0).
  Equal bodies count as engulfing.
  - For **breaks** in `engulf` mode, same-colour engulfing is allowed (`break_engulfing_require_opposite_color:
    false`, the spec default).
  - For the **pattern** signals, opposite colours are required (`pattern_engulfing_require_opposite_color: true`,
    decision B3).

## Hold signals (1-bar confirmation)

### `support_hold`
For any level L, both of the following hold:
1. **A test happened.** Within the last `hold_test_lookback` bars (default 3, including N-1 and N), there is a bar whose
   low entered the zone (`low ≤ zone_high`) **and** which closed at or above `zone_low`.
2. **It came from the support side** (decision B4, `hold_require_prior_side: true`): the close before that test
   bar was ≥ `zone_low`. This stops a gap up from *below* the level from counting as a hold.
3. **Confirmation:** bar N **closes above bar N-1's high**.

Evidence: the level, its zone on the test bar, OHLC of the test, prior and confirmation bars, and the distance from the level in ATRs.
Parameters: `hold_test_lookback`, `hold_require_prior_side`, `zone_atr_mult`.

### `resistance_hold`
Mirror of `support_hold`. A bar's high entered the zone (`high ≥ zone_low`) and closed ≤ `zone_high`, and the
previous close was ≤ `zone_high`. Bar N **closes below bar N-1's low**.

## Held signals (passive: the level was tested and has held since)

"Not exactly bullish, but support has held." A held signal fires on the bar that **tested** a level, and is kept only
while it still holds. Each needs all of the following:
1. **The test.** For support, the bar's low entered the zone (`low ≤ zone_high`) and it closed ≥ `zone_low`, and the
   close before it was ≥ `zone_low` (it came from above). For resistance, the mirror: `high ≥ zone_low`, close ≤
   `zone_high`, previous close ≤ `zone_high`.
   The test bar must also be a **pullback extreme**: its low is the lowest of the `held_swing_bars` (default 5) bars
   before it (for resistance, its high is the highest). A touch while price drifts along a level is not a test.
2. **The level existed before the test.** A pivot cluster needs ≥ `min_touches_horizontal` pivots dated before the
   test bar and a trendline both anchors before it (SMAs and manual horizontals always qualify). This stops a pivot
   made by the test itself from proving the level held.
3. **It still holds.** Every completed close after the test stayed on the same side of the zone (for a diagonal or
   SMA, the zone on each bar), and no later bar went beyond the test bar's low (high, for resistance), so the test
   is still the swing. A close through the zone drops the signal; a lower low that still holds becomes the new test.

Only the latest test of each level is reported. Its age is the test bar's age, so a scan's `lookback_bars` sets how
recent the test must be. No confirmation candle is needed; that is `support_hold` / `resistance_hold`.

| Signal | Levels | Leans |
|---|---|---|
| `support_held` | horizontal, tested from above | non-bearish |
| `resistance_held` | horizontal, tested from below | non-bullish |
| `rising_line_held` | rising diagonal or SMA, tested from whichever side price was on (support or resistance) | non-bearish |
| `falling_line_held` | falling diagonal or SMA, tested from whichever side price was on | non-bullish |

Parameters: `zone_atr_mult`, `held_swing_bars`, `min_touches_horizontal`.

## Break signals (2-bar close through a level, OQ-1, decision B5)

The owner's rule: *a break is a 2-bar close beyond the level, with strong candles.* Bar N-1 is the first close beyond
the zone. Bar N is the second, confirming bar. Expect to see this rarely (most tests of a level do not break it), and
avoid it as an entry when the stock is already extended (RSI). `break_confirm` chooses the confirmation:

- **`prior_extreme`** (default, the owner's diagrams): bars N-1 and N are **both full-bodied** candles in the break
  direction, and bar N **closes beyond bar N-1's low** (support break) or **high** (resistance break).
- **`engulf`** (the original rule): bar N is full-bodied in the break direction and its body **engulfs** bar N-1's.

### `support_break`
For any level L, all of the following hold:
1. Bar N-1 **closes below `zone_low`**.
2. Bar N **closes below `zone_low`**.
3. The confirmation above: with `prior_extreme`, N-1 and N are full-bodied bearish and N closes below N-1's low;
   with `engulf`, N is full-bodied bearish and its body engulfs N-1's.
4. Price came from the support side: each of the `break_prior_side_bars` (default 3) closes before bar N-1 was
   ≥ `zone_low`. So a reclaim of a level that price had already broken is not a break.

A level that breaks on a bar is not reported as a hold of the opposite role on the same bar. For example, a
resistance break is never also reported as a `support_hold` on that level.

Evidence includes `first_close_bar` (bar N-1) and `beyond_zone_atr`, how far bar N's close is beyond the zone. It is
flagged `stretched` when that exceeds `chase_atr` (1.5).
Parameters: `full_body_ratio`, `break_confirm`, `break_engulfing_require_opposite_color`, `engulfing_tolerance_atr`,
`break_prior_side_bars`, `chase_atr`.

**Watch list (near):**
- price is inside the zone, and the prior closes are on the level's side;
- or bar N is the first close beyond the zone (a full-bodied one, with `prior_extreme`), and the second close is
  still needed. With `prior_extreme` the trigger is a close beyond bar N's low/high.

### `resistance_break`
Mirror image of `support_break`:
- Bars N-1 and N both close above `zone_high`.
- `prior_extreme`: both bars full-bodied bullish and N closes above N-1's high. `engulf`: N full-bodied bullish and
  its body engulfs N-1's.
- The `break_prior_side_bars` closes before N-1 were all ≤ `zone_high`.

## Pattern signals

### `bullish_engulfing`
Bar N is bullish and its body engulfs bar N-1's body. By default bar N-1 must be bearish (classic pattern).
`at_level` records whether bar N's range touched any level's zone (evidence only, not a requirement).

### `bearish_engulfing`
Mirror image: bar N is bearish and engulfs N-1's body (by default N-1 must be bullish).

## Context signals

### `approaching_support`
Bar N closes **above** a level's zone, within `approach_atr` ATRs of `zone_high` (default 1.0), and is closer to
the level than it was `approach_lookback_bars` bars ago (default 3). A close inside the zone is "in zone", not
"approaching". The watch list uses that state.

### `approaching_resistance`
Mirror image: bar N closes **below** the zone, within `approach_atr` ATRs of `zone_low`, and is moving toward the level.

### `range_bound`
Trend state is `range` (see below).

### `range_mid`
Range-bound, and the close sits inside the middle band of the range, `range.mid_band` (default 0.35–0.65 of
the range height).

### `extended_up`
RSI(14) ≥ `rsi_overbought` (default 70). Evidence: the RSI value and its distance above the threshold.

### `extended_down`
RSI(14) ≤ `rsi_oversold` (default 30).

## Trend states (the `trend:` criterion)

The trend state compares the last two confirmed pivot highs and the last two confirmed pivot lows. The tolerance is
`tol = cluster_atr_mult × ATR` (decision B1):
- **higher:** the move is more than +tol;
- **equal:** within ±tol;
- **lower:** the move is more than −tol.

| State | Rule |
|---|---|
| `uptrend` | higher high **and** higher low |
| `downtrend` | lower high **and** lower low |
| `range` | equal highs **and** equal lows, and the close lies between `range_low.zone_low` and `range_high.zone_high`. The boundaries are the horizontal clusters containing both pivots. |
| `mixed` | anything else, including fewer than two confirmed pivots on either side |

Relative-strength ratios (sector ETF / SPY, symbol / sector ETF) use the same logic on the ratio of closes. A
ratio's ATR is therefore Wilder's average of |Δratio|.

## Scan criteria (`scans.yaml`)

There are five scans, most bullish to most bearish: `bullish`, `non_bearish`, `neutral`, `non_bullish`, `bearish`.
Each has `enabled`, `min_pct`, `lookback_bars` and criteria. Each criterion has exactly one check, plus an optional
`weight` (default 1) and `label`; a signal criterion can also take `within`.

| Check | Value | Met when |
|---|---|---|
| `trend` | list of trend states | The symbol's trend state is in the list. |
| `signal_all` | list of signal names | Every listed signal fired on one of the latest `lookback_bars` bars (or `within`). |
| `signal_any` | list of signal names | At least one listed signal fired in that window. |
| `rsi_below` | number | RSI on the latest bar < the value. |
| `rsi_above` | number | RSI on the latest bar > the value. |
| `rsi_between` | `[low, high]` | low ≤ RSI ≤ high on the latest bar. |
| `flow` | `with_flow` \| `not_countertrend` | The flow label for the scan's direction (below) is `with_flow`, or is anything but `countertrend`. |
| `channel` | bars (3–30) | Every close of the last N completed bars stayed between a **horizontal** support and a **horizontal** resistance (below). |

**Channel.** The support and resistance are horizontal levels (pivot clusters, range boundaries or manual
horizontals) from the structure known on the bar before the window, so the channel cannot be proven by pivots it
made itself. No close in the window went below the support's zone low or above the resistance's zone high, the two
zones do not overlap, and each zone was touched by at least `range.channel_min_touches` bars of the window (default
1). Sloped lines never count: a rising or falling channel is non-bearish or non-bullish, not neutral. Completed bars
only, so a channel is never pending.

**Match %** is the weight of the criteria met ÷ the total weight. A ticker is listed in every scan whose `min_pct`
it reaches, so it can be listed in more than one. Ranking within a scan:
1. Higher match % first.
2. At equal match, less PENDING_CLOSE weight first (confirmed before pending).
3. Then symbol.

**Lookback (signal age).** Signals on bar N have age 0, on N-1 age 1, and so on. A signal criterion counts signals
with age < the scan's `lookback_bars` (1 = the latest bar only; 5 ≈ one week; 10 ≈ two weeks), or the criterion's own
`within`. Every bar in the window is evaluated with only the data that existed on it. Longer windows cost run time:
each bar rebuilds pivots, levels and trend, so the cap is 30.

**PENDING_CLOSE.** During market hours the latest bar is still forming. A signal that *would* fire on it is
reported as `PENDING_CLOSE`, and criteria combine three-valued:
- `signal_any`: true if any member is true, pending if none is true but some are pending.
- `signal_all`: the dual (false if any member is false, pending if none is false but some are pending).
- RSI checks: a condition met on the forming bar is pending; one not met is false.

Pending counts as not met in the match %. A signal that would not fire on the forming bar is simply absent. Pivots,
levels and trend never use a forming bar.

**Primary signal.** The price ladder is built around one fired signal: confirmed before pending, then break >
hold > held > pattern > context, then the newest, then the best level. It does not affect ranking.

**Watch list.** A ticker below a scan's minimum is on the watch list when its unmet signal criteria have
near-firing signals (`register_near`) and firing them would lift it to the minimum.

## Flow alignment (the `flow:` criterion, decision B2)

There are three components: market (SPY trend), sector (sector ETF / SPY), and name (symbol / sector ETF; `n/a` without a
mapping). Which component states agree with or oppose each scan direction is set in `settings.yaml → flow`:

| Scan | Agree | Oppose |
|---|---|---|
| bullish | uptrend | downtrend |
| non_bearish | uptrend, range | downtrend |
| neutral | range | uptrend, downtrend |
| non_bullish | downtrend, range | uptrend |
| bearish | downtrend | uptrend |

Components that are `mixed` or `n/a` count as neutral. The label is:
- `countertrend` if opposing components outnumber agreeing ones;
- `with_flow` if none oppose and at least one agrees;
- `mixed` otherwise.
