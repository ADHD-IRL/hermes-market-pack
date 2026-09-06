---
name: trade-journal
description: Log trades to SQLite and score them in R-multiples.
version: 1.0.0
author: Mako (ADHD-IRL), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Journal, Performance, Trading, Risk, Finance]
    category: finance
    related_skills: [position-sizing, options-strategy, market-brief]
---

# Trade Journal Skill

Records entries and exits in a local SQLite database so performance comes from
what actually happened rather than from memory. Every closed trade is scored in
R-multiples — profit divided by the risk originally taken — which makes a small
win on a tight stop comparable to a large win on a wide one. The database stays
on the user's machine and is never transmitted anywhere.

## When to Use

- User says they bought, sold, entered or exited something
- User asks how they have been trading, their win rate, or their expectancy
- User asks what positions are currently open
- User wants a strategy compared against another over time
- After `position-sizing` produces a size the user acts on

## Prerequisites

Python 3.10+ standard library only. No API key, no network, no installs.

The database is created on first use at `$HERMES_HOME/trade_journal.db`
(default `~/.hermes/trade_journal.db`). Set `TRADE_JOURNAL_DB` to point
somewhere else.

On Windows, Hermes keeps its data under `%LOCALAPPDATA%\hermes`, so the skill
path is `%LOCALAPPDATA%\hermes\skills\finance\trade-journal\scripts\` rather than
`~/.hermes/skills/finance/trade-journal/scripts/`. The examples below use the POSIX
form; substitute accordingly.

## How to Run

Invoke through the `terminal` tool:

```
SCRIPT=~/.hermes/skills/finance/trade-journal/scripts/journal.py
python $SCRIPT open --symbol AAPL --side long --qty 400 --entry 50 --stop 47.50
```

Output is JSON on stdout. Each `open` returns the trade `id` needed to close it.

## Quick Reference

```
python $SCRIPT open --symbol AAPL --side long --qty 400 --entry 50 --stop 47.50 \
  --target 60 --strategy breakout --notes "gap and go"
python $SCRIPT open --symbol SPY --side long --qty 2 --entry 5.00 --stop 2.50 \
  --kind option --strategy bull-call-spread
python $SCRIPT close --id 1 --exit 57.50 --notes "hit target"
python $SCRIPT list --status open
python $SCRIPT stats --since 2026-01-01
python $SCRIPT stats --strategy breakout
python $SCRIPT note --id 1 --text "sized too small in hindsight"
```

## Procedure

1. **Always record a stop.** Without one the trade cannot be scored in R and is
   excluded from the R statistics. If the user did not set one, ask before
   logging rather than inventing a number.
2. **Log options with `--kind option`.** Quantity is contracts and prices are
   per share, so the 100x multiplier is applied automatically. Getting this
   wrong understates P/L by two orders of magnitude.
3. **Tag with `--strategy`.** This is what makes `stats --strategy` able to show
   which approach actually works. Use consistent tags.
4. **Close with the real fill**, not the target. `close` computes P/L and the
   R-multiple and stamps the exit time.
5. **Read `stats` honestly.** `profit_factor` above 1.0 is profitable;
   `expectancy_r` is the average R per trade and is the single most useful
   number. A high win rate with negative expectancy means the losses are too big.
6. **Feed `win_rate` and `win_loss_ratio` into `position-sizing kelly`** once
   there is a real sample to draw on.

## Quick Reference: Output Fields

| Field | Meaning |
| --- | --- |
| `pl` | Realised profit in dollars, multiplier applied |
| `r_multiple` | Profit as a multiple of the risk taken at entry |
| `expectancy_r` | Average R across scored trades — the edge, per trade |
| `profit_factor` | Gross wins / gross losses; above 1.0 is profitable |
| `win_loss_ratio` | Average win / average loss, in dollars |

## Pitfalls

- **Small samples lie.** Twenty trades cannot distinguish a real edge from
  luck. Report the trade count alongside any statistic and resist drawing
  conclusions from a handful of trades.
- **Trades without stops silently skew things.** They appear in the dollar
  statistics but not the R statistics; the `note` field says how many.
- **`delete` is permanent.** There is no undo. Prefer `note` for corrections so
  the record stays honest.
- **Fees and commissions are not tracked.** Reported P/L is gross.
- **Multi-leg option positions log as one row.** Use the net debit or credit per
  spread as the entry price and record the structure in `--strategy`.
- **The journal describes the past.** It does not predict the next trade, and a
  good run is not evidence the next one will work.

## Verification

```
export TRADE_JOURNAL_DB=/tmp/journal-check.db
python ~/.hermes/skills/finance/trade-journal/scripts/journal.py \
  open --symbol TEST --side long --qty 100 --entry 50 --stop 45
python ~/.hermes/skills/finance/trade-journal/scripts/journal.py close --id 1 --exit 60
```

The close must report `pl: 1000.0` and `r_multiple: 2.0`.
