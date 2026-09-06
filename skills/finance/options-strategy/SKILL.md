---
name: options-strategy
description: Payoff, breakevens and P(profit) for option positions.
version: 1.0.0
author: Mako (ADHD-IRL), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Options, Spreads, Payoff, Risk, Trading]
    category: finance
    related_skills: [options-chain, position-sizing, trade-journal]
---

# Options Strategy Skill

Models a multi-leg option position at expiry: net debit or credit, max profit,
max loss, every breakeven, a payoff table, and a lognormal probability of
profit. Pure arithmetic — no market data and no broker, so quotes come from
`options-chain` or from the user. Everything is evaluated at expiry; it does
not model closing the position early.

## When to Use

- User describes a spread, condor, straddle, covered call or any multi-leg idea
- User asks "what's my max loss", "where do I break even", "what's the risk to
  reward on this"
- User asks the odds a position finishes profitable
- Comparing two candidate structures on the same underlying
- After pulling quotes with `options-chain` and before sizing with
  `position-sizing`

## Prerequisites

Python 3.10+ standard library only. No API key, no network, no installs.

On Windows, Hermes keeps its data under `%LOCALAPPDATA%\hermes`, so the skill
path is `%LOCALAPPDATA%\hermes\skills\finance\options-strategy\scripts\` rather than
`~/.hermes/skills/finance/options-strategy/scripts/`. The examples below use the POSIX
form; substitute accordingly.

## How to Run

Invoke through the `terminal` tool:

```
SCRIPT=~/.hermes/skills/finance/options-strategy/scripts/strategy.py
python $SCRIPT preset vertical --type call --long-strike 100 --short-strike 110 \
  --long-premium 5 --short-premium 2
```

Output is JSON on stdout. All dollar figures are per position (option legs are
already multiplied by 100).

## Quick Reference

```
# Arbitrary legs: SIDE,KIND,STRIKE,PREMIUM[,QTY]
python $SCRIPT analyze --leg buy,call,100,5 --leg sell,call,110,2

# Named structures
python $SCRIPT preset covered-call --stock-price 100 --short-strike 105 --short-premium 3
python $SCRIPT preset cash-secured-put --short-strike 95 --short-premium 2.10
python $SCRIPT preset straddle --strike 100 --call-premium 5 --put-premium 5 --short
python $SCRIPT preset strangle --call-strike 110 --put-strike 90 --call-premium 2 --put-premium 2
python $SCRIPT preset iron-condor \
  --long-put-strike 90 --short-put-strike 95 \
  --short-call-strike 105 --long-call-strike 110 \
  --long-put-premium 1 --short-put-premium 2 \
  --short-call-premium 2 --long-call-premium 1

# Add probability of profit
python $SCRIPT analyze --leg sell,put,95,2.10 --spot 100 --iv 0.28 --days 30
```

## Procedure

1. **Get real premiums first.** Use `options-chain` for live mid prices rather
   than inventing them. A payoff built on a made-up premium is fiction.
2. **Prefer `preset` over `analyze` for standard structures.** The presets
   enforce strike ordering — an iron condor with crossed strikes is rejected
   instead of silently producing a nonsense payoff.
3. **Use `analyze --leg` for anything non-standard.** Legs are repeatable and
   combine freely: ratio spreads, broken-wing butterflies, stock plus options.
   Stock legs put the entry price in the strike field: `buy,stock,150,0,100`.
4. **Pass `--spot`, `--iv` and `--days` when the user wants odds.** Without all
   three, the `probability` block is omitted rather than guessed.
5. **Report max loss before max profit.** That is the number that decides
   whether the position is sized correctly.
6. **Hand `max_loss` to `position-sizing`** to check the trade fits the user's
   risk budget.

## Quick Reference: Output Fields

| Field | Meaning |
| --- | --- |
| `net_cash` | Entry cash flow. Negative = debit paid, positive = credit received |
| `max_loss` / `max_profit` | At expiry; `"unlimited"` when the tail is uncapped |
| `breakevens` | Underlying prices where P/L crosses zero |
| `risk_reward` | max_profit / max_loss, only when both are bounded |
| `probability.probability_of_profit` | Risk-neutral lognormal estimate |
| `payoff_table` | P/L across ±25% of spot |

## Pitfalls

- **Expiry-only.** A short premium position often hits its profit target well
  before expiry; this model cannot show that. It answers "if held to expiry".
- **Probability of profit is risk-neutral**, derived from implied vol. It is not
  a forecast, and it ignores drift, skew and early assignment. Quote it as an
  estimate.
- **`"unlimited"` is literal.** A naked short call has uncapped loss. Never
  present that as a number.
- **Assignment and dividends are not modelled.** Short American options can be
  exercised early, especially calls before an ex-dividend date.
- **Commissions and fees are excluded.** On multi-leg positions these matter.
- **This is analysis, not advice.** Present the payoff; let the user decide.

## Verification

```
python ~/.hermes/skills/finance/options-strategy/scripts/strategy.py \
  analyze --leg buy,call,100,5 --leg sell,call,110,2
```

A 100/110 call debit spread paid for $3.00 must return `net_cash: -300`,
`max_loss: -300`, `max_profit: 700`, and a single breakeven at `103.0`.
