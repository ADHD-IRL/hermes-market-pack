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
    related_skills: [trade-journal, options-chain, trailing-buy]
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
python $SCRIPT                                    # indices + watchlist + positions + vol
python $SCRIPT --symbol AAPL --symbol NVDA        # ad-hoc, ignores the watchlist file
python $SCRIPT --no-indices --mover-threshold 5   # only large watchlist moves
python $SCRIPT --no-iv                            # skip the vol section (much faster)
python $SCRIPT --volume-threshold 2               # only flag volume at 2x normal or more
python $SCRIPT --iv-limit 20                      # price more symbols for the vol section
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
5. **Read volume as pace, not total.** During the session `pace` compares volume
   against how much the name *should* have traded by this point in the day, so
   1.5x means genuinely heavy, not "half a day gone". Outside the session it
   compares against the full-day average. A big price move on ordinary volume is
   weaker evidence than the same move on 2x volume.
6. **Use the vol section to say whether options are rich or cheap.** `iv_hv_ratio`
   divides at-the-money implied vol (nearest ~30-day expiry) by 20-day realised
   vol. Above ~1.35 the options are pricing more movement than the stock has
   actually delivered; below ~0.85 they are pricing less. Report it as context,
   never as a trade instruction.
7. **Check the `errors` list.** A missing quote means a bad or delisted ticker,
   not a flat market — surface it rather than silently omitting the symbol.

## Quick Reference: Position Fields

| Field | Meaning |
| --- | --- |
| `unrealized_pl` | Mark-to-market P/L, option multiplier applied |
| `distance_to_stop_pct` | How far price sits from the stop, as a percent of entry |
| `r_remaining` | Fraction of the original risk still intact; negative means through the stop |
| `alert` | `STOP BREACHED`, `near stop`, or `no quote` |

## Quick Reference: Volume and Volatility Fields

| Field | Meaning |
| --- | --- |
| `pace` | Volume against normal for this point in the session (`basis: pace`) or against the full-day average (`basis: full session`) |
| `session_elapsed_pct` | How far into the regular session the quote was taken |
| `unusual` | `pace` at or above `--volume-threshold` (default 1.5) |
| `atm_iv` / `iv_dte` | At-the-money implied vol and the expiry it came from (~30 days out) |
| `hv20` / `hv60` | Annualised realised vol over the last 20 and 60 sessions |
| `iv_hv_ratio` | `atm_iv / hv20` — above 1 means options price more movement than the stock has delivered |
| `verdict` | `rich` (≥1.35), `fair`, or `cheap` (≤0.85) |

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
- **The vol section costs two requests per symbol**, so it is capped at
  `--iv-limit` (default 12) and can be turned off with `--no-iv`. Expect a few
  seconds per symbol.
- **`iv_hv_ratio` is context, not a signal.** Options are often priced above
  realised vol for good reason — an earnings date, a pending decision, a known
  catalyst. "Rich" does not mean "sell premium"; it means "find out why".
- **Realised vol looks backwards.** A name that was calm for twenty sessions and
  just gapped will show low `hv20` and a misleadingly rich-looking ratio until
  the window catches up.
- **Volume pace is unreliable in the first minutes of the session**, when a tiny
  elapsed fraction turns ordinary volume into a huge multiple. It is not
  computed before the open at all.
- **Mutual funds and most indices have no options chain**, so they appear in the
  quote sections but are skipped in the vol comparison.

## Verification

```
python ~/.hermes/skills/finance/market-brief/scripts/brief.py --symbol AAPL --no-indices
```

Prints a `Watchlist` section containing AAPL with a numeric price and a percent
change.
