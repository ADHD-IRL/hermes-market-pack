# Hermes Market Pack ☤

Five trading skills for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
Nous Research's self-improving AI agent. Drop them into `~/.hermes/skills/` and
your agent can price options, model spreads, size positions against a real risk
budget, keep a trade journal, and deliver a scheduled market brief to Telegram.

This is an **extension pack**, not a fork. Hermes stays on upstream; these skills
sit alongside the bundled ones and survive `hermes update`.

> **Read-only by design.** Nothing here connects to a broker or places an order.
> These skills analyse and record — every decision stays with you. Nothing in
> this repository is financial advice.

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
both installers and all five skills resolve this the same way Hermes itself
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
```

The skills compose. A typical flow: `options-chain` for real premiums →
`options-strategy` for the payoff → `position-sizing` for the contract count →
`trade-journal` to record it → `market-brief` to watch it.

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
```

## Scheduled briefs

`cron/schedules.example.yaml` has ready-made jobs: a pre-open brief, a
post-close recap, a Friday performance review, and a Monday volatility check.
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

`$HERMES_HOME` defaults to `~/.hermes` on Linux and macOS, and to
`%LOCALAPPDATA%\hermes` on Windows.

Your journal is a local SQLite file. It is never transmitted anywhere.

## Tests

```bash
pip install pytest
./run_tests.sh
```

221 tests, standard library and `unittest.mock` only, no live network calls.
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

## License

MIT — see [LICENSE](LICENSE). Built for [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
