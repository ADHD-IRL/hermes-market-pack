#!/usr/bin/env python3
"""options_client.py - Options chain data + locally computed greeks.

Yahoo Finance publishes strikes, bids/asks, volume, open interest and an
implied-volatility figure, but *no greeks*. This client fetches the chain and
computes delta/gamma/theta/vega/rho locally with Black-Scholes-Merton, plus a
bisection implied-vol solver used to cross-check Yahoo's number.

Python stdlib only. Read-only: no order placement, no broker account.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar

USER_AGENT = "Mozilla/5.0 (compatible; HermesAgent/1.0)"
YF1 = "https://query1.finance.yahoo.com"
YF2 = "https://query2.finance.yahoo.com"

MAX_RETRIES = 3
SECONDS_PER_YEAR = 365.0 * 24 * 3600
# US equity options stop trading 16:00 America/New_York. Yahoo's `expiration`
# is UTC midnight of expiry day, so add ~20h to land on the real cutoff.
EXPIRY_CUTOFF_SECONDS = 20 * 3600

_cookie_jar = CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))
_crumb: str | None = None


# ---------------------------------------------------------------------------
# HTTP / Yahoo session
# ---------------------------------------------------------------------------


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with _opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _fetch_crumb() -> str | None:
    """Establish Yahoo's cookie session and grab a crumb token.

    Yahoo rejects the options endpoint with HTTP 401 unless a session cookie
    has been set. Touching fc.yahoo.com raises an HTTPError but still delivers
    the cookie, so the exception is expected and swallowed.
    """
    global _crumb
    if _crumb is not None:
        return _crumb
    try:
        _get("https://fc.yahoo.com")
    except Exception:
        pass
    for base in (YF1, YF2):
        try:
            candidate = _get(f"{base}/v1/test/getcrumb").strip()
            if candidate and "<" not in candidate:
                _crumb = candidate
                return _crumb
        except Exception:
            continue
    return None


def fetch_chain(symbol: str, expiry_ts: int | None = None) -> dict:
    """Fetch a raw option chain payload for `symbol`."""
    params: dict[str, str | int] = {}
    crumb = _fetch_crumb()
    if crumb:
        params["crumb"] = crumb
    if expiry_ts is not None:
        params["date"] = expiry_ts

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        for base in (YF2, YF1):
            url = f"{base}/v7/finance/options/{urllib.parse.quote(symbol.upper())}"
            if params:
                url += "?" + urllib.parse.urlencode(params)
            try:
                payload = json.loads(_get(url))
            except Exception as exc:  # network hiccup or non-JSON body
                last_error = exc
                continue
            chain = payload.get("optionChain") or {}
            if chain.get("error"):
                raise SystemExit(f"Yahoo error for {symbol}: {chain['error']}")
            results = chain.get("result") or []
            if results:
                return results[0]
            last_error = ValueError(f"no chain data for {symbol}")
        if attempt < MAX_RETRIES - 1:
            _reset_session()
    raise SystemExit(f"Could not fetch options for {symbol}: {last_error}")


def _reset_session() -> None:
    global _crumb
    _crumb = None
    _cookie_jar.clear()


# ---------------------------------------------------------------------------
# Black-Scholes-Merton
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(spot, strike, t_years, vol, rate, div=0.0, kind="call") -> float:
    """Black-Scholes-Merton price. Falls back to intrinsic at T=0 or vol=0."""
    if t_years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        intrinsic = spot - strike if kind == "call" else strike - spot
        return max(intrinsic, 0.0)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc_r = math.exp(-rate * t_years)
    disc_q = math.exp(-div * t_years)
    if kind == "call":
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)


def greeks(spot, strike, t_years, vol, rate, div=0.0, kind="call") -> dict:
    """Delta, gamma, theta (per day), vega and rho (per 1 vol/rate point)."""
    if t_years <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        if kind == "call":
            delta = 1.0 if spot > strike else 0.0
        else:
            delta = -1.0 if spot < strike else 0.0
        return {
            "delta": delta,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "rho": 0.0,
            "prob_itm": 1.0 if delta else 0.0,
        }

    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc_r = math.exp(-rate * t_years)
    disc_q = math.exp(-div * t_years)
    pdf_d1 = _norm_pdf(d1)

    gamma = disc_q * pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * disc_q * pdf_d1 * sqrt_t / 100.0  # per 1 percentage point of IV
    common_theta = -(spot * disc_q * pdf_d1 * vol) / (2.0 * sqrt_t)

    if kind == "call":
        delta = disc_q * _norm_cdf(d1)
        theta = common_theta - rate * strike * disc_r * _norm_cdf(d2) + div * spot * disc_q * _norm_cdf(d1)
        rho = strike * t_years * disc_r * _norm_cdf(d2) / 100.0
        prob_itm = _norm_cdf(d2)
    else:
        delta = disc_q * (_norm_cdf(d1) - 1.0)
        theta = common_theta + rate * strike * disc_r * _norm_cdf(-d2) - div * spot * disc_q * _norm_cdf(-d1)
        rho = -strike * t_years * disc_r * _norm_cdf(-d2) / 100.0
        prob_itm = _norm_cdf(-d2)

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta / 365.0,  # per calendar day
        "vega": vega,
        "rho": rho,
        "prob_itm": prob_itm,
    }


def implied_vol(price, spot, strike, t_years, rate, div=0.0, kind="call") -> float | None:
    """Solve for implied volatility by bisection. None when unsolvable."""
    if price is None or price <= 0 or t_years <= 0 or spot <= 0 or strike <= 0:
        return None
    # The no-arbitrage floor for a European option is the *discounted*
    # intrinsic value. An ITM European put legitimately trades below K - S,
    # so comparing against undiscounted intrinsic wrongly rejects it.
    forward = spot * math.exp(-div * t_years)
    discounted_strike = strike * math.exp(-rate * t_years)
    if kind == "call":
        floor = max(forward - discounted_strike, 0.0)
    else:
        floor = max(discounted_strike - forward, 0.0)
    if price < floor - 1e-6:
        return None  # below the no-arbitrage floor: stale or crossed quote

    low, high = 1e-4, 5.0
    if bs_price(spot, strike, t_years, high, rate, div, kind) < price:
        return None  # beyond 500% vol; treat as unsolvable
    for _ in range(100):
        mid = 0.5 * (low + high)
        if bs_price(spot, strike, t_years, mid, rate, div, kind) < price:
            low = mid
        else:
            high = mid
        if high - low < 1e-6:
            break
    return 0.5 * (low + high)


# ---------------------------------------------------------------------------
# Contract enrichment
# ---------------------------------------------------------------------------


def _mid(bid, ask, last):
    """Mid price, falling back to last trade when a side is missing."""
    try:
        if bid and ask and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
    except (TypeError, ValueError):
        pass
    try:
        return float(last) if last else None
    except (TypeError, ValueError):
        return None


def _years_to_expiry(expiry_ts: int, now_ts: float | None = None) -> float:
    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    return max((expiry_ts + EXPIRY_CUTOFF_SECONDS - now) / SECONDS_PER_YEAR, 0.0)


def _round(value, digits=4):
    return None if value is None else round(value, digits)


def enrich(contract: dict, spot: float, rate: float, div: float, kind: str, now_ts=None) -> dict:
    """Attach mid price, time to expiry, solved IV and greeks to a contract."""
    strike = float(contract.get("strike") or 0.0)
    expiry_ts = int(contract.get("expiration") or 0)
    t_years = _years_to_expiry(expiry_ts, now_ts)
    mid = _mid(contract.get("bid"), contract.get("ask"), contract.get("lastPrice"))

    yahoo_iv = contract.get("impliedVolatility")
    try:
        yahoo_iv = float(yahoo_iv) if yahoo_iv else None
    except (TypeError, ValueError):
        yahoo_iv = None
    if yahoo_iv is not None and not (0.0 < yahoo_iv < 10.0):
        yahoo_iv = None

    solved_iv = implied_vol(mid, spot, strike, t_years, rate, div, kind)
    vol = solved_iv if solved_iv is not None else yahoo_iv

    row = {
        "contract": contract.get("contractSymbol"),
        "type": kind,
        "strike": strike,
        "expiry": datetime.fromtimestamp(expiry_ts, timezone.utc).strftime("%Y-%m-%d") if expiry_ts else None,
        "dte": round(t_years * 365.0, 1),
        "bid": contract.get("bid"),
        "ask": contract.get("ask"),
        "mid": _round(mid, 4),
        "last": contract.get("lastPrice"),
        "volume": contract.get("volume") or 0,
        "open_interest": contract.get("openInterest") or 0,
        "in_the_money": contract.get("inTheMoney"),
        "iv": _round(vol, 4),
        "iv_source": "solved" if solved_iv is not None else ("yahoo" if yahoo_iv is not None else None),
        "iv_yahoo": _round(yahoo_iv, 4),
    }

    if vol:
        row.update({k: _round(v, 6) for k, v in greeks(spot, strike, t_years, vol, rate, div, kind).items()})
        row["theoretical"] = _round(bs_price(spot, strike, t_years, vol, rate, div, kind), 4)
    return row


def _spread_pct(row) -> float | None:
    """Bid-ask spread as a percentage of mid - the liquidity tell."""
    try:
        bid, ask, mid = float(row["bid"]), float(row["ask"]), float(row["mid"])
    except (TypeError, ValueError, KeyError):
        return None
    if mid <= 0 or bid <= 0 or ask <= 0:
        return None
    return round(100.0 * (ask - bid) / mid, 2)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _spot_and_div(result: dict) -> tuple[float, float]:
    quote = result.get("quote") or {}
    spot = quote.get("regularMarketPrice")
    if not spot:
        raise SystemExit("Yahoo returned no spot price; cannot compute greeks.")
    # Yahoo exposes two dividend fields with different units:
    #   trailingAnnualDividendYield -> fraction (0.0032 == 0.32%)
    #   dividendYield               -> percent  (0.33   == 0.33%)
    # Prefer the fraction; only scale the percent field when falling back.
    div_yield = _as_float(quote.get("trailingAnnualDividendYield"))
    if div_yield is None:
        percent = _as_float(quote.get("dividendYield"))
        div_yield = percent / 100.0 if percent is not None else 0.0
    if not 0.0 <= div_yield <= 0.5:  # implausible yield: treat as unusable
        div_yield = 0.0
    return float(spot), div_yield


def cmd_expirations(args) -> dict:
    result = fetch_chain(args.symbol)
    spot, _ = _spot_and_div(result)
    now = datetime.now(timezone.utc).timestamp()
    rows = []
    for ts in result.get("expirationDates") or []:
        rows.append(
            {
                "expiry": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                "timestamp": ts,
                "dte": round(_years_to_expiry(ts, now) * 365.0, 1),
            }
        )
    return {"symbol": args.symbol.upper(), "spot": spot, "expirations": rows}


def _resolve_expiry(result: dict, wanted: str | None) -> int | None:
    dates = result.get("expirationDates") or []
    if not wanted:
        # Skip expiries already past their 16:00 ET cutoff - they only ever
        # price to intrinsic and their greeks are all zero.
        live = [ts for ts in dates if _years_to_expiry(ts) > 0]
        return (live or dates)[0] if dates else None
    for ts in dates:
        if datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") == wanted:
            return ts
    available = [datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in dates]
    raise SystemExit(f"Expiry {wanted} not listed. Available: {', '.join(available[:12])}")


def cmd_chain(args) -> dict:
    result = fetch_chain(args.symbol)
    expiry_ts = _resolve_expiry(result, args.expiry)
    if args.expiry:
        result = fetch_chain(args.symbol, expiry_ts)
    spot, div = _spot_and_div(result)
    rate = args.rate

    options = (result.get("options") or [{}])[0]
    rows: list[dict] = []
    if args.type in ("call", "both"):
        rows += [enrich(c, spot, rate, div, "call") for c in options.get("calls") or []]
    if args.type in ("put", "both"):
        rows += [enrich(p, spot, rate, div, "put") for p in options.get("puts") or []]

    for row in rows:
        row["spread_pct"] = _spread_pct(row)

    if args.near:
        rows.sort(key=lambda r: abs(r["strike"] - spot))
        rows = rows[: args.near * (2 if args.type == "both" else 1)]
    if args.min_oi:
        rows = [r for r in rows if (r.get("open_interest") or 0) >= args.min_oi]
    rows.sort(key=lambda r: (r["type"], r["strike"]))

    return {
        "symbol": args.symbol.upper(),
        "spot": spot,
        "dividend_yield": round(div, 4),
        "risk_free_rate": rate,
        "expiry": rows[0]["expiry"] if rows else None,
        "contracts": rows,
    }


def cmd_iv(args) -> dict:
    """ATM implied vol per expiry (term structure) plus put/call skew."""
    result = fetch_chain(args.symbol)
    spot, div = _spot_and_div(result)
    all_dates = result.get("expirationDates") or []
    term = []

    live_dates = [ts for ts in all_dates if _years_to_expiry(ts) > 0]
    for ts in live_dates[: args.expiries]:
        chain_result = fetch_chain(args.symbol, ts)
        options = (chain_result.get("options") or [{}])[0]
        calls = [enrich(c, spot, args.rate, div, "call") for c in options.get("calls") or []]
        puts = [enrich(p, spot, args.rate, div, "put") for p in options.get("puts") or []]
        if not calls and not puts:
            continue

        def atm(rows):
            usable = [r for r in rows if r.get("iv")]
            return min(usable, key=lambda r: abs(r["strike"] - spot)) if usable else None

        atm_call, atm_put = atm(calls), atm(puts)
        ivs = [r["iv"] for r in (atm_call, atm_put) if r]
        if not ivs:
            continue

        # 25-delta skew: OTM put IV minus OTM call IV. Positive = puts bid up.
        def by_delta(rows, target):
            usable = [r for r in rows if r.get("delta") is not None and r.get("iv")]
            return min(usable, key=lambda r: abs(abs(r["delta"]) - target)) if usable else None

        wing_put, wing_call = by_delta(puts, 0.25), by_delta(calls, 0.25)
        skew = None
        if wing_put and wing_call:
            candidate = wing_put["iv"] - wing_call["iv"]
            # Guard against thin wings: a skew past 100 vol points is a data
            # artefact, not a real risk reversal.
            if abs(candidate) < 1.0:
                skew = round(candidate, 4)

        term.append(
            {
                "expiry": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                "dte": round(_years_to_expiry(ts) * 365.0, 1),
                "atm_iv": round(sum(ivs) / len(ivs), 4),
                "atm_call_iv": atm_call["iv"] if atm_call else None,
                "atm_put_iv": atm_put["iv"] if atm_put else None,
                "skew_25d": skew,
            }
        )

    shape = None
    if len(term) >= 2 and term[0]["atm_iv"] and term[-1]["atm_iv"]:
        shape = "contango" if term[-1]["atm_iv"] > term[0]["atm_iv"] else "backwardation"

    return {"symbol": args.symbol.upper(), "spot": spot, "term_structure": term, "term_shape": shape}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Options chain data with locally computed greeks.")
    default_rate = float(os.environ.get("RISK_FREE_RATE", "0.04"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("expirations", help="List available expiry dates with DTE")
    p_exp.add_argument("symbol")
    p_exp.set_defaults(func=cmd_expirations)

    p_chain = sub.add_parser("chain", help="Option chain with greeks for one expiry")
    p_chain.add_argument("symbol")
    p_chain.add_argument("--expiry", help="YYYY-MM-DD (default: nearest expiry)")
    p_chain.add_argument("--type", choices=["call", "put", "both"], default="both")
    p_chain.add_argument("--near", type=int, default=0, help="Keep only N strikes nearest spot")
    p_chain.add_argument("--min-oi", type=int, default=0, help="Drop contracts below this open interest")
    p_chain.add_argument("--rate", type=float, default=default_rate)
    p_chain.set_defaults(func=cmd_chain)

    p_iv = sub.add_parser("iv", help="ATM IV term structure and 25-delta skew")
    p_iv.add_argument("symbol")
    p_iv.add_argument("--expiries", type=int, default=4, help="How many expiries to sample")
    p_iv.add_argument("--rate", type=float, default=default_rate)
    p_iv.set_defaults(func=cmd_iv)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(args.func(args), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
