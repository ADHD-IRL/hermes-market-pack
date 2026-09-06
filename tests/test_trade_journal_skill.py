"""Tests for the trade-journal skill. Uses a temp database, no network."""

import pathlib

import pytest

import journal


def args(**kwargs):
    kwargs.setdefault("notes", None)
    return type("Args", (), kwargs)()


@pytest.fixture()
def conn(tmp_path):
    connection = journal.connect(tmp_path / "journal.db")
    yield connection
    connection.close()


def open_trade(conn, **overrides):
    payload = dict(
        symbol="AAPL", side="long", qty=100, entry=50.0, stop=45.0, target=None,
        kind="stock", strategy=None, notes=None, opened_at=None,
    )
    payload.update(overrides)
    return journal.cmd_open(args(**payload), conn)


class TestOpen:
    def test_records_a_trade_and_returns_an_id(self, conn):
        trade = open_trade(conn)
        assert trade["id"] == 1
        assert trade["status"] == "open"
        assert trade["symbol"] == "AAPL"

    def test_uppercases_the_symbol(self, conn):
        assert open_trade(conn, symbol="aapl")["symbol"] == "AAPL"

    def test_reports_dollar_risk_at_entry(self, conn):
        assert open_trade(conn)["risk_at_entry"] == 500.0        # $5 x 100 shares

    def test_option_risk_applies_the_multiplier(self, conn):
        trade = open_trade(conn, kind="option", qty=2, entry=5.0, stop=2.5)
        assert trade["risk_at_entry"] == 500.0                    # $2.50 x 2 x 100

    def test_warns_when_no_stop_is_recorded(self, conn):
        trade = open_trade(conn, stop=None)
        assert "warning" in trade

    def test_rejects_a_long_stop_above_entry(self, conn):
        with pytest.raises(SystemExit, match="below the entry"):
            open_trade(conn, side="long", entry=50, stop=55)

    def test_rejects_a_short_stop_below_entry(self, conn):
        with pytest.raises(SystemExit, match="above the entry"):
            open_trade(conn, side="short", entry=50, stop=45)

    @pytest.mark.parametrize("field,value", [("qty", 0), ("qty", -5), ("entry", 0), ("side", "sideways")])
    def test_rejects_invalid_input(self, conn, field, value):
        with pytest.raises(SystemExit):
            open_trade(conn, **{field: value})


class TestClose:
    def test_computes_pl_and_r_for_a_long_winner(self, conn):
        trade = open_trade(conn)                                  # entry 50, stop 45
        closed = journal.cmd_close(args(id=trade["id"], exit=60.0, closed_at=None), conn)
        assert closed["pl"] == 1000.0                             # $10 x 100
        assert closed["r_multiple"] == 2.0                        # $10 gained / $5 risked
        assert closed["status"] == "closed"

    def test_a_short_profits_when_price_falls(self, conn):
        trade = open_trade(conn, side="short", entry=200, stop=210, qty=100)
        closed = journal.cmd_close(args(id=trade["id"], exit=190.0, closed_at=None), conn)
        assert closed["pl"] == 1000.0
        assert closed["r_multiple"] == 1.0

    def test_a_full_stop_out_is_minus_one_r(self, conn):
        trade = open_trade(conn)
        closed = journal.cmd_close(args(id=trade["id"], exit=45.0, closed_at=None), conn)
        assert closed["r_multiple"] == -1.0

    def test_option_pl_applies_the_multiplier(self, conn):
        trade = open_trade(conn, kind="option", qty=2, entry=5.0, stop=2.5)
        closed = journal.cmd_close(args(id=trade["id"], exit=7.5, closed_at=None), conn)
        assert closed["pl"] == 500.0

    def test_r_is_none_without_a_stop(self, conn):
        trade = open_trade(conn, stop=None)
        closed = journal.cmd_close(args(id=trade["id"], exit=60.0, closed_at=None), conn)
        assert closed["r_multiple"] is None
        assert closed["pl"] == 1000.0

    def test_closing_twice_is_refused(self, conn):
        trade = open_trade(conn)
        journal.cmd_close(args(id=trade["id"], exit=60.0, closed_at=None), conn)
        with pytest.raises(SystemExit, match="already closed"):
            journal.cmd_close(args(id=trade["id"], exit=70.0, closed_at=None), conn)

    def test_closing_an_unknown_trade_is_refused(self, conn):
        with pytest.raises(SystemExit, match="No trade"):
            journal.cmd_close(args(id=999, exit=60.0, closed_at=None), conn)

    def test_closing_notes_append_rather_than_replace(self, conn):
        trade = open_trade(conn, notes="entered on breakout")
        closed = journal.cmd_close(args(id=trade["id"], exit=60.0, closed_at=None, notes="hit target"), conn)
        assert "entered on breakout" in closed["notes"] and "hit target" in closed["notes"]


class TestList:
    def test_filters_by_status(self, conn):
        first = open_trade(conn)
        open_trade(conn, symbol="NVDA")
        journal.cmd_close(args(id=first["id"], exit=60.0, closed_at=None), conn)

        assert journal.cmd_list(args(status="open", symbol=None, strategy=None, limit=50), conn)["count"] == 1
        assert journal.cmd_list(args(status="closed", symbol=None, strategy=None, limit=50), conn)["count"] == 1
        assert journal.cmd_list(args(status="all", symbol=None, strategy=None, limit=50), conn)["count"] == 2

    def test_filters_by_symbol(self, conn):
        open_trade(conn, symbol="AAPL")
        open_trade(conn, symbol="NVDA")
        result = journal.cmd_list(args(status="all", symbol="nvda", strategy=None, limit=50), conn)
        assert result["count"] == 1 and result["trades"][0]["symbol"] == "NVDA"

    def test_respects_the_limit(self, conn):
        for _ in range(5):
            open_trade(conn)
        assert journal.cmd_list(args(status="all", symbol=None, strategy=None, limit=2), conn)["count"] == 2


class TestStats:
    def _closed_book(self, conn):
        # +$3,000 win, -$1,000 loss, +$500 option win
        a = open_trade(conn, symbol="AAPL", qty=400, entry=50, stop=47.5, strategy="breakout")
        b = open_trade(conn, symbol="NVDA", side="short", qty=100, entry=200, stop=210, strategy="fade")
        c = open_trade(conn, symbol="SPY", kind="option", qty=2, entry=5, stop=2.5, strategy="breakout")
        journal.cmd_close(args(id=a["id"], exit=57.5, closed_at=None), conn)
        journal.cmd_close(args(id=b["id"], exit=210.0, closed_at=None), conn)
        journal.cmd_close(args(id=c["id"], exit=7.5, closed_at=None), conn)

    def test_summarises_the_book(self, conn):
        self._closed_book(conn)
        stats = journal.cmd_stats(args(symbol=None, strategy=None, since=None), conn)
        assert stats["trades"] == 3
        assert stats["wins"] == 2 and stats["losses"] == 1
        assert stats["total_pl"] == 2500.0
        assert stats["win_rate"] == pytest.approx(0.6667, abs=1e-4)
        assert stats["profit_factor"] == 3.5              # 3500 gross win / 1000 gross loss
        assert stats["win_loss_ratio"] == 1.75            # avg win 1750 / avg loss 1000
        assert stats["expectancy_r"] == 1.0               # (3 - 1 + 1) / 3

    def test_filters_by_strategy(self, conn):
        self._closed_book(conn)
        stats = journal.cmd_stats(args(symbol=None, strategy="fade", since=None), conn)
        assert stats["trades"] == 1 and stats["total_pl"] == -1000.0

    def test_open_trades_are_excluded(self, conn):
        open_trade(conn)
        stats = journal.cmd_stats(args(symbol=None, strategy=None, since=None), conn)
        assert stats["trades"] == 0

    def test_notes_how_many_trades_lack_an_r_score(self, conn):
        trade = open_trade(conn, stop=None)
        journal.cmd_close(args(id=trade["id"], exit=60.0, closed_at=None), conn)
        stats = journal.cmd_stats(args(symbol=None, strategy=None, since=None), conn)
        assert "no stop recorded" in stats["note"]

    def test_empty_book_reports_zero_not_an_error(self, conn):
        assert journal.cmd_stats(args(symbol=None, strategy=None, since=None), conn)["trades"] == 0


class TestNotesAndDelete:
    def test_note_appends_to_existing_notes(self, conn):
        trade = open_trade(conn, notes="first")
        updated = journal.cmd_note(args(id=trade["id"], text="second"), conn)
        assert updated["notes"] == "first\nsecond"

    def test_note_on_an_unknown_trade_is_refused(self, conn):
        with pytest.raises(SystemExit, match="No trade"):
            journal.cmd_note(args(id=999, text="x"), conn)

    def test_delete_removes_the_row(self, conn):
        trade = open_trade(conn)
        journal.cmd_delete(args(id=trade["id"]), conn)
        assert journal.cmd_list(args(status="all", symbol=None, strategy=None, limit=50), conn)["count"] == 0


class TestDatabaseLocation:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRADE_JOURNAL_DB", str(tmp_path / "custom.db"))
        assert journal.db_path() == tmp_path / "custom.db"

    def test_falls_back_to_hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TRADE_JOURNAL_DB", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert journal.db_path() == tmp_path / "trade_journal.db"

    def test_windows_default_is_local_appdata_not_dot_hermes(self, monkeypatch):
        """Hermes stores its data under %LOCALAPPDATA%\\hermes on Windows."""
        monkeypatch.delenv("TRADE_JOURNAL_DB", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(journal.sys, "platform", "win32")
        local_appdata = r"C:\Users\Mako\AppData\Local"
        monkeypatch.setenv("LOCALAPPDATA", local_appdata)
        assert journal.hermes_home() == pathlib.Path(local_appdata) / "hermes"

    def test_windows_falls_back_when_localappdata_is_unset(self, monkeypatch):
        monkeypatch.delenv("TRADE_JOURNAL_DB", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(journal.sys, "platform", "win32")
        assert journal.hermes_home() == pathlib.Path.home() / "AppData" / "Local" / "hermes"

    def test_posix_default_is_dot_hermes(self, monkeypatch):
        monkeypatch.delenv("TRADE_JOURNAL_DB", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(journal.sys, "platform", "linux")
        assert journal.hermes_home().name == ".hermes"

    def test_blank_env_override_is_ignored(self, monkeypatch):
        monkeypatch.delenv("TRADE_JOURNAL_DB", raising=False)
        monkeypatch.setenv("HERMES_HOME", "   ")
        monkeypatch.setattr(journal.sys, "platform", "linux")
        assert journal.hermes_home().name == ".hermes"
