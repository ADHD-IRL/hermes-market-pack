#!/usr/bin/env python3
"""brief.py - A scheduled market brief: indices, watchlist, movers, positions.

Built for the Hermes cron scheduler. Prints a short plain-text digest suitable
for delivery to Telegram or any other gateway platform, or JSON with --json.

If the trade-journal skill has a database, open positions are checked against
live prices and any position sitting near its stop is flagged. Read-only: this
never places or modifies an order.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sqlite3
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (compatible; HermesAgent/1.0)"
YF1 = "https://query1.finance.yahoo.com"
YF2 = "https://query2.finance.yahoo.com"
DEFAULT_INDICES = ["^GSPC", "^IXIC", "^DJI", "^VIX"]
BATCH_SIZE = 40
TRADING_DAYS = 252
REGULAR_SESSION_MINUTES = 390  # 09:30-16:00 America/New_York
SESSION_OPEN_MINUTE = 9 * 60 + 30
# Compare implied vol against a ~30-day expiry, the same horizon the volatility
# indices use, so it lines up with 20-30 day realized vol.
IV_TARGET_DTE = 30

_cookie_jar = CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))
_crumb: str | None = None


def hermes_home() -> Path:
    """Hermes home: HERMES_HOME, else the platform default.

    Mirrors hermes_constants._get_platform_default_hermes_home(): Windows keeps
    its data under %LOCALAPPDATA%\\hermes, not ~/.hermes.
    """
    override = os.environ.get("HERMES_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with _opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _fetch_crumb() -> str | None:
    global _crumb
    if _crumb is not None:
        return _crumb
    try:
        _get("https://fc.yahoo.com")
    except Exception:
        pass  # expected: this request errors but still sets the session cookie
    for base in (YF1, YF2):
        try:
            candidate = _get(f"{base}/v1/test/getcrumb").strip()
            if candidate and "<" not in candidate:
                _crumb = candidate
                return _crumb
        except Exception:
            continue
    return None


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """Batch-quote symbols. Missing symbols are simply absent from the result."""
    quotes: dict[str, dict] = {}
    crumb = _fetch_crumb()

    for start in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[start : start + BATCH_SIZE]
        params = {"symbols": ",".join(batch)}
        if crumb:
            params["crumb"] = crumb
        for base in (YF2, YF1):
            try:
                payload = json.loads(_get(f"{base}/v7/finance/quote?" + urllib.parse.urlencode(params)))
            except Exception:
                continue
            for row in (payload.get("quoteResponse") or {}).get("result") or []:
                symbol = row.get("symbol")
                if symbol:
                    quotes[symbol] = row
            if quotes:
                break

    missing = [s for s in symbols if s not in quotes]
    for symbol in missing:
        fallback = _chart_quote(symbol)
        if fallback:
            quotes[symbol] = fallback
    return quotes


def _chart_quote(symbol: str) -> dict | None:
    """Fallback for symbols the batch endpoint drops."""
    try:
        url = f"{YF1}/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1d&interval=1d"
        meta = json.loads(_get(url))["chart"]["result"][0]["meta"]
    except Exception:
        return None
    price = meta.get("regularMarketPrice")
    previous = meta.get("previousClose") or meta.get("chartPreviousClose")
    if price is None or not previous:
        return None
    return {
        "symbol": meta.get("symbol", symbol),
        "shortName": meta.get("shortName"),
        "regularMarketPrice": price,
        "regularMarketChangePercent": 100.0 * (price - previous) / previous,
        "regularMarketChange": price - previous,
    }


# ---------------------------------------------------------------------------
# Volatility: implied vs realized
# ---------------------------------------------------------------------------


def _load_options_client():
    """Import the sibling options-chain skill, if it is installed.

    The two skills ship together but can be installed separately, so a missing
    options-chain simply means the brief skips the volatility comparison.
    """
    path = Path(__file__).resolve().parent.parent.parent / "options-chain" / "scripts" / "options_client.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_hmp_options_client", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def realized_vol(symbol: str, windows=(20, 60)) -> dict | None:
    """Annualised realised volatility from daily closes."""
    try:
        url = f"{YF1}/v8/finance/chart/{urllib.parse.quote(symbol)}?range=6mo&interval=1d"
        result = json.loads(_get(url))["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c]
    except Exception:
        return None

    if len(closes) < max(windows) + 1:
        # Not enough history for the longest window; fall back to what we have.
        windows = tuple(w for w in windows if len(closes) >= w + 1)
        if not windows:
            return None

    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    out = {}
    for window in windows:
        sample = returns[-window:]
        if len(sample) < 2:
            continue
        out[f"hv{window}"] = round(statistics.stdev(sample) * math.sqrt(TRADING_DAYS), 4)
    return out or None


def atm_iv(symbol: str, oc, rate: float) -> dict | None:
    """At-the-money implied vol for the expiry nearest IV_TARGET_DTE days out."""
    try:
        result = oc.fetch_chain(symbol)
        spot, div = oc._spot_and_div(result)
        live = [ts for ts in result.get("expirationDates") or [] if oc._years_to_expiry(ts) > 0]
        if not live:
            return None
        target = min(live, key=lambda ts: abs(oc._years_to_expiry(ts) * 365.0 - IV_TARGET_DTE))
        chain = oc.fetch_chain(symbol, target)
        options = (chain.get("options") or [{}])[0]
    except SystemExit:
        return None  # fetch_chain exits on an unusable symbol
    except Exception:
        return None

    ivs = []
    for kind, key in (("call", "calls"), ("put", "puts")):
        try:
            rows = [oc.enrich(c, spot, rate, div, kind) for c in options.get(key) or []]
        except Exception:
            continue
        usable = [r for r in rows if r.get("iv")]
        if usable:
            ivs.append(min(usable, key=lambda r: abs(r["strike"] - spot))["iv"])
    if not ivs:
        return None
    return {
        "atm_iv": round(sum(ivs) / len(ivs), 4),
        "iv_dte": round(oc._years_to_expiry(target) * 365.0, 1),
    }


def _vol_verdict(ratio: float) -> str:
    """Where implied sits against realised. Wide bands - this is a signal, not a trigger."""
    if ratio >= 1.35:
        return "rich"
    if ratio <= 0.85:
        return "cheap"
    return "fair"


def volatility_rows(symbols: list[str], rate: float, limit: int) -> list[dict]:
    """Implied vs realised vol per symbol. Index and crypto symbols are skipped."""
    oc = _load_options_client()
    rows = []
    for symbol in symbols[:limit]:
        if symbol.startswith("^") or "-" in symbol:
            continue  # indices and crypto pairs have no comparable equity chain
        realised = realized_vol(symbol)
        implied = atm_iv(symbol, oc, rate) if oc else None
        if not realised and not implied:
            continue
        row = {"symbol": symbol, **(realised or {}), **(implied or {})}
        hv = (realised or {}).get("hv20")
        iv = (implied or {}).get("atm_iv")
        if hv and iv and hv > 0:
            row["iv_hv_ratio"] = round(iv / hv, 2)
            row["verdict"] = _vol_verdict(row["iv_hv_ratio"])
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def _session_fraction(quote: dict) -> float | None:
    """How much of the regular session has elapsed, 0.0-1.0.

    Intraday volume accumulates through the day, so comparing it against a
    full-day average is meaningless without this: at 10:00 a name trading at
    its normal pace has only ~8% of its daily volume.
    """
    try:
        market_time = int(quote["regularMarketTime"])
        offset_seconds = int(quote.get("gmtOffSetMilliseconds") or 0) // 1000
    except (KeyError, TypeError, ValueError):
        return None
    local = market_time + offset_seconds
    minutes = (local % 86400) // 60
    elapsed = minutes - SESSION_OPEN_MINUTE
    if elapsed <= 0:
        return None
    return min(elapsed / REGULAR_SESSION_MINUTES, 1.0)


def volume_row(quote: dict, threshold: float) -> dict | None:
    """Volume against its 10-day average, scaled for a partial session."""
    volume = quote.get("regularMarketVolume")
    average = quote.get("averageDailyVolume10Day")
    if not volume or not average:
        return None

    state = quote.get("marketState") or ""
    fraction = _session_fraction(quote) if state == "REGULAR" else None
    if state == "REGULAR":
        if fraction is None:
            return None  # session hasn't opened; nothing meaningful to compare
        expected = average * fraction
        basis = "pace"
    else:
        expected = average
        basis = "full session"
    if expected <= 0:
        return None

    pace = round(volume / expected, 2)
    return {
        "symbol": quote.get("symbol"),
        "volume": int(volume),
        "avg_10d": int(average),
        "pace": pace,
        "basis": basis,
        "session_elapsed_pct": round(100 * fraction, 1) if fraction else None,
        "unusual": pace >= threshold,
    }


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


def watchlist_path() -> Path:
    override = os.environ.get("WATCHLIST_FILE")
    return Path(override).expanduser() if override else hermes_home() / "watchlist.txt"


def load_watchlist(explicit: list[str] | None) -> list[str]:
    """Symbols from --symbol flags, else the watchlist file."""
    if explicit:
        return [s.strip().upper() for s in explicit if s.strip()]
    path = watchlist_path()
    if not path.exists():
        return []
    symbols = []
    for line in path.read_text().splitlines():
        entry = line.split("#", 1)[0].strip().upper()
        if entry:
            symbols.append(entry)
    return symbols


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------


def journal_db() -> Path:
    override = os.environ.get("TRADE_JOURNAL_DB")
    return Path(override).expanduser() if override else hermes_home() / "trade_journal.db"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open the journal read-only so a brief can never mutate it.

    A SQLite URI needs a real URL path: Windows backslashes and the drive
    colon are not valid there, so the path is converted rather than
    interpolated. If the URI form is rejected anyway, fall back to a plain
    connection - reading is still read-only by discipline.
    """
    uri = "file:" + urllib.request.pathname2url(str(path)) + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return sqlite3.connect(path)


def load_open_positions() -> list[dict]:
    """Read open trades from the trade-journal database, if one exists."""
    path = journal_db()
    if not path.exists():
        return []
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def evaluate_positions(positions: list[dict], quotes: dict[str, dict]) -> list[dict]:
    evaluated = []
    for position in positions:
        quote = quotes.get(position["symbol"])
        price = quote.get("regularMarketPrice") if quote else None
        multiplier = 100 if position.get("kind") == "option" else 1
        direction = 1.0 if position["side"] == "long" else -1.0

        row = {
            "id": position["id"],
            "symbol": position["symbol"],
            "side": position["side"],
            "qty": position["qty"],
            "entry": position["entry"],
            "stop": position["stop"],
            "price": price,
        }
        if price is not None:
            row["unrealized_pl"] = round(
                (price - position["entry"]) * direction * position["qty"] * multiplier, 2
            )
            row["move_pct"] = round(100.0 * (price - position["entry"]) / position["entry"], 2)
            if position["stop"]:
                risk = abs(position["entry"] - position["stop"])
                distance = (price - position["stop"]) * direction
                row["distance_to_stop_pct"] = round(100.0 * distance / position["entry"], 2)
                # How much of the original risk is left before the stop trips.
                row["r_remaining"] = round(distance / risk, 2) if risk else None
                if distance <= 0:
                    row["alert"] = "STOP BREACHED"
                elif risk and distance / risk < 0.25:
                    row["alert"] = "near stop"
        else:
            row["alert"] = "no quote"
        evaluated.append(row)
    return evaluated


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _row(quote: dict) -> dict:
    return {
        "symbol": quote.get("symbol"),
        "name": quote.get("shortName"),
        "price": quote.get("regularMarketPrice"),
        "change_pct": round(quote.get("regularMarketChangePercent") or 0.0, 2),
    }


def _fmt(row: dict) -> str:
    change = row["change_pct"]
    arrow = "+" if change >= 0 else ""
    price = row["price"]
    price_text = f"{price:,.2f}" if isinstance(price, (int, float)) else "n/a"
    return f"  {row['symbol']:<8} {price_text:>12}  {arrow}{change:.2f}%"


def render_text(report: dict) -> str:
    lines = [f"Market brief - {report['generated_at']}", ""]

    if report.get("indices"):
        lines.append("Indices")
        lines += [_fmt(r) for r in report["indices"]]
        lines.append("")

    if report.get("watchlist"):
        lines.append("Watchlist")
        lines += [_fmt(r) for r in report["watchlist"]]
        lines.append("")

    if report.get("movers"):
        lines.append(f"Movers (>{report['mover_threshold']}%)")
        lines += [_fmt(r) for r in report["movers"]]
        lines.append("")

    if report.get("positions"):
        lines.append("Open positions")
        for p in report["positions"]:
            price = f"{p['price']:,.2f}" if p.get("price") is not None else "n/a"
            pl = p.get("unrealized_pl")
            pl_text = f"{pl:+,.2f}" if pl is not None else "n/a"
            alert = f"  [{p['alert']}]" if p.get("alert") else ""
            lines.append(f"  #{p['id']} {p['symbol']:<6} {p['side']:<5} @ {price:>10}  P/L {pl_text}{alert}")
        lines.append("")

    if report.get("unusual_volume"):
        lines.append(f"Unusual volume (>{report['volume_threshold']}x normal)")
        for v in sorted(report["unusual_volume"], key=lambda r: r["pace"], reverse=True):
            note = "vs full session" if v["basis"] == "full session" else f"{v['session_elapsed_pct']:.0f}% into session"
            lines.append(f"  {v['symbol']:<8} {v['pace']:>5.2f}x normal   ({note})")
        lines.append("")

    if report.get("volatility"):
        lines.append("Implied vs realised vol")
        for v in report["volatility"]:
            iv = f"{v['atm_iv'] * 100:.1f}%" if v.get("atm_iv") else "n/a"
            hv = f"{v['hv20'] * 100:.1f}%" if v.get("hv20") else "n/a"
            ratio = f"{v['iv_hv_ratio']:.2f}x {v['verdict']}" if v.get("iv_hv_ratio") else ""
            lines.append(f"  {v['symbol']:<8} IV {iv:>7}  HV20 {hv:>7}   {ratio}")
        lines.append("")

    if report.get("errors"):
        lines.append("Notes")
        lines += [f"  - {e}" for e in report["errors"]]

    return "\n".join(lines).rstrip() + "\n"


def build_report(args) -> dict:
    errors: list[str] = []
    watch = load_watchlist(args.symbol)
    indices = [] if args.no_indices else (args.index or DEFAULT_INDICES)

    if not watch and not indices:
        errors.append(
            f"No watchlist found at {watchlist_path()} and no --symbol given."
        )

    positions = load_open_positions()
    needed = sorted({*indices, *watch, *(p["symbol"] for p in positions)})
    quotes = fetch_quotes(needed) if needed else {}

    for symbol in needed:
        if symbol not in quotes:
            errors.append(f"No quote for {symbol}")

    watch_rows = [_row(quotes[s]) for s in watch if s in quotes]

    volume_rows = []
    for symbol in watch:
        if symbol in quotes:
            row = volume_row(quotes[symbol], args.volume_threshold)
            if row:
                volume_rows.append(row)

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "indices": [_row(quotes[s]) for s in indices if s in quotes],
        "watchlist": watch_rows,
        "mover_threshold": args.mover_threshold,
        "movers": sorted(
            [r for r in watch_rows if abs(r["change_pct"]) >= args.mover_threshold],
            key=lambda r: abs(r["change_pct"]),
            reverse=True,
        ),
        "positions": evaluate_positions(positions, quotes),
        "volume": volume_rows,
        "unusual_volume": [r for r in volume_rows if r["unusual"]],
        "volume_threshold": args.volume_threshold,
        "errors": errors,
    }

    if args.iv and watch:
        report["volatility"] = volatility_rows(watch, args.rate, args.iv_limit)
        if len(watch) > args.iv_limit:
            errors.append(
                f"Volatility comparison covered the first {args.iv_limit} watchlist symbols; "
                f"raise --iv-limit to include the rest."
            )

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scheduled market brief.")
    parser.add_argument("--symbol", action="append", help="Override the watchlist file; repeatable")
    parser.add_argument("--index", action="append", help="Override default indices; repeatable")
    parser.add_argument("--no-indices", action="store_true")
    parser.add_argument("--mover-threshold", type=float, default=3.0, help="Percent move to flag")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument(
        "--volume-threshold",
        type=float,
        default=1.5,
        help="Flag volume at or above this multiple of normal pace (default 1.5)",
    )
    parser.add_argument(
        "--no-iv",
        dest="iv",
        action="store_false",
        help="Skip the implied-vs-realised vol section (it costs two requests per symbol)",
    )
    parser.add_argument(
        "--iv-limit",
        type=int,
        default=12,
        help="How many watchlist symbols to price for the vol comparison (default 12)",
    )
    parser.add_argument("--rate", type=float, default=float(os.environ.get("RISK_FREE_RATE", "0.04")))
    parser.set_defaults(iv=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(args)
    print(json.dumps(report, indent=2) if args.json else render_text(report), end="" if not args.json else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
