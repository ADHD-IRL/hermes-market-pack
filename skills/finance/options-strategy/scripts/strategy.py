#!/usr/bin/env python3
"""strategy.py - Expiry payoff analysis for multi-leg option positions.

Takes a set of legs and reports net debit/credit, max profit, max loss,
breakevens, a payoff table, and (given IV and days to expiry) a lognormal
probability of profit. Pure arithmetic on stdlib - no network, no market data,
no broker. Everything is evaluated at expiry; this does not model the P/L of
closing early.
"""

from __future__ import annotations

import argparse
import json
import math
import sys

OPTION_MULTIPLIER = 100
UNBOUNDED = "unlimited"


class LegError(ValueError):
    """Raised when a leg specification cannot be parsed."""


# ---------------------------------------------------------------------------
# Leg parsing
# ---------------------------------------------------------------------------


def parse_leg(spec: str) -> dict:
    """Parse `side,kind,strike,premium[,qty]` into a leg dict.

    Stock legs use the entry price in the strike position and `0` premium:
    `buy,stock,150,0,100`.
    """
    parts = [p.strip().lower() for p in spec.split(",")]
    if len(parts) not in (4, 5):
        raise LegError(f"Leg needs 4 or 5 comma-separated fields, got {len(parts)}: {spec!r}")

    side, kind = parts[0], parts[1]
    if side not in ("buy", "sell"):
        raise LegError(f"Leg side must be 'buy' or 'sell', got {side!r}")
    if kind not in ("call", "put", "stock"):
        raise LegError(f"Leg kind must be 'call', 'put' or 'stock', got {kind!r}")

    try:
        strike = float(parts[2])
        premium = float(parts[3])
        qty = float(parts[4]) if len(parts) == 5 else 1.0
    except ValueError as exc:
        raise LegError(f"Non-numeric value in leg {spec!r}: {exc}") from exc

    if strike <= 0:
        raise LegError(f"Strike/entry must be positive in leg {spec!r}")
    if premium < 0:
        raise LegError(f"Premium cannot be negative in leg {spec!r}")
    if qty <= 0:
        raise LegError(f"Quantity must be positive in leg {spec!r}")

    return {
        "side": side,
        "kind": kind,
        "strike": strike,
        "premium": premium,
        "qty": qty,
        "multiplier": 1 if kind == "stock" else OPTION_MULTIPLIER,
    }


def leg_payoff(leg: dict, price: float) -> float:
    """P/L of a single leg at underlying `price` on expiry day."""
    kind, strike = leg["kind"], leg["strike"]
    if kind == "call":
        intrinsic = max(price - strike, 0.0)
    elif kind == "put":
        intrinsic = max(strike - price, 0.0)
    else:  # stock: "intrinsic" is the share price itself
        intrinsic = price

    sign = 1.0 if leg["side"] == "buy" else -1.0
    cost = leg["strike"] if kind == "stock" else leg["premium"]
    return sign * (intrinsic - cost) * leg["qty"] * leg["multiplier"]


def total_payoff(legs: list[dict], price: float) -> float:
    return sum(leg_payoff(leg, price) for leg in legs)


def net_cash(legs: list[dict]) -> float:
    """Cash flow at entry. Negative = net debit paid, positive = net credit."""
    total = 0.0
    for leg in legs:
        cost = leg["strike"] if leg["kind"] == "stock" else leg["premium"]
        flow = -cost if leg["side"] == "buy" else cost
        total += flow * leg["qty"] * leg["multiplier"]
    return total


# ---------------------------------------------------------------------------
# Payoff geometry
# ---------------------------------------------------------------------------


def _price_grid(legs: list[dict], steps: int = 1200) -> list[float]:
    strikes = [leg["strike"] for leg in legs]
    # Start at zero: long stock and short puts keep losing all the way down,
    # so a grid floored above zero understates max loss.
    low = 0.0
    high = max(strikes) * 1.6
    step = (high - low) / steps
    return [low + i * step for i in range(steps + 1)]


def breakevens(legs: list[dict], tolerance: float = 1e-4) -> list[float]:
    """Underlying prices where total P/L crosses zero, found by bisection."""
    grid = _price_grid(legs)
    found: list[float] = []
    previous_price = grid[0]
    previous_pl = total_payoff(legs, previous_price)

    for price in grid[1:]:
        current_pl = total_payoff(legs, price)
        if previous_pl == 0.0:
            found.append(previous_price)
        elif previous_pl * current_pl < 0:
            low, high = previous_price, price
            for _ in range(80):
                mid = 0.5 * (low + high)
                if total_payoff(legs, low) * total_payoff(legs, mid) <= 0:
                    high = mid
                else:
                    low = mid
                if high - low < tolerance:
                    break
            found.append(0.5 * (low + high))
        previous_price, previous_pl = price, current_pl

    # Collapse near-duplicate roots from flat segments sitting on zero.
    unique: list[float] = []
    for value in sorted(found):
        if not unique or abs(value - unique[-1]) > 0.01:
            unique.append(value)
    return [round(v, 4) for v in unique]


def _slope(legs: list[dict], price: float) -> float:
    delta = max(price * 0.001, 0.01)
    return (total_payoff(legs, price + delta) - total_payoff(legs, price)) / delta


def extremes(legs: list[dict]) -> dict:
    """Max profit and max loss at expiry, flagging unbounded tails."""
    grid = _price_grid(legs)
    values = [total_payoff(legs, p) for p in grid]
    best_index = max(range(len(values)), key=lambda i: values[i])
    worst_index = min(range(len(values)), key=lambda i: values[i])

    # A non-flat slope at the far right means the position keeps gaining or
    # losing as the underlying runs; downside is capped at price zero.
    upside_slope = _slope(legs, grid[-1])
    max_profit: float | str = round(values[best_index], 2)
    max_loss: float | str = round(values[worst_index], 2)

    if upside_slope > 1e-6:
        max_profit = UNBOUNDED
    if upside_slope < -1e-6:
        max_loss = UNBOUNDED

    return {
        "max_profit": max_profit,
        "max_profit_at": None if max_profit == UNBOUNDED else round(grid[best_index], 2),
        "max_loss": max_loss,
        "max_loss_at": None if max_loss == UNBOUNDED else round(grid[worst_index], 2),
    }


# ---------------------------------------------------------------------------
# Probability of profit (lognormal terminal distribution)
# ---------------------------------------------------------------------------


def probability_of_profit(legs, spot, vol, days, rate=0.04, samples=4000) -> dict | None:
    """Risk-neutral P(profit) assuming lognormal terminal prices at expiry."""
    if not spot or not vol or vol <= 0 or days is None or days <= 0:
        return None

    t = days / 365.0
    sigma_t = vol * math.sqrt(t)
    drift = math.log(spot) + (rate - 0.5 * vol * vol) * t

    # Integrate over +/- 5 sigma of log-price, which captures ~99.99997%.
    low, high = drift - 5 * sigma_t, drift + 5 * sigma_t
    step = (high - low) / samples

    prob_profit = 0.0
    expected_pl = 0.0
    total_density = 0.0

    for i in range(samples + 1):
        log_price = low + i * step
        density = math.exp(-0.5 * ((log_price - drift) / sigma_t) ** 2) / (sigma_t * math.sqrt(2 * math.pi))
        weight = density * step
        pl = total_payoff(legs, math.exp(log_price))
        total_density += weight
        expected_pl += pl * weight
        if pl > 0:
            prob_profit += weight

    if total_density <= 0:
        return None
    return {
        "probability_of_profit": round(prob_profit / total_density, 4),
        "expected_pl": round(expected_pl / total_density, 2),
        "assumed_iv": vol,
        "days_to_expiry": days,
        "note": "Risk-neutral lognormal estimate, not a real-world probability.",
    }


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def preset_legs(name: str, args) -> list[dict]:
    """Build legs for common structures so strike ordering can't be inverted."""
    def leg(side, kind, strike, premium, qty=None):
        return parse_leg(f"{side},{kind},{strike},{premium},{qty or args.qty}")

    if name == "vertical":
        # Debit or credit spread; direction follows which strike is bought.
        kind = args.type
        long_leg = leg("buy", kind, args.long_strike, args.long_premium)
        short_leg = leg("sell", kind, args.short_strike, args.short_premium)
        return [long_leg, short_leg]

    if name == "covered-call":
        return [
            leg("buy", "stock", args.stock_price, 0, args.qty * 100),
            leg("sell", "call", args.short_strike, args.short_premium),
        ]

    if name == "cash-secured-put":
        return [leg("sell", "put", args.short_strike, args.short_premium)]

    if name == "straddle":
        side = "sell" if args.short else "buy"
        return [
            leg(side, "call", args.strike, args.call_premium),
            leg(side, "put", args.strike, args.put_premium),
        ]

    if name == "strangle":
        side = "sell" if args.short else "buy"
        return [
            leg(side, "call", args.call_strike, args.call_premium),
            leg(side, "put", args.put_strike, args.put_premium),
        ]

    if name == "iron-condor":
        if not (args.long_put_strike < args.short_put_strike < args.short_call_strike < args.long_call_strike):
            raise SystemExit(
                "Iron condor strikes must ascend: long put < short put < short call < long call"
            )
        return [
            leg("buy", "put", args.long_put_strike, args.long_put_premium),
            leg("sell", "put", args.short_put_strike, args.short_put_premium),
            leg("sell", "call", args.short_call_strike, args.short_call_premium),
            leg("buy", "call", args.long_call_strike, args.long_call_premium),
        ]

    raise SystemExit(f"Unknown preset: {name}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def payoff_table(legs: list[dict], spot: float | None, rows: int = 15) -> list[dict]:
    strikes = [leg["strike"] for leg in legs]
    center = spot or (sum(strikes) / len(strikes))
    low, high = center * 0.75, center * 1.25
    step = (high - low) / (rows - 1)
    return [
        {"price": round(low + i * step, 2), "pl": round(total_payoff(legs, low + i * step), 2)}
        for i in range(rows)
    ]


def analyze(legs: list[dict], spot=None, vol=None, days=None, rate=0.04) -> dict:
    cash = net_cash(legs)
    report = {
        "legs": [
            {
                "side": leg["side"],
                "kind": leg["kind"],
                "strike": leg["strike"],
                "premium": leg["premium"],
                "qty": leg["qty"],
            }
            for leg in legs
        ],
        "net_cash": round(cash, 2),
        "position": "credit" if cash > 0 else ("debit" if cash < 0 else "flat"),
        "breakevens": breakevens(legs),
    }
    report.update(extremes(legs))

    max_loss = report["max_loss"]
    max_profit = report["max_profit"]
    if isinstance(max_loss, (int, float)) and isinstance(max_profit, (int, float)) and max_loss < 0:
        report["risk_reward"] = round(abs(max_profit / max_loss), 3)

    if spot is not None:
        report["spot"] = spot
        report["pl_at_spot"] = round(total_payoff(legs, spot), 2)

    pop = probability_of_profit(legs, spot, vol, days, rate)
    if pop:
        report["probability"] = pop

    report["payoff_table"] = payoff_table(legs, spot)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Expiry payoff analysis for option positions.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--spot", type=float, help="Current underlying price")
        p.add_argument("--iv", type=float, help="Implied vol as a decimal, e.g. 0.28")
        p.add_argument("--days", type=float, help="Calendar days to expiry")
        p.add_argument("--rate", type=float, default=0.04)

    p_analyze = sub.add_parser("analyze", help="Analyze arbitrary legs")
    p_analyze.add_argument(
        "--leg",
        action="append",
        required=True,
        metavar="SIDE,KIND,STRIKE,PREMIUM[,QTY]",
        help="Repeatable. e.g. --leg sell,call,320,4.50,1",
    )
    add_common(p_analyze)

    p_preset = sub.add_parser("preset", help="Analyze a named structure")
    p_preset.add_argument(
        "name",
        choices=["vertical", "covered-call", "cash-secured-put", "straddle", "strangle", "iron-condor"],
    )
    p_preset.add_argument("--qty", type=float, default=1)
    p_preset.add_argument("--type", choices=["call", "put"], default="call")
    p_preset.add_argument("--strike", type=float)
    p_preset.add_argument("--long-strike", type=float)
    p_preset.add_argument("--short-strike", type=float)
    p_preset.add_argument("--long-premium", type=float, default=0.0)
    p_preset.add_argument("--short-premium", type=float, default=0.0)
    p_preset.add_argument("--call-strike", type=float)
    p_preset.add_argument("--put-strike", type=float)
    p_preset.add_argument("--call-premium", type=float, default=0.0)
    p_preset.add_argument("--put-premium", type=float, default=0.0)
    p_preset.add_argument("--stock-price", type=float)
    p_preset.add_argument("--long-put-strike", type=float)
    p_preset.add_argument("--short-put-strike", type=float)
    p_preset.add_argument("--short-call-strike", type=float)
    p_preset.add_argument("--long-call-strike", type=float)
    p_preset.add_argument("--long-put-premium", type=float, default=0.0)
    p_preset.add_argument("--short-put-premium", type=float, default=0.0)
    p_preset.add_argument("--short-call-premium", type=float, default=0.0)
    p_preset.add_argument("--long-call-premium", type=float, default=0.0)
    p_preset.add_argument("--short", action="store_true", help="Sell the structure instead of buying")
    add_common(p_preset)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "analyze":
            legs = [parse_leg(spec) for spec in args.leg]
        else:
            legs = preset_legs(args.name, args)
    except LegError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    print(json.dumps(analyze(legs, args.spot, args.iv, args.days, args.rate), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
