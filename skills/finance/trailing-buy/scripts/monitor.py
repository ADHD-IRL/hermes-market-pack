#!/usr/bin/env python3
"""monitor.py - Poll every live trailing-buy plan and print a change-stable digest.

Written for Hermes cron's `--monitor-script` mode: this runs before the agent on
every tick, and Hermes hashes its output. Identical output suppresses the agent run
entirely; changed output wakes the agent with a diff. That is exactly the split the
skill is built around - the rules poll all session for free, and the model is woken
only when something actually happened.

So the output must carry the signal and nothing that moves on its own. No prices, no
timestamps, no ATR: those change on almost every poll and would wake the agent every
five minutes, which is the cost this mode exists to avoid.

A failed poll is deliberately invisible here. Network blips are common and flapping
output would wake the agent on each one; the tick itself records the failure in the
ledger either way, and the unconditional daily recap is what notices a watcher that
has quietly stopped working.

Stdlib only. Copy it to $HERMES_HOME/scripts/ - Hermes runs monitor scripts from
there.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


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


def skill_path() -> Path:
    """Where trailing_buy.py lives. Overridable, because this file is copied away
    from the skill directory and cannot find its sibling by relative path."""
    override = os.environ.get("TRAILING_BUY_SCRIPT", "").strip()
    if override:
        return Path(override).expanduser()
    return hermes_home() / "skills" / "finance" / "trailing-buy" / "scripts" / "trailing_buy.py"


def load_skill():
    """Import the skill by path, the same way market-brief loads options-chain."""
    path = skill_path()
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_hmp_trailing_buy", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def digest(tb, conn) -> list[str]:
    """One stable line per live plan, plus the kill switch when it is set."""
    lines = []
    if tb.halted():
        lines.append("KILL SWITCH SET - every trailing-buy command is refusing")

    plans = conn.execute(
        "SELECT * FROM plans WHERE state NOT IN (?,?,?) ORDER BY id", tb.TERMINAL_STATES
    ).fetchall()
    if not plans:
        lines.append("no live plans")
        return lines

    for plan in plans:
        if not tb.halted():
            try:
                tb.cmd_tick(
                    _Args(plan=plan["id"], price=None, allow_extended=False, no_context=True),
                    conn,
                )
            except SystemExit:
                pass  # logged in the ledger; see the module docstring
            except Exception:
                pass

        current = conn.execute("SELECT * FROM plans WHERE id=?", (plan["id"],)).fetchone()
        entries = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE plan_id=? AND stage!='rejected'",
            (plan["id"],),
        ).fetchone()["n"]
        mode = "paper" if current["paper"] else ("autonomous" if current["autonomous"] else "live")
        lines.append(
            f"plan {current['id']} {current['symbol']} {current['state']} "
            f"entries={entries} mode={mode}"
        )
    return lines


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def main() -> int:
    tb = load_skill()
    if tb is None:
        # Stable text, so a missing install wakes the agent once and then stops.
        print(f"trailing-buy skill not found at {skill_path()}")
        return 0

    conn = tb.connect()
    try:
        for line in digest(tb, conn):
            print(line)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
