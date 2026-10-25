# Editing `scans.yaml`

`config/scans.yaml` holds the five scans, from most bullish to most bearish: **bullish**, **non_bearish**,
**neutral**, **non_bullish** and **bearish**. It is reloaded on every run, and no code changes are ever needed.

You can edit it here by hand, or in the app: **Criteria → Edit criteria** on a scan sets `enabled`, `min_pct`,
`lookback_bars` and its criteria in a table. Saving validates first, then rewrites the whole file in one fixed
layout, so comments you add inside it are not kept. Use a criterion's `label:` for notes. `scans.default.yaml` is the
preserved original. **Restore defaults** copies it back over `scans.yaml`. To change what "default" means, edit
`scans.default.yaml` directly.

## What the default scans look for

| Scan | Looks for |
|---|---|
| bullish | An uptrend (higher pivot highs + higher pivot lows) and/or a 2-bar bullish close through resistance, while RSI is not extended. |
| non_bearish | Horizontal support held, or a rising line (diagonal or SMA) held, with an optional bullish reversal off support. |
| neutral | A horizontal channel: every close of the last 10 bars between a horizontal support and resistance. |
| non_bullish | Horizontal resistance held, or a falling line held, with an optional bearish reversal off resistance. |
| bearish | A downtrend and/or a 2-bar bearish close through support, while RSI is not extended. |

Sloped lines go by slope, not role: a rising line that held is non-bearish even when price held *below* it (as
resistance), and a falling line that held is non-bullish even when price held above it. Only horizontal levels make
a neutral channel. See [signals.md](signals.md) for the exact definitions.

## How a scan ranks

Each criterion has a `weight` (default 1). A ticker's **match %** for a scan is the weight of the criteria it meets ÷
the scan's total weight. It is listed in that scan when the match reaches `min_pct`, highest match first. A ticker
can be listed in more than one scan.

## How far back a scan looks

`lookback_bars` is how many of the latest bars a signal may come from: `1` is the latest bar only, `5` is about one
week and `10` about two weeks. A signal criterion can use its own window with `within:`:

```yaml
non_bearish:
  lookback_bars: 10                                     # a held support in the last two weeks counts
  criteria:
    - {signal_any: [support_held, rising_line_held], weight: 2}
    - {signal_any: [support_hold], within: 3}           # but the reversal candle must be recent
```

The `channel:` check has its own length: `{channel: 10}` is every close of the last 10 completed bars. Every bar in a
window is evaluated with only the data it had. The maximum is 30, and longer windows make runs slower.

## Typical edits

**Make a trigger count more** than the context around it:

```yaml
    - {signal_any: [resistance_break], weight: 2, label: "2-bar close through resistance"}
```

**Require going with the flow** instead of only not against it:

```yaml
    - {flow: with_flow}
```

**Loosen or tighten the list:** lower or raise `min_pct`. At `0` every ticker is listed; at `100` every criterion must
be met.

**Require two signals together:** `signal_all` needs every listed signal to fire within the window.

```yaml
    - {signal_all: [support_held, bullish_engulfing]}
```

**Switch a scan off** without deleting it: `enabled: false`.

## Rules of the file

- Top-level keys: `bullish`, `non_bearish`, `neutral`, `non_bullish`, `bearish`. A missing one is off (with a
  warning that suggests **Restore defaults**).
- `min_pct`: 0–100 (default 70). `lookback_bars`: 1–30 (default 5).
- Each criterion has exactly one check:
  - `trend`: a list of `uptrend`, `downtrend`, `range`, `mixed`;
  - `signal_all` / `signal_any`: a list of names from [signals.md](signals.md);
  - `rsi_below` / `rsi_above`: a number;
  - `rsi_between`: `[low, high]`;
  - `flow`: `with_flow` or `not_countertrend`;
  - `channel`: a number of bars, 3–30.
- `weight` must be > 0. `within` (1–30) is allowed on `signal_all` / `signal_any` only. `label` is optional; a
  readable one is generated.

Settings such as zone widths, RSI periods, the break confirmation (`break_confirm`) and the held-signal pullback
(`held_swing_bars`) are global and live in `settings.yaml`.

## After every edit

Save from the app's **Criteria** tab (**Edit criteria**), which validates before writing. If you edit the file by
hand, reload the app: it validates `config/` on load and lists any errors on the page. Typos are caught with the
file, line number and a suggestion, for example:

```
ERROR scans.yaml:27: non_bearish.criteria[0].signal_any: unknown signal 'suport_held' — did you mean 'support_held'?
```

To check how a change behaves on a chart you already know, pick that date as the app's session date, press **Run
analysis**, open the symbol and pick the scan. The setup pane lists every criterion with its evidence.
