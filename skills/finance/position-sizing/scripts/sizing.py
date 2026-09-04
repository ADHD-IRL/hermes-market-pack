#!/usr/bin/env python3
"""sizing.py - Risk-first position sizing.

Answers "how many" from a risk budget rather than from a dollar amount: share
count from a stop distance, contract count from a defined max loss, Kelly and
fractional-Kelly fractions, portfolio heat across open positions, and realised
R-multiples. Pure arithmetic on stdlib - no market data, no broker.
"""

from __future__ import annotations

import argparse
import json
import sys


def _risk_budget(account: float, risk_pct: float) -> float:
    """Dollars at risk for one trade."""
    if account <= 0:
        raise SystemExit("Account equity must be positive.")
    if not 0 < risk_pct <= 100:
        raise SystemExit("Risk percent must be between 0 and 100.")
    if risk_pct > 5:
        # Not fatal, but worth surfacing: >5% per trade is far outside
        # conventional risk-of-ruin guidance.
        print(f"warning: risking {risk_pct}% on a single trade is very aggressive", file=sys.stderr)
    return account * (risk_pct / 100.0)


def cmd_shares(args) -> dict:
    """Share count from entry, stop and a percent-of-account risk budget."""
    budget = _risk_budget(args.account, args.risk_pct)
    per_share = abs(args.entry - args.stop)
    if per_share == 0:
        raise SystemExit("Entry and stop cannot be equal - risk per share would be zero.")

    raw = budget / per_share
    shares = int(raw)  # never round up past the risk budget
    direction = "long" if args.stop < args.entry else "short"
    notional = shares * args.entry

    result = {
        "direction": direction,
        "entry": args.entry,
        "stop": args.stop,
        "risk_budget": round(budget, 2),
        "risk_per_share": round(per_share, 4),
        "shares": shares,
        "actual_risk": round(shares * per_share, 2),
        "notional": round(notional, 2),
        "notional_pct_of_account": round(100.0 * notional / args.account, 2),
        "stop_distance_pct": round(100.0 * per_share / args.entry, 2),
    }
    if shares == 0:
        result["warning"] = "Risk budget is smaller than one share's risk. Widen the budget or tighten the stop."
    if args.target is not None:
        reward = abs(args.target - args.entry)
        result["target"] = args.target
        result["reward_per_share"] = round(reward, 4)
        result["reward_risk_ratio"] = round(reward / per_share, 2)
    return result


def cmd_contracts(args) -> dict:
    """Contract count from a defined max loss per contract."""
    budget = _risk_budget(args.account, args.risk_pct)
    if args.max_loss <= 0:
        raise SystemExit("Max loss per contract must be positive (pass it as a positive number).")

    contracts = int(budget / args.max_loss)
    result = {
        "risk_budget": round(budget, 2),
        "max_loss_per_contract": args.max_loss,
        "contracts": contracts,
        "actual_risk": round(contracts * args.max_loss, 2),
        "budget_used_pct": round(100.0 * contracts * args.max_loss / budget, 2) if budget else 0.0,
    }
    if contracts == 0:
        result["warning"] = (
            "One contract already exceeds the risk budget. Use a narrower spread, "
            "a cheaper strike, or accept a smaller risk percentage."
        )
    if args.max_profit is not None:
        result["max_profit_per_contract"] = args.max_profit
        result["total_max_profit"] = round(contracts * args.max_profit, 2)
        result["reward_risk_ratio"] = round(args.max_profit / args.max_loss, 2)
    return result


def cmd_kelly(args) -> dict:
    """Kelly fraction and the fractional-Kelly stake most traders actually use."""
    win_rate = args.win_rate
    if win_rate > 1:
        win_rate /= 100.0  # accept 55 as well as 0.55
    if not 0 < win_rate < 1:
        raise SystemExit("Win rate must be between 0 and 1 (or 1 and 100).")
    if args.win_loss_ratio <= 0:
        raise SystemExit("Win/loss ratio must be positive.")

    b = args.win_loss_ratio
    loss_rate = 1.0 - win_rate
    full_kelly = (win_rate * b - loss_rate) / b
    expectancy = win_rate * b - loss_rate  # in R units per trade

    result = {
        "win_rate": round(win_rate, 4),
        "win_loss_ratio": b,
        "expectancy_r": round(expectancy, 4),
        "full_kelly": round(full_kelly, 4),
        "fraction": args.fraction,
        "fractional_kelly": round(full_kelly * args.fraction, 4),
        "suggested_risk_pct": round(max(full_kelly * args.fraction, 0.0) * 100, 2),
    }
    if full_kelly <= 0:
        result["warning"] = (
            "Negative edge: this win rate and payoff lose money over time. "
            "Correct size is zero."
        )
    else:
        result["note"] = (
            "Full Kelly maximises growth but suffers deep drawdowns. "
            f"This is {args.fraction:g}x Kelly."
        )
    if args.account:
        result["account"] = args.account
        result["suggested_risk_dollars"] = round(
            args.account * max(full_kelly * args.fraction, 0.0), 2
        )
    return result


def _parse_position(spec: str) -> dict:
    """Parse `name:risk_dollars` for the heat calculation."""
    if ":" not in spec:
        raise SystemExit(f"Position must be NAME:RISK, got {spec!r}")
    name, _, risk = spec.rpartition(":")
    try:
        risk_value = float(risk)
    except ValueError as exc:
        raise SystemExit(f"Non-numeric risk in {spec!r}") from exc
    if risk_value < 0:
        raise SystemExit(f"Risk cannot be negative in {spec!r}")
    return {"name": name, "risk": risk_value}


def cmd_heat(args) -> dict:
    """Total open risk across positions as a percentage of account equity."""
    if args.account <= 0:
        raise SystemExit("Account equity must be positive.")
    positions = [_parse_position(p) for p in args.position]
    total = sum(p["risk"] for p in positions)
    heat_pct = 100.0 * total / args.account

    if heat_pct <= 2:
        assessment = "conservative"
    elif heat_pct <= 6:
        assessment = "normal"
    elif heat_pct <= 10:
        assessment = "elevated"
    else:
        assessment = "excessive"

    return {
        "account": args.account,
        "positions": [
            {**p, "pct_of_account": round(100.0 * p["risk"] / args.account, 2)} for p in positions
        ],
        "total_risk": round(total, 2),
        "portfolio_heat_pct": round(heat_pct, 2),
        "assessment": assessment,
        "remaining_budget": round(args.account * (args.max_heat / 100.0) - total, 2),
        "max_heat_pct": args.max_heat,
    }


def cmd_r_multiple(args) -> dict:
    """Realised R: how many multiples of initial risk the trade returned."""
    risk_per_share = abs(args.entry - args.stop)
    if risk_per_share == 0:
        raise SystemExit("Entry and stop cannot be equal.")

    direction = "long" if args.stop < args.entry else "short"
    move = (args.exit - args.entry) if direction == "long" else (args.entry - args.exit)
    r = move / risk_per_share

    return {
        "direction": direction,
        "entry": args.entry,
        "stop": args.stop,
        "exit": args.exit,
        "risk_per_share": round(risk_per_share, 4),
        "pl_per_share": round(move, 4),
        "r_multiple": round(r, 3),
        "outcome": "win" if r > 0 else ("breakeven" if r == 0 else "loss"),
        "total_pl": round(move * args.shares, 2) if args.shares else None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Risk-first position sizing.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_shares = sub.add_parser("shares", help="Share count from entry, stop and risk budget")
    p_shares.add_argument("--account", type=float, required=True)
    p_shares.add_argument("--risk-pct", type=float, default=1.0)
    p_shares.add_argument("--entry", type=float, required=True)
    p_shares.add_argument("--stop", type=float, required=True)
    p_shares.add_argument("--target", type=float)
    p_shares.set_defaults(func=cmd_shares)

    p_contracts = sub.add_parser("contracts", help="Contract count from a defined max loss")
    p_contracts.add_argument("--account", type=float, required=True)
    p_contracts.add_argument("--risk-pct", type=float, default=1.0)
    p_contracts.add_argument("--max-loss", type=float, required=True, help="Per contract, positive")
    p_contracts.add_argument("--max-profit", type=float)
    p_contracts.set_defaults(func=cmd_contracts)

    p_kelly = sub.add_parser("kelly", help="Kelly and fractional-Kelly sizing")
    p_kelly.add_argument("--win-rate", type=float, required=True, help="0.55 or 55")
    p_kelly.add_argument("--win-loss-ratio", type=float, required=True, help="Avg win / avg loss")
    p_kelly.add_argument("--fraction", type=float, default=0.25, help="Kelly fraction, default 0.25")
    p_kelly.add_argument("--account", type=float)
    p_kelly.set_defaults(func=cmd_kelly)

    p_heat = sub.add_parser("heat", help="Total open risk across positions")
    p_heat.add_argument("--account", type=float, required=True)
    p_heat.add_argument("--position", action="append", required=True, metavar="NAME:RISK")
    p_heat.add_argument("--max-heat", type=float, default=6.0)
    p_heat.set_defaults(func=cmd_heat)

    p_r = sub.add_parser("r-multiple", help="Realised R-multiple for a closed trade")
    p_r.add_argument("--entry", type=float, required=True)
    p_r.add_argument("--stop", type=float, required=True)
    p_r.add_argument("--exit", type=float, required=True)
    p_r.add_argument("--shares", type=float)
    p_r.set_defaults(func=cmd_r_multiple)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(args.func(args), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
