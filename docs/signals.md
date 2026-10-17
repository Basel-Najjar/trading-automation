# Signal vocabulary and exact definitions

This is the reference for every name you can use in `strategies.yaml` (§6.4, §7.3 of the requirements). The
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
| `diagonal` | A line through a pair of confirmed pivot lows (support) or pivot highs (resistance) within `trendline_lookback_bars`. The line is **discarded** if any close from its first anchor up to bar N − `max_signal_age_bars` − 1 is beyond its zone, i.e. a close through the line between the anchors or since the second anchor. Excluding the most recent bars means the bar that breaks the line is still evaluated as a break. Touches are same-side pivots inside the zone. The best `max_trendlines_per_side` lines per side are kept, and near-duplicates are removed. | `trendline_lookback_bars`, `trendline_scale` (`linear`/`log`), `min_touches_diagonal`, `max_trendlines_per_side` |
| manual | From `manual_levels.yaml`, tagged `(manual)`. Always included. A diagonal whose anchors are dated after the as-of date is ignored. | `manual_quality` |

**Pivots.** A pivot high on bar *i* has a high strictly greater than each of the `pivot_window` bars before it and
greater than or equal to each of the `pivot_window` bars after it. If several bars share the top, the first one counts. Pivot lows mirror this. A pivot only
**exists once `pivot_window` later bars have closed** (no look-ahead). A forming bar never confirms a pivot.

**Quality** = touches × 0.5^(bars since last touch / `recency_half_life_bars`). SMAs have a fixed quality
(`sma_base_quality`).

**Confluence.** Levels of different types whose zones overlap on a bar. The report merges overlapping zones
transitively and lists their `sources`. For scoring, a signal level's confluence is the number of distinct
level types whose zones overlap its zone on the signal bar.

## Candle helpers

- `body = |close − open|`, `range = high − low`.
- **Full-bodied**: `body / range ≥ full_body_ratio` (default 0.70). A bar with zero range is never full-bodied.
- **Engulfing**: bar N's body covers bar N-1's body, i.e. `max(open,close)_N ≥ max(open,close)_{N-1} − tol` and
  `min(open,close)_N ≤ min(open,close)_{N-1} + tol`, with `tol = engulfing_tolerance_atr × ATR` (default 0).
  Equal bodies count as engulfing.
  - For **breaks**, same-colour engulfing is allowed (`break_engulfing_require_opposite_color: false`, the spec
    default).
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

## Break signals (2-bar break with engulfing confirmation, OQ-1, decision B5)

The owner's rule: *a break is a 2-bar close beyond the level. An engulfing candle is required, and the second bar
must be full-bodied.* Bar N-1 is the first close beyond the zone. Bar N is the second, confirming bar.

### `support_break`
For any level L, all of the following hold:
1. Bar N-1 **closes below `zone_low`**.
2. Bar N **closes below `zone_low`**.
3. Bar N is a **full-bodied bearish** bar.
4. Bar N's body **engulfs** bar N-1's body.
5. Price came from the support side: each of the `break_prior_side_bars` (default 3) closes before bar N-1 was
   ≥ `zone_low`. So a reclaim of a level that price had already broken is not a break.

A level that breaks on a bar is not reported as a hold of the opposite role on the same bar. For example, a
resistance break is never also reported as a `support_hold` on that level.

Evidence includes `first_close_bar` (bar N-1) and `beyond_zone_atr`, how far bar N's close is beyond the zone. It is
flagged `stretched` when that exceeds `chase_atr` (1.5).
Parameters: `full_body_ratio`, `break_engulfing_require_opposite_color`, `engulfing_tolerance_atr`,
`break_prior_side_bars`, `chase_atr`.

**Watch list (near):**
- price is inside the zone, and the prior closes are on the level's side;
- or bar N is the first close beyond the zone, and the second close is still needed.

### `resistance_break`
Mirror image of `support_break`:
- Bars N-1 and N both close above `zone_high`.
- Bar N is a full-bodied bullish bar, and its body engulfs bar N-1's body.
- The `break_prior_side_bars` closes before N-1 were all ≤ `zone_high`.

## Pattern signals

### `bullish_engulfing`
Bar N is bullish and its body engulfs bar N-1's body. By default bar N-1 must be bearish (classic pattern).
`at_level` records whether bar N's range touched any level's zone; it is a **score bonus**, not a requirement.

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

## Trend states (`technical.trend`)

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
| `any` | (strategies only) no restriction; cannot be combined with other states |

Relative-strength ratios (sector ETF / SPY, symbol / sector ETF) use the same logic on the ratio of closes. A
ratio's ATR is therefore Wilder's average of |Δratio|.

## Strategy rule keys (`technical:`)

| Key | Values | Meaning |
|---|---|---|
| `trend` | list of trend states, or `[any]` | The symbol's trend state must be in the list. |
| `signals_all` | list of signal names | Every listed signal must be true (at age ≤ `max_signal_age_bars`). |
| `signals_any` | list of signal names | At least one must be true. Leave it empty or omit it if no signal is needed. |
| `not_extended` | `auto` \| `none` \| `up` \| `down` | `up` fails if `extended_up` is true on the latest bar; `down` fails if `extended_down` is true; `auto` uses the bias (bullish → `up`, bearish → `down`, others → `none`). |
| `alignment` | `soft` \| `required` \| `ignore` | `soft` labels the candidate and affects its score. `required` drops `countertrend` candidates. `ignore` skips the check. |
| `overrides` | map | Optional per-strategy setting overrides, limited to the `signals:` keys and `rsi_overbought`/`rsi_oversold`. They apply only to that strategy. |

**Signal age.** Signals on bar N have age 0. Up to `max_signal_age_bars` (default 1) earlier bars are also
evaluated (age 1 = "recent"), each using only the data that existed on that bar. Older signals are not reported.

**PENDING_CLOSE.** During market hours the latest bar is still forming. A signal that *would* fire on it is
reported as `PENDING_CLOSE`, and rules combine three-valued:
- `signals_any`: true if any member is true, pending if none is true but some are pending.
- `signals_all`: the dual (false if any member is false, pending if none is false but some are pending).

A signal that would not fire on the forming bar is simply absent. Pivots, levels and trend never use a forming bar.

## Flow alignment (`with_flow`, decision B2)

There are three components: market (SPY trend), sector (sector ETF / SPY), and name (symbol / sector ETF; `n/a` without a
mapping). Which component states agree with or oppose a bias is set in `settings.yaml → flow`:

| Bias | Agree | Oppose |
|---|---|---|
| bullish | uptrend | downtrend |
| non_bearish | uptrend, range | downtrend |
| bearish | downtrend | uptrend |
| non_bullish | downtrend, range | uptrend |
| neutral | range | uptrend, downtrend |

Components that are `mixed` or `n/a` count as neutral. The label is:
- `countertrend` if opposing components outnumber agreeing ones;
- `with_flow` if none oppose and at least one agrees;
- `mixed` otherwise.

## Plan placeholders

The display-only plan text may contain these placeholders. They are replaced on each candidate card:

| Placeholder | Replaced with |
|---|---|
| `{nearest_support}` | The zone low of the nearest support below the close, projected one bar ahead, with its type and level, e.g. `$178.20 (sma50 zone low; level $179.10)`. |
| `{nearest_resistance}` | The zone high of the nearest resistance above the close, in the same form. |

If no such level exists the text reads `n/a (no level found)`.

## Score (0–100)

The score is the weighted sum of eight components, each scaled to 0–1. Weights and scaling are in `settings.yaml → scoring`.

| Component | Raw value |
|---|---|
| signal | by kind: break 1.0, hold 0.75, pattern 0.5, context 0.2 |
| level_quality | quality / `level_quality_cap` (+ `role_reversal_bonus`), capped at 1 |
| confluence | (distinct overlapping level types − 1) / 2, capped at 1 |
| trend_fit | 1.0 if the strategy lists trend states (the match guarantees one fits); `trend_fit_any` for `[any]` |
| flow | `flow_scores` (with_flow 1, mixed 0.5, countertrend 0; `alignment: ignore` scores 0.5) |
| rsi_headroom | bullish/non_bearish: (70 − RSI)/30; bearish/non_bullish: (RSI − 30)/30; neutral: 0.5 |
| entry_quality | holds: 1 − distance from level / `approach_atr`; breaks: 1 − distance beyond zone / `chase_atr`; patterns: 1 at a level, else 0.5; context: 0.5 |
| age | `age_scores` (age 0: 1.0, age 1: 0.5) |

If several signals qualify a candidate, the one giving the highest score is the **primary signal**.

Ranking:
1. Confirmed candidates come before PENDING_CLOSE ones.
2. Within each group, higher score first.
3. Ties break by primary-level quality, then symbol, then strategy id.
