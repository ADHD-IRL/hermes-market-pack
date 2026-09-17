# Hermes Market Pack ☤

Six trading skills for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
Nous Research's self-improving AI agent. Drop them into `~/.hermes/skills/` and
your agent can price options, model spreads, size positions against a real risk
budget, keep a trade journal, follow a falling price to a considered entry, and
deliver a scheduled market brief to Telegram.

This is an **extension pack**, not a fork. Hermes stays on upstream; these skills
sit alongside the bundled ones and survive `hermes update`.

> **No broker, no orders.** Nothing here connects to a broker or places an order.
> Five skills analyse and record. The sixth, `trailing-buy`, goes one step further
> and emits a *validated order spec* — a proposal your agent hands to a broker MCP
> only after you approve it. Placing it is always a separate, explicit act.
> Every decision stays with you. Nothing in this repository is financial advice.

## Install

### Linux, macOS, WSL2

```bash
git clone https://github.com/ADHD-IRL/hermes-market-pack.git
cd hermes-market-pack
./install.sh
echo -e "AAPL\nNVDA\nSPY" >> ~/.hermes/watchlist.txt
```

`install.sh` symlinks the skills into `$HERMES_HOME/skills/finance/` (default
`~/.hermes/skills/finance/`), so edits here take effect immediately. Use
`--copy` for a snapshot instead, or `--uninstall` to remove them.

### Windows (native, PowerShell)

```powershell
git clone https://github.com/ADHD-IRL/hermes-market-pack.git
cd hermes-market-pack
.\install.ps1
"AAPL" | Out-File -Append -Encoding utf8 "$env:LOCALAPPDATA\hermes\watchlist.txt"
```

If PowerShell blocks the script, run it as
`powershell -ExecutionPolicy Bypass -File .\install.ps1`.

Native Windows keeps Hermes under `%LOCALAPPDATA%\hermes`, not `~/.hermes` —
both installers and all six skills resolve this the same way Hermes itself
does. `install.ps1` **copies** rather than symlinks, because symbolic links on
Windows need Developer Mode or an elevated shell; re-run it after pulling
updates, or pass `-Link` if you do have Developer Mode on. `-Uninstall`
removes the skills.

Git Bash works too — `./install.sh` detects MSYS/Cygwin and resolves the same
`%LOCALAPPDATA%` path, also defaulting to copy.

Uninstalling never touches your trade journal.

**Requirements:** Python 3.10+ and nothing else. No API keys, no pip installs —
every skill is standard library only. The Hermes installer already puts a
suitable Python on your PATH.

## The skills

| Skill | What it does |
| --- | --- |
| **options-chain** | Option chains with delta, gamma, theta, vega and rho computed locally, plus IV term structure and skew |
| **options-strategy** | Expiry payoff for any multi-leg position: max profit, max loss, breakevens, probability of profit |
| **position-sizing** | Share and contract counts from a risk budget; Kelly, portfolio heat, R-multiples |
| **trade-journal** | SQLite trade log scored in R-multiples, with win rate, expectancy and profit factor |
| **market-brief** | Scheduled digest of indices, watchlist and open positions, with stop alerts, unusual-volume flags and an implied-vs-realised vol read |
| **trailing-buy** | Follows a falling price, proposes an ATR-sized entry off the low behind hard guardrails, and reports the result against a plain limit order |

### Why compute greeks locally

The free market-data endpoints publish strikes, bids, asks, open interest and a
single implied-volatility number — but no greeks. `options-chain` fetches the
chain and prices it with Black-Scholes-Merton, solving implied vol from the mid
price by bisection and cross-checking it against the vendor's figure. When the
two disagree, `iv_source` tells you which one was used.

That means a delta you can trust the provenance of, and a `theoretical` price
you can compare against the mid to spot a mispriced or stale quote.

## Using it

Once installed, just talk to Hermes:

```
you>  what does the NVDA option chain look like for next month?
you>  if I sell the 95 put for 2.10, what's my max loss and breakeven?
you>  I have $50k and risk 1% per trade — how many contracts is that?
you>  log that: bought 400 AAPL at 50, stop 47.50
you>  how have I been trading this quarter?
you>  watch AGNC — if it drops through 9.98, follow it down and tell me when to buy
```

The skills compose. A typical flow: `options-chain` for real premiums →
`options-strategy` for the payoff → `position-sizing` for the contract count →
`trade-journal` to record it → `market-brief` to watch it. For an equity entry:
`trailing-buy` to find the price → `position-sizing` to check the size →
`trade-journal` once it fills.

### Following a drop instead of sitting in front of it

A resting buy limit at 9.98 fills the moment price falls through it. `trailing-buy`
follows the drop instead — it arms at 9.98, tracks the running low, and proposes an
entry only when price recovers by an ATR-sized amount off that low, with a hard floor
underneath.

The split matters. A chat agent cannot watch a chart; it only acts when prompted. So
the polling, the trailing arithmetic and every guardrail are plain Python that runs on
each tick and costs nothing. The model is consulted once, at the trigger, to answer
what the rules cannot: is this ex-dividend day, is the whole sector selling off, is
there news? Its answer comes back through `decide` — which refuses any decision that
does not cite the tick ids behind it — and is then checked against limits it has no
way to reach.

Run it read-only for a few weeks first. `--paper` records what it *would* have bought
at and re-arms, so a fortnight produces a series rather than one event, with every
guardrail still running. `report` then renders those entries against what your plain
limit would have got. `cron/agnc-phase1.yaml` is a complete two-week trial you can hand
to `hermes cron add` — setup, session polling, daily recap, weekly report, and a review
at the end that is allowed to conclude the sample is too small to say anything.

### Running the scripts directly

Every skill is a plain CLI that prints JSON, usable without Hermes:

```bash
S=~/.hermes/skills/finance
python $S/options-chain/scripts/options_client.py chain AAPL --near 5 --min-oi 100
python $S/options-chain/scripts/options_client.py iv TSLA --expiries 6

python $S/options-strategy/scripts/strategy.py preset iron-condor \
  --long-put-strike 90 --short-put-strike 95 \
  --short-call-strike 105 --long-call-strike 110 \
  --long-put-premium 1 --short-put-premium 2 \
  --short-call-premium 2 --long-call-premium 1

python $S/position-sizing/scripts/sizing.py shares \
  --account 100000 --risk-pct 1 --entry 50 --stop 47.50

python $S/trade-journal/scripts/journal.py open \
  --symbol AAPL --side long --qty 400 --entry 50 --stop 47.50 --strategy breakout
python $S/trade-journal/scripts/journal.py stats --since 2026-01-01

python $S/market-brief/scripts/brief.py

python $S/trailing-buy/scripts/trailing_buy.py plan create \
  --symbol AGNC --arm 9.98 --floor 9.20 --shares 200
python $S/trailing-buy/scripts/trailing_buy.py tick --plan 1
python $S/trailing-buy/scripts/trailing_buy.py report --plan 1 --out ~/agnc-trail.html
```

## Scheduled briefs

`cron/schedules.example.yaml` has ready-made jobs: a pre-open brief, a
post-close recap, a Friday performance review, a Monday volatility check, and a
trailing-buy poll and recap. `cron/start-phase1.sh` sets up a complete
two-week read-only trailing-buy trial in one command, and
`cron/agnc-phase1.yaml` explains every number it uses.

Linux, macOS, WSL2:

```bash
./install.sh                        # if you have not already
./cron/start-phase1.sh --dry-run    # see the commands first
./cron/start-phase1.sh              # AGNC 9.98 / 9.20 / 200 shares, paper mode
```

Windows, PowerShell:

```powershell
.\install.ps1                        # copies, so re-run after every git pull
.\cron\start-phase1.ps1 -DryRun      # see the commands first
.\cron\start-phase1.ps1             # AGNC 9.98 / 9.20 / 200 shares, paper mode
```

Both take a different name and levels: `NLY 18.50 17.00 100` positionally in
bash, `-Symbol NLY -Arm 18.50 -Floor 17.00 -Shares 100` in PowerShell. The
PowerShell version resolves `%LOCALAPPDATA%\hermes`, finds `python` or `py -3`
rather than `python3`, and formats every number with the invariant culture so a
machine set to comma decimals still passes `9.98` and not `9,98`.

The polling job uses Hermes' `--monitor-script` mode: a cheap script runs the tick
every five minutes and Hermes hashes its output, so an unchanged digest suppresses
the agent run entirely. The rules poll all session for nothing and the model is
woken only when a plan actually changes state.
Hermes' cron scheduler takes natural-language prompts and delivers to whichever
platform you have wired up.

```
hermes cron add
```

## Configuration

Nothing is required. Optional environment variables (put them in
`~/.hermes/.env`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `RISK_FREE_RATE` | `0.04` | Rate used for greeks; affects rho and long-dated theta |
| `WATCHLIST_FILE` | `$HERMES_HOME/watchlist.txt` | Where the watchlist lives |
| `TRADE_JOURNAL_DB` | `$HERMES_HOME/trade_journal.db` | Where the journal lives |
| `TRAILING_BUY_DB` | `$HERMES_HOME/trailing_buy.db` | Where trailing-buy plans and their tick log live |
| `TRAILING_BUY_HALT_FILE` | `$HERMES_HOME/trailing_buy.HALT` | Kill switch; while this file exists every trailing-buy command refuses |

`$HERMES_HOME` defaults to `~/.hermes` on Linux and macOS, and to
`%LOCALAPPDATA%\hermes` on Windows.

Your journal is a local SQLite file. It is never transmitted anywhere.

## Tests

```bash
pip install pytest
./run_tests.sh
```

362 tests, standard library and `unittest.mock` only, no live network calls.
The option pricing is checked against put-call parity and known payoff
structures — a 100/110 call debit spread paid for $3.00 must show a $300 max
loss, a $700 max profit and a breakeven at 103.

## Limitations

Worth knowing before you rely on any of this:

- **Market data is unofficial and delayed.** The endpoints rate-limit and change
  without notice.
- **The pricing model is European.** American early-exercise premium is not
  captured, which matters for deep ITM puts and for calls into a dividend.
- **Payoffs are at expiry.** Closing a position early is not modelled.
- **Probability of profit is risk-neutral**, derived from implied vol. It is an
  estimate, not a forecast.
- **Fees, commissions and slippage are excluded** everywhere.
- **Small samples lie.** Twenty journalled trades cannot separate an edge from
  luck.
- **Implied-vs-realised vol is context, not a signal.** Options priced above
  realised vol usually have a reason — earnings, a pending decision, a catalyst.
  "Rich" means find out why, not sell premium.
- **Polling misses fast wicks.** `trailing-buy` sees the low it polled, not the low
  that printed. It is built for minutes-scale moves, not scalping.
- **There is no holiday calendar.** Market-hours gating reads `marketState` off the
  quote itself.
- **The next ex-dividend date is projected from past cadence**, not published by the
  vendor. Check the company's own calendar before leaning on it.
- **An order spec is a proposal, and the losses are yours.** If you run a plan with
  `--autonomous`, specs are emitted without waiting for your approval. Every other
  guardrail still applies, but the person in the loop does not.

## License

MIT — see [LICENSE](LICENSE). Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
