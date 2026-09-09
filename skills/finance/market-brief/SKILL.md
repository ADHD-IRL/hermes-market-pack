---
name: market-brief
description: Scheduled digest of indices, watchlist and positions.
version: 1.0.0
author: Mako (ADHD-IRL), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Market, Watchlist, Cron, Reporting, Finance]
    category: finance
    related_skills: [trade-journal, options-chain, position-sizing]
---

# Market Brief Skill

Produces a short market digest: index levels, watchlist quotes, anything that
moved past a threshold, and — when the `trade-journal` database exists — open
positions marked to market with a warning on any sitting near its stop. Built
for the Hermes cron scheduler so it can be delivered unattended to Telegram or
any other gateway platform. Read-only.

## When to Use

- User asks for a market summary, morning brief, or end-of-day recap
- User asks how their open positions are doing right now
- User asks whether anything on their watchlist moved
- Setting up a recurring pre-open or post-close report with `hermes cron`

## Prerequisites

Python 3.10+ standard library only. No API key, no installs. Network access to
Yahoo Finance is required.

The watchlist is one symbol per line at `$HERMES_HOME/watchlist.txt`; `#` starts
a comment. Override the location with `WATCHLIST_FILE`. Open positions are read
from the `trade-journal` database if present — that skill is optional, and the
brief degrades to quotes alone without it.

```
# ~/.hermes/watchlist.txt
AAPL
NVDA   # semis
SPY
```

On Windows, Hermes keeps its data under `%LOCALAPPDATA%\hermes`, so the skill
path is `%LOCALAPPDATA%\hermes\skills\finance\market-brief\scripts\` rather than
`~/.hermes/skills/finance/market-brief/scripts/`. The examples below use the POSIX
form; substitute accordingly.

## How to Run

Invoke through the `terminal` tool:

```
SCRIPT=~/.hermes/skills/finance/market-brief/scripts/brief.py
python $SCRIPT
```

Plain text by default — already formatted for a chat message. Add `--json` when
you need to reason over the numbers rather than relay them.

## Quick Reference

```
python $SCRIPT                                    # indices + watchlist + positions
python $SCRIPT --symbol AAPL --symbol NVDA        # ad-hoc, ignores the watchlist file
python $SCRIPT --no-indices --mover-threshold 5   # only large watchlist moves
python $SCRIPT --index '^VIX' --index '^GSPC'     # custom indices
python $SCRIPT --json                             # structured output
```

## Procedure

1. **Run it plain for a chat reply.** The default text output is already sized
   for a message — relay it rather than reformatting it into a table.
2. **Use `--json` when the user asks a question about the numbers**, such as
   which position is closest to its stop.
3. **Lead with alerts.** `STOP BREACHED` means the last price is through the
   stop and the position should already be closed; `near stop` means under 25%
   of the original risk is left. Say these first, before index levels.
4. **Schedule it with cron for unattended delivery.** See `cron/schedules.example.yaml`
   in this pack for pre-open and post-close examples.
5. **Check the `errors` list.** A missing quote means a bad or delisted ticker,
   not a flat market — surface it rather than silently omitting the symbol.

## Quick Reference: Position Fields

| Field | Meaning |
| --- | --- |
| `unrealized_pl` | Mark-to-market P/L, option multiplier applied |
| `distance_to_stop_pct` | How far price sits from the stop, as a percent of entry |
| `r_remaining` | Fraction of the original risk still intact; negative means through the stop |
| `alert` | `STOP BREACHED`, `near stop`, or `no quote` |

## Pitfalls

- **Quotes are delayed and can be stale**, especially outside regular trading
  hours. A brief generated overnight shows the previous close, not a live price.
- **`STOP BREACHED` is an observation, not an order.** Nothing here trades. Say
  what happened and let the user act.
- **Index symbols need quoting in a shell.** `^GSPC` must be `'^GSPC'` or the
  caret is interpreted by the shell.
- **The vendor endpoint is unofficial** and rate-limits. Keep scheduled runs to
  a few per day rather than every minute.
- **Open positions come from the journal, not a broker.** They are only as
  accurate as what was logged, and manual closes made elsewhere will not show.

## Verification

```
python ~/.hermes/skills/finance/market-brief/scripts/brief.py --symbol AAPL --no-indices
```

Prints a `Watchlist` section containing AAPL with a numeric price and a percent
change.
