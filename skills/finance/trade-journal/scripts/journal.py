#!/usr/bin/env python3
"""journal.py - A durable trade log with R-multiple statistics.

Records entries and exits in SQLite so performance is measured from what
actually happened rather than from memory. Every closed trade is scored in
R-multiples (profit divided by the risk originally taken), which makes trades
with different stop distances comparable.

Stdlib only. The database lives under HERMES_HOME (default ~/.hermes) and is
never transmitted anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

OPTION_MULTIPLIER = 100
SCHEMA_VERSION = 1


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


def db_path() -> Path:
    """Resolve the journal database, honouring test and user overrides."""
    override = os.environ.get("TRADE_JOURNAL_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return hermes_home() / "trade_journal.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol       TEXT    NOT NULL,
            kind         TEXT    NOT NULL DEFAULT 'stock',
            side         TEXT    NOT NULL,
            strategy     TEXT,
            qty          REAL    NOT NULL,
            entry        REAL    NOT NULL,
            stop         REAL,
            target       REAL,
            exit         REAL,
            opened_at    TEXT    NOT NULL,
            closed_at    TEXT,
            status       TEXT    NOT NULL DEFAULT 'open',
            notes        TEXT,
            pl           REAL,
            r_multiple   REAL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _multiplier(kind: str) -> int:
    return OPTION_MULTIPLIER if kind == "option" else 1


def compute_pl(row, exit_price: float) -> float:
    """Realised P/L. Shorts profit when price falls."""
    direction = 1.0 if row["side"] == "long" else -1.0
    return (exit_price - row["entry"]) * direction * row["qty"] * _multiplier(row["kind"])


def compute_r(row, exit_price: float) -> float | None:
    """Realised R-multiple, or None when the trade was logged without a stop."""
    if row["stop"] is None:
        return None
    risk = abs(row["entry"] - row["stop"])
    if risk == 0:
        return None
    direction = 1.0 if row["side"] == "long" else -1.0
    return (exit_price - row["entry"]) * direction / risk


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_open(args, conn) -> dict:
    if args.side not in ("long", "short"):
        raise SystemExit("Side must be 'long' or 'short'.")
    if args.qty <= 0:
        raise SystemExit("Quantity must be positive.")
    if args.entry <= 0:
        raise SystemExit("Entry price must be positive.")
    if args.stop is not None:
        if args.side == "long" and args.stop >= args.entry:
            raise SystemExit("A long stop must sit below the entry price.")
        if args.side == "short" and args.stop <= args.entry:
            raise SystemExit("A short stop must sit above the entry price.")

    cursor = conn.execute(
        """INSERT INTO trades (symbol, kind, side, strategy, qty, entry, stop, target,
                               opened_at, status, notes)
           VALUES (?,?,?,?,?,?,?,?,?,'open',?)""",
        (
            args.symbol.upper(),
            args.kind,
            args.side,
            args.strategy,
            args.qty,
            args.entry,
            args.stop,
            args.target,
            args.opened_at or _now(),
            args.notes,
        ),
    )
    conn.commit()
    trade_id = cursor.lastrowid
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    result = dict(row)
    if args.stop is not None:
        result["risk_at_entry"] = round(
            abs(args.entry - args.stop) * args.qty * _multiplier(args.kind), 2
        )
    else:
        result["warning"] = "No stop recorded - this trade cannot be scored in R."
    return result


def cmd_close(args, conn) -> dict:
    row = conn.execute("SELECT * FROM trades WHERE id=?", (args.id,)).fetchone()
    if row is None:
        raise SystemExit(f"No trade with id {args.id}.")
    if row["status"] == "closed":
        raise SystemExit(f"Trade {args.id} is already closed (exit {row['exit']}).")

    pl = compute_pl(row, args.exit)
    r = compute_r(row, args.exit)
    notes = row["notes"]
    if args.notes:
        notes = f"{notes}\n{args.notes}" if notes else args.notes

    conn.execute(
        """UPDATE trades SET exit=?, closed_at=?, status='closed', pl=?, r_multiple=?, notes=?
           WHERE id=?""",
        (args.exit, args.closed_at or _now(), pl, r, notes, args.id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM trades WHERE id=?", (args.id,)).fetchone())


def cmd_list(args, conn) -> dict:
    clauses, params = [], []
    if args.status != "all":
        clauses.append("status = ?")
        params.append(args.status)
    if args.symbol:
        clauses.append("symbol = ?")
        params.append(args.symbol.upper())
    if args.strategy:
        clauses.append("strategy = ?")
        params.append(args.strategy)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM trades {where} ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?",
        (*params, args.limit),
    ).fetchall()
    return {"count": len(rows), "trades": [dict(r) for r in rows]}


def cmd_stats(args, conn) -> dict:
    clauses, params = ["status = 'closed'"], []
    if args.symbol:
        clauses.append("symbol = ?")
        params.append(args.symbol.upper())
    if args.strategy:
        clauses.append("strategy = ?")
        params.append(args.strategy)
    if args.since:
        clauses.append("closed_at >= ?")
        params.append(args.since)

    rows = conn.execute(
        f"SELECT * FROM trades WHERE {' AND '.join(clauses)}", tuple(params)
    ).fetchall()
    if not rows:
        return {"trades": 0, "note": "No closed trades match this filter."}

    pls = [r["pl"] for r in rows if r["pl"] is not None]
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p < 0]
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    stats = {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(pls) - len(wins) - len(losses),
        "win_rate": round(len(wins) / len(pls), 4) if pls else None,
        "total_pl": round(sum(pls), 2),
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "largest_win": round(max(pls), 2) if pls else None,
        "largest_loss": round(min(pls), 2) if pls else None,
        # Profit factor: gross wins over gross losses. Above 1 is profitable.
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }
    if wins and losses:
        stats["win_loss_ratio"] = round((gross_win / len(wins)) / (gross_loss / len(losses)), 3)
    if rs:
        stats.update(
            {
                "trades_scored_in_r": len(rs),
                "avg_r": round(sum(rs) / len(rs), 3),
                "total_r": round(sum(rs), 3),
                "best_r": round(max(rs), 3),
                "worst_r": round(min(rs), 3),
            }
        )
        stats["expectancy_r"] = stats["avg_r"]
    if len(rs) < len(rows):
        stats["note"] = (
            f"{len(rows) - len(rs)} closed trade(s) had no stop recorded and are "
            "excluded from the R statistics."
        )
    return stats


def cmd_note(args, conn) -> dict:
    row = conn.execute("SELECT * FROM trades WHERE id=?", (args.id,)).fetchone()
    if row is None:
        raise SystemExit(f"No trade with id {args.id}.")
    combined = f"{row['notes']}\n{args.text}" if row["notes"] else args.text
    conn.execute("UPDATE trades SET notes=? WHERE id=?", (combined, args.id))
    conn.commit()
    return dict(conn.execute("SELECT * FROM trades WHERE id=?", (args.id,)).fetchone())


def cmd_delete(args, conn) -> dict:
    row = conn.execute("SELECT * FROM trades WHERE id=?", (args.id,)).fetchone()
    if row is None:
        raise SystemExit(f"No trade with id {args.id}.")
    conn.execute("DELETE FROM trades WHERE id=?", (args.id,))
    conn.commit()
    return {"deleted": args.id, "trade": dict(row)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trade journal with R-multiple statistics.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="Log a new position")
    p_open.add_argument("--symbol", required=True)
    p_open.add_argument("--side", required=True, choices=["long", "short"])
    p_open.add_argument("--qty", type=float, required=True)
    p_open.add_argument("--entry", type=float, required=True)
    p_open.add_argument("--stop", type=float, help="Strongly recommended - required for R scoring")
    p_open.add_argument("--target", type=float)
    p_open.add_argument("--kind", choices=["stock", "option"], default="stock")
    p_open.add_argument("--strategy", help="Free-form tag, e.g. 'bull-put-spread'")
    p_open.add_argument("--notes")
    p_open.add_argument("--opened-at", help="ISO timestamp; defaults to now")
    p_open.set_defaults(func=cmd_open)

    p_close = sub.add_parser("close", help="Close a position and score it")
    p_close.add_argument("--id", type=int, required=True)
    p_close.add_argument("--exit", type=float, required=True)
    p_close.add_argument("--notes")
    p_close.add_argument("--closed-at", help="ISO timestamp; defaults to now")
    p_close.set_defaults(func=cmd_close)

    p_list = sub.add_parser("list", help="List trades")
    p_list.add_argument("--status", choices=["open", "closed", "all"], default="open")
    p_list.add_argument("--symbol")
    p_list.add_argument("--strategy")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    p_stats = sub.add_parser("stats", help="Performance statistics over closed trades")
    p_stats.add_argument("--symbol")
    p_stats.add_argument("--strategy")
    p_stats.add_argument("--since", help="ISO date, e.g. 2026-01-01")
    p_stats.set_defaults(func=cmd_stats)

    p_note = sub.add_parser("note", help="Append a note to a trade")
    p_note.add_argument("--id", type=int, required=True)
    p_note.add_argument("--text", required=True)
    p_note.set_defaults(func=cmd_note)

    p_delete = sub.add_parser("delete", help="Delete a trade permanently")
    p_delete.add_argument("--id", type=int, required=True)
    p_delete.set_defaults(func=cmd_delete)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect()
    try:
        print(json.dumps(args.func(args, conn), indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
