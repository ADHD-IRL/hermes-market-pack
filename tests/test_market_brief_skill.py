"""Tests for the market-brief skill. All quotes are mocked - no network."""

from unittest import mock

import pytest

import brief


def args(**kwargs):
    defaults = dict(symbol=None, index=None, no_indices=False, mover_threshold=3.0, json=False)
    defaults.update(kwargs)
    return type("Args", (), defaults)()


def quote(symbol, price, change_pct, name=None):
    return {
        "symbol": symbol,
        "shortName": name or symbol,
        "regularMarketPrice": price,
        "regularMarketChangePercent": change_pct,
    }


class TestWatchlist:
    def test_reads_symbols_from_the_file(self, tmp_path, monkeypatch):
        path = tmp_path / "watchlist.txt"
        path.write_text("AAPL\nnvda\nSPY\n")
        monkeypatch.setenv("WATCHLIST_FILE", str(path))
        assert brief.load_watchlist(None) == ["AAPL", "NVDA", "SPY"]

    def test_strips_comments_and_blank_lines(self, tmp_path, monkeypatch):
        path = tmp_path / "watchlist.txt"
        path.write_text("# my list\nAAPL\n\nMSFT  # megacap\n   \n")
        monkeypatch.setenv("WATCHLIST_FILE", str(path))
        assert brief.load_watchlist(None) == ["AAPL", "MSFT"]

    def test_explicit_symbols_override_the_file(self, tmp_path, monkeypatch):
        path = tmp_path / "watchlist.txt"
        path.write_text("AAPL\n")
        monkeypatch.setenv("WATCHLIST_FILE", str(path))
        assert brief.load_watchlist(["tsla"]) == ["TSLA"]

    def test_missing_file_yields_an_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WATCHLIST_FILE", str(tmp_path / "absent.txt"))
        assert brief.load_watchlist(None) == []


class TestPositionEvaluation:
    def _position(self, **overrides):
        base = {"id": 1, "symbol": "AAPL", "side": "long", "qty": 100,
                "entry": 100.0, "stop": 95.0, "kind": "stock"}
        base.update(overrides)
        return base

    def test_marks_a_long_to_market(self):
        rows = brief.evaluate_positions([self._position()], {"AAPL": quote("AAPL", 110.0, 1.0)})
        assert rows[0]["unrealized_pl"] == 1000.0
        assert rows[0]["move_pct"] == 10.0
        assert rows[0]["r_remaining"] == 3.0        # $15 above stop / $5 risked
        assert "alert" not in rows[0]

    def test_marks_a_short_to_market(self):
        position = self._position(side="short", entry=100.0, stop=105.0)
        rows = brief.evaluate_positions([position], {"AAPL": quote("AAPL", 90.0, -1.0)})
        assert rows[0]["unrealized_pl"] == 1000.0

    def test_applies_the_option_multiplier(self):
        position = self._position(kind="option", qty=2, entry=5.0, stop=2.5)
        rows = brief.evaluate_positions([position], {"AAPL": quote("AAPL", 7.5, 1.0)})
        assert rows[0]["unrealized_pl"] == 500.0

    def test_flags_a_breached_stop(self):
        rows = brief.evaluate_positions([self._position()], {"AAPL": quote("AAPL", 94.0, -6.0)})
        assert rows[0]["alert"] == "STOP BREACHED"
        assert rows[0]["r_remaining"] < 0

    def test_flags_a_position_close_to_its_stop(self):
        rows = brief.evaluate_positions([self._position()], {"AAPL": quote("AAPL", 96.0, -4.0)})
        assert rows[0]["alert"] == "near stop"      # $1 left of $5 risk

    def test_exactly_at_the_stop_counts_as_breached(self):
        rows = brief.evaluate_positions([self._position()], {"AAPL": quote("AAPL", 95.0, -5.0)})
        assert rows[0]["alert"] == "STOP BREACHED"

    def test_flags_a_missing_quote(self):
        rows = brief.evaluate_positions([self._position()], {})
        assert rows[0]["alert"] == "no quote"
        assert rows[0]["price"] is None

    def test_position_without_a_stop_has_no_alert(self):
        rows = brief.evaluate_positions([self._position(stop=None)], {"AAPL": quote("AAPL", 110.0, 1.0)})
        assert "alert" not in rows[0]
        assert "r_remaining" not in rows[0]


class TestReport:
    def test_flags_movers_past_the_threshold(self):
        quotes = {"AAPL": quote("AAPL", 100, -5.0), "MSFT": quote("MSFT", 200, 1.0)}
        with mock.patch.object(brief, "fetch_quotes", return_value=quotes), \
             mock.patch.object(brief, "load_open_positions", return_value=[]):
            report = brief.build_report(args(symbol=["AAPL", "MSFT"], no_indices=True))
        assert [r["symbol"] for r in report["movers"]] == ["AAPL"]

    def test_movers_are_ordered_by_absolute_move(self):
        quotes = {"A": quote("A", 10, 4.0), "B": quote("B", 10, -9.0), "C": quote("C", 10, 6.0)}
        with mock.patch.object(brief, "fetch_quotes", return_value=quotes), \
             mock.patch.object(brief, "load_open_positions", return_value=[]):
            report = brief.build_report(args(symbol=["A", "B", "C"], no_indices=True))
        assert [r["symbol"] for r in report["movers"]] == ["B", "C", "A"]

    def test_records_an_error_for_a_symbol_with_no_quote(self):
        with mock.patch.object(brief, "fetch_quotes", return_value={}), \
             mock.patch.object(brief, "load_open_positions", return_value=[]):
            report = brief.build_report(args(symbol=["BOGUS"], no_indices=True))
        assert any("BOGUS" in e for e in report["errors"])

    def test_position_symbols_are_quoted_even_when_off_the_watchlist(self):
        positions = [{"id": 1, "symbol": "TSLA", "side": "long", "qty": 1,
                      "entry": 100.0, "stop": 95.0, "kind": "stock"}]
        captured = {}

        def fake_fetch(symbols):
            captured["symbols"] = symbols
            return {s: quote(s, 100.0, 0.0) for s in symbols}

        with mock.patch.object(brief, "fetch_quotes", side_effect=fake_fetch), \
             mock.patch.object(brief, "load_open_positions", return_value=positions):
            brief.build_report(args(symbol=["AAPL"], no_indices=True))
        assert "TSLA" in captured["symbols"]

    def test_indices_are_included_by_default(self):
        with mock.patch.object(brief, "fetch_quotes",
                               side_effect=lambda s: {x: quote(x, 100.0, 0.0) for x in s}), \
             mock.patch.object(brief, "load_open_positions", return_value=[]):
            report = brief.build_report(args(symbol=["AAPL"]))
        assert [r["symbol"] for r in report["indices"]] == brief.DEFAULT_INDICES


class TestRendering:
    def test_renders_sections_as_text(self):
        report = {
            "generated_at": "2026-09-04 13:00 UTC",
            "indices": [{"symbol": "^GSPC", "name": "S&P", "price": 7718.6, "change_pct": -0.38}],
            "watchlist": [{"symbol": "AAPL", "name": "Apple", "price": 319.97, "change_pct": -2.51}],
            "mover_threshold": 3.0,
            "movers": [],
            "positions": [{"id": 1, "symbol": "AAPL", "side": "long", "price": 319.97,
                           "unrealized_pl": 1997.0, "alert": "near stop"}],
            "errors": [],
        }
        text = brief.render_text(report)
        assert "Indices" in text and "^GSPC" in text
        assert "AAPL" in text and "-2.51%" in text
        assert "[near stop]" in text
        assert "+1,997.00" in text

    def test_handles_a_position_without_a_quote(self):
        report = {
            "generated_at": "now", "indices": [], "watchlist": [], "mover_threshold": 3.0,
            "movers": [], "errors": [],
            "positions": [{"id": 1, "symbol": "AAPL", "side": "long", "price": None, "alert": "no quote"}],
        }
        assert "n/a" in brief.render_text(report)

    def test_errors_are_surfaced(self):
        report = {"generated_at": "now", "indices": [], "watchlist": [], "mover_threshold": 3.0,
                  "movers": [], "positions": [], "errors": ["No quote for BOGUS"]}
        assert "BOGUS" in brief.render_text(report)


class TestQuoteFallback:
    def test_chart_endpoint_derives_change_from_previous_close(self):
        payload = '{"chart": {"result": [{"meta": {"symbol": "AAPL", "regularMarketPrice": 110.0, "previousClose": 100.0}}]}}'
        with mock.patch.object(brief, "_get", return_value=payload):
            row = brief._chart_quote("AAPL")
        assert row["regularMarketPrice"] == 110.0
        assert row["regularMarketChangePercent"] == pytest.approx(10.0)

    def test_returns_none_when_the_payload_is_unusable(self):
        with mock.patch.object(brief, "_get", return_value='{"chart": {"result": []}}'):
            assert brief._chart_quote("AAPL") is None


class TestJournalIntegration:
    def test_absent_database_yields_no_positions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRADE_JOURNAL_DB", str(tmp_path / "absent.db"))
        assert brief.load_open_positions() == []

    def test_unreadable_database_is_tolerated(self, tmp_path, monkeypatch):
        path = tmp_path / "corrupt.db"
        path.write_text("this is not sqlite")
        monkeypatch.setenv("TRADE_JOURNAL_DB", str(path))
        assert brief.load_open_positions() == []
