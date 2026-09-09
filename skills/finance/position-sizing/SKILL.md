---
name: position-sizing
description: Share and contract counts from a stated risk budget.
version: 1.0.0
author: Mako (ADHD-IRL), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Risk, Sizing, Kelly, Trading, Finance]
    category: finance
    related_skills: [options-strategy, trade-journal, options-chain]
---

# Position Sizing Skill

Answers "how many" from a risk budget instead of a dollar amount: share count
from a stop distance, contract count from a defined max loss, Kelly and
fractional-Kelly fractions, portfolio heat across open positions, and realised
R-multiples. Pure arithmetic — no market data, no broker, no orders.

## When to Use

- User asks how many shares or contracts to buy
- User gives an entry and a stop and wants the size that fits their risk
- User asks whether they are carrying too much open risk at once
- User asks about Kelly sizing or optimal bet fraction
- User wants a closed trade scored in R-multiples
- Immediately after `options-strategy` returns a `max_loss` for a structure

## Prerequisites

Python 3.10+ standard library only. No API key, no network, no installs.

You need the user's account equity and their risk tolerance per trade. Ask for
both rather than assuming — 1% per trade is a common default but it is theirs
to set, not yours.

On Windows, Hermes keeps its data under `%LOCALAPPDATA%\hermes`, so the skill
path is `%LOCALAPPDATA%\hermes\skills\finance\position-sizing\scripts\` rather than
`~/.hermes/skills/finance/position-sizing/scripts/`. The examples below use the POSIX
form; substitute accordingly.

## How to Run

Invoke through the `terminal` tool:

```
SCRIPT=~/.hermes/skills/finance/position-sizing/scripts/sizing.py
python $SCRIPT shares --account 100000 --risk-pct 1 --entry 50 --stop 47.50
```

Output is JSON on stdout.

## Quick Reference

```
python $SCRIPT shares --account 100000 --risk-pct 1 --entry 50 --stop 47.50 --target 60
python $SCRIPT contracts --account 50000 --risk-pct 2 --max-loss 300 --max-profit 200
python $SCRIPT kelly --win-rate 55 --win-loss-ratio 1.8 --fraction 0.25
python $SCRIPT heat --account 100000 --position AAPL:1000 --position NVDA:1500
python $SCRIPT r-multiple --entry 50 --stop 47.5 --exit 57.5 --shares 400
```

## Procedure

1. **Establish the risk budget.** Account equity times risk percent. Everything
   else follows from it. If the user has not named a percentage, ask.
2. **Size stock from the stop.** `shares` divides the budget by the per-share
   stop distance and truncates — it never rounds up past the budget. A wide stop
   means fewer shares, not more risk.
3. **Size options from max loss.** Take `max_loss` from `options-strategy` and
   feed it to `contracts` as a positive number. For defined-risk spreads this is
   exact; for undefined-risk positions there is no valid contract count and the
   skill should not be used to manufacture one.
4. **Check the whole book, not just the trade.** `heat` sums open risk across
   positions. Sustained heat above ~6% means one correlated move can do real
   damage.
5. **Score closed trades in R.** `r-multiple` normalises outcomes so a $200 win
   on a tight stop and a $2,000 win on a wide one compare honestly. Feed these
   into `trade-journal`.
6. **Report the actual risk, not just the count.** `actual_risk` is what the
   truncated size really puts at stake.

## Pitfalls

- **Kelly's output is a fraction of capital to stake, and it is aggressive.**
  A 30% full Kelly means risking 30% of the account per trade. Quarter-Kelly is
  the conventional floor, and even that can read high. Kelly also assumes the
  win rate and payoff ratio are known exactly — estimated from a small sample
  they are not, and overestimating either oversizes badly.
- **A negative Kelly means no trade.** When `full_kelly` is negative the edge is
  negative; correct size is zero. Do not present a "smaller" size instead.
- **Truncation is deliberate.** `shares` and `contracts` round down. Zero is a
  real answer: it means the trade does not fit the budget.
- **Stop distance is not the only risk.** Gaps, halts and assignment can exceed
  the stop. Treat `actual_risk` as a floor.
- **Heat ignores correlation.** Five semiconductor positions at 1% each are not
  5% of independent risk — they are closer to one 5% position. Say so.
- **This is arithmetic, not advice.** The skill sizes the trade the user
  described; it does not endorse taking it.

## Verification

```
python ~/.hermes/skills/finance/position-sizing/scripts/sizing.py \
  shares --account 100000 --risk-pct 1 --entry 50 --stop 47.50
```

A $1,000 budget over a $2.50 stop must return `shares: 400` and
`actual_risk: 1000.0`.
