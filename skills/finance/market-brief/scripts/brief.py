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
import json
import os
import sqlite3
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
        "errors": errors,
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scheduled market brief.")
    parser.add_argument("--symbol", action="append", help="Override the watchlist file; repeatable")
    parser.add_argument("--index", action="append", help="Override default indices; repeatable")
    parser.add_argument("--no-indices", action="store_true")
    parser.add_argument("--mover-threshold", type=float, default=3.0, help="Percent move to flag")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(args)
    print(json.dumps(report, indent=2) if args.json else render_text(report), end="" if not args.json else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
