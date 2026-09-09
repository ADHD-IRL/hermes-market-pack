---
name: options-chain
description: Option chains with locally computed Black-Scholes greeks.
version: 1.0.0
author: Mako (ADHD-IRL), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Options, Greeks, Volatility, Trading, Finance]
    category: finance
    related_skills: [options-strategy, position-sizing, stocks]
---

# Options Chain Skill

Fetches equity option chains and computes delta, gamma, theta, vega and rho
locally with Black-Scholes-Merton, because the upstream feed publishes strikes
and quotes but no greeks. Implied vol is solved from the mid price by bisection
and cross-checked against the vendor's own figure. Read-only market data — this
skill never places an order and has no broker connection.

## When to Use

- User asks for an option chain, a specific strike, or a contract's greeks
- User asks what delta, theta or vega a position carries
- User asks whether implied volatility is high or low, or about IV skew
- User asks which expiries are listed for a ticker
- Before sizing an options trade with `position-sizing` or modelling legs with
  `options-strategy`

## Prerequisites

Python 3.10+ standard library only — no API key, no pip installs. Network
access to Yahoo Finance is required; the client establishes the cookie/crumb
session itself.

Optional: set `RISK_FREE_RATE` (decimal, e.g. `0.042`) to match the current
short-term Treasury yield. Defaults to `0.04`. The rate mostly affects rho and
long-dated theta; leaving the default is fine for short-dated analysis.

On Windows, Hermes keeps its data under `%LOCALAPPDATA%\hermes`, so the skill
path is `%LOCALAPPDATA%\hermes\skills\finance\options-chain\scripts\` rather than
`~/.hermes/skills/finance/options-chain/scripts/`. The examples below use the POSIX
form; substitute accordingly.

## How to Run

Invoke through the `terminal` tool:

```
SCRIPT=~/.hermes/skills/finance/options-chain/scripts/options_client.py
python $SCRIPT chain AAPL --near 5
```

Every command prints JSON on stdout. Read the script itself with `read_file` if
you need to confirm a formula.

## Quick Reference

```
python $SCRIPT expirations NVDA
python $SCRIPT chain NVDA --expiry 2026-01-16 --type call --near 8
python $SCRIPT chain SPY --type put --min-oi 500
python $SCRIPT iv TSLA --expiries 6
```

## Procedure

1. **Pick the expiry.** Run `expirations SYMBOL` to see listed dates with days
   to expiry. Omitting `--expiry` on `chain` selects the nearest expiry that has
   not passed its 16:00 ET cutoff.
2. **Pull the chain.** `chain SYMBOL --near N` keeps the N strikes closest to
   spot, which is what almost every question actually needs — a full chain can
   run to hundreds of contracts and wastes context.
3. **Filter for tradability.** Add `--min-oi 100` (or higher) to drop contracts
   nobody holds. Check `spread_pct` on each row: under ~5% is liquid, over ~20%
   means the mid price is fiction.
4. **Read the greeks.** `delta` is per share (multiply by 100 per contract),
   `theta` is dollars per calendar day, `vega` is dollars per 1 IV point, `rho`
   is dollars per 1% rate move. `prob_itm` is N(d2), the risk-neutral chance of
   finishing in the money — not a real-world probability.
5. **Check the vol regime.** `iv SYMBOL` returns the ATM term structure and the
   25-delta skew per expiry. `term_shape` is `contango` (far IV > near, normal)
   or `backwardation` (near IV > far, usually an event or stress).
6. **Report numbers with their expiry and spot.** Greeks are meaningless
   detached from the underlying price they were computed at.

## Quick Reference: Output Fields

| Field | Meaning |
| --- | --- |
| `mid` | (bid + ask) / 2, falling back to last trade |
| `iv` / `iv_source` | Volatility used for greeks; `solved` (from mid) or `yahoo` |
| `theoretical` | Black-Scholes price at `iv` — equals `mid` when `iv_source` is `solved` |
| `spread_pct` | Bid-ask spread as a percentage of mid — the liquidity tell |
| `prob_itm` | N(d2) for calls, N(-d2) for puts |

## Pitfalls

- **The vendor endpoint is unofficial.** It rate-limits and changes without
  notice. Repeated failures usually mean a stale crumb — the client retries with
  a fresh session automatically.
- **Wide spreads poison everything downstream.** When `spread_pct` is large the
  solved IV and every greek derived from it are guesses. Say so rather than
  quoting six decimal places.
- **`iv_source: yahoo` means the mid was unusable** (no two-sided quote, or a
  price below intrinsic). Treat those greeks as lower confidence.
- **Greeks go flat at expiry.** A contract past its cutoff prices to intrinsic
  with zero gamma/theta/vega. That is correct, not a bug.
- **Dividend yield is approximated** from the trailing annual figure, and the
  model is European. American early-exercise premium on deep ITM puts and on
  calls into a dividend is not captured.
- **This is analysis, not advice.** Report what the numbers say; do not tell the
  user what to buy.

## Verification

```
python ~/.hermes/skills/finance/options-chain/scripts/options_client.py chain AAPL --near 2
```

Returns JSON with a numeric `spot` and a `contracts` array. Sanity check: for a
matched call and put at the same strike, `delta_call - delta_put` should be
close to 1.0, and their `gamma` and `vega` should match.
