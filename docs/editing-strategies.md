# Editing `strategies.yaml`

`config/strategies.yaml` is your playbook. It is reloaded on every run, and no code changes are ever needed.

You can edit it here by hand, or in the app: **Criteria → Edit rules** on a strategy sets `enabled` and the
`technical:` keys from dropdowns. It rewrites only that strategy's lines and keeps your comments.
`strategies.default.yaml` is the preserved original. **Restore defaults** copies it back over `strategies.yaml`. To
change what "default" means, edit `strategies.default.yaml` directly.

## Typical edits

**Disable a strategy** without deleting it:

```yaml
- id: wheel
  enabled: false
```

**Change what triggers it.** For example, let a Bull Put Spread also fire on a bullish engulfing at support:

```yaml
  technical:
    trend: [uptrend, range]
    signals_any: [support_hold, bullish_engulfing]
```

**Require going with the flow** (this drops `countertrend` candidates instead of only labelling them):

```yaml
    alignment: required
```

**Tighten one strategy only.** For example, require a stricter full body for its breaks:

```yaml
    overrides: {full_body_ratio: 0.8, rsi_overbought: 75}
```

Only `signals:` settings and the RSI thresholds can be overridden per strategy.

**Add a strategy.** Copy an existing block and give it a new unique `id` (lower_snake_case). Every field except
`bias` and `technical` is display-only: it is shown on the candidate card exactly as written and never evaluated.
You can add your own fields, such as `notes:`, and they will be displayed too.

**Use level placeholders in plan text:**

```yaml
  plan:
    management:
      - "Exit on 2-bar close below support: {nearest_support}"
```

## Rules of the file

- `id`: unique, lower_snake_case.
- `bias`: one of `bullish`, `bearish`, `non_bearish`, `non_bullish`, `neutral`.
- `status`: `complete` | `partial` | `stub`. A `stub` strategy is never evaluated.
- `technical.trend`: any of `uptrend`, `downtrend`, `range`, `mixed`, or `[any]`.
- Signal names must come from [signals.md](signals.md).

## After every edit

```
uv run scanner validate --offline
```

Typos are caught with the file, line number and a suggestion, for example:

```
ERROR strategies.yaml:66: strategies[2] (bull_put_spread).technical.signals_any[0]: unknown signal 'suport_hold' — did you mean 'support_hold'?
```

To check how a change behaves on a chart you already know, replay that date:

```
uv run scanner explain --symbol AAPL --strategy bull_put_spread --as-of 2026-09-15
```
