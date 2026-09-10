"""Tests for the market-brief skill. All quotes are mocked - no network."""

import json
import pathlib
from unittest import mock

import pytest

import brief


def args(**kwargs):
    # iv defaults off here: the volatility section makes live requests, and no
    # test may touch the network.
    defaults = dict(
        symbol=None, index=None, no_indices=False, mover_threshold=3.0, json=False,
        volume_threshold=1.5, iv=False, iv_limit=12, rate=0.04,
    )
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

    def test_reads_open_positions_through_a_readonly_connection(self, tmp_path, monkeypatch):
        import sqlite3
        path = tmp_path / "journal.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE trades (id INTEGER, symbol TEXT, side TEXT, qty REAL,"
            " entry REAL, stop REAL, kind TEXT, status TEXT);"
            "INSERT INTO trades VALUES (1,'AAPL','long',100,50.0,45.0,'stock','open');"
            "INSERT INTO trades VALUES (2,'NVDA','long',10,20.0,18.0,'stock','closed');"
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("TRADE_JOURNAL_DB", str(path))
        rows = brief.load_open_positions()
        assert [r["symbol"] for r in rows] == ["AAPL"]

    def test_readonly_connection_refuses_writes(self, tmp_path):
        import sqlite3
        path = tmp_path / "journal.db"
        sqlite3.connect(path).executescript("CREATE TABLE trades (id INTEGER);")
        conn = brief._connect_readonly(path)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO trades VALUES (1)")
        conn.close()

    def test_path_with_spaces_is_readable(self, tmp_path, monkeypatch):
        """Windows user directories routinely contain spaces."""
        import sqlite3
        spaced = tmp_path / "First Last"
        spaced.mkdir()
        path = spaced / "journal.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE trades (id INTEGER, symbol TEXT, side TEXT, qty REAL,"
            " entry REAL, stop REAL, kind TEXT, status TEXT);"
            "INSERT INTO trades VALUES (1,'AAPL','long',1,1.0,0.5,'stock','open');"
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("TRADE_JOURNAL_DB", str(path))
        assert len(brief.load_open_positions()) == 1


class TestHermesHome:
    def test_windows_default_is_local_appdata(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(brief.sys, "platform", "win32")
        local_appdata = r"C:\Users\Mako\AppData\Local"
        monkeypatch.setenv("LOCALAPPDATA", local_appdata)
        assert brief.hermes_home() == pathlib.Path(local_appdata) / "hermes"

    def test_posix_default_is_dot_hermes(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(brief.sys, "platform", "linux")
        assert brief.hermes_home().name == ".hermes"

    def test_watchlist_follows_hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WATCHLIST_FILE", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert brief.watchlist_path() == tmp_path / "watchlist.txt"


class TestSessionFraction:
    """Intraday volume must be compared against a partial-session expectation."""

    def _quote(self, hour, minute, offset_hours=-4):
        # Build an epoch whose exchange-local time is hour:minute.
        local_midnight = 1_800_000_000 - (1_800_000_000 % 86400)
        local = local_midnight + hour * 3600 + minute * 60
        return {"regularMarketTime": local - offset_hours * 3600,
                "gmtOffSetMilliseconds": offset_hours * 3600 * 1000}

    def test_none_before_the_open(self):
        assert brief._session_fraction(self._quote(9, 0)) is None

    def test_none_exactly_at_the_open(self):
        assert brief._session_fraction(self._quote(9, 30)) is None

    def test_small_shortly_after_the_open(self):
        fraction = brief._session_fraction(self._quote(10, 0))
        assert fraction == pytest.approx(30 / 390, abs=1e-6)

    def test_about_half_at_midday(self):
        assert brief._session_fraction(self._quote(12, 45)) == pytest.approx(0.5, abs=0.01)

    def test_one_at_the_close(self):
        assert brief._session_fraction(self._quote(16, 0)) == pytest.approx(1.0)

    def test_clamped_after_the_close(self):
        assert brief._session_fraction(self._quote(20, 0)) == 1.0

    def test_none_without_time_fields(self):
        assert brief._session_fraction({}) is None


class TestVolumeRow:
    def _quote(self, volume, average, state="POST", **extra):
        base = {"symbol": "AAPL", "regularMarketVolume": volume,
                "averageDailyVolume10Day": average, "marketState": state}
        base.update(extra)
        return base

    def test_full_session_compares_against_the_average(self):
        row = brief.volume_row(self._quote(60_000_000, 40_000_000), 1.5)
        assert row["pace"] == 1.5
        assert row["basis"] == "full session"
        assert row["unusual"] is True

    def test_normal_volume_is_not_flagged(self):
        row = brief.volume_row(self._quote(40_000_000, 40_000_000), 1.5)
        assert row["pace"] == 1.0 and row["unusual"] is False

    def test_intraday_scales_by_session_progress(self):
        """Half a day's volume at the halfway point is normal, not half-normal."""
        quote = self._quote(20_000_000, 40_000_000, state="REGULAR",
                            regularMarketTime=1_800_000_000, gmtOffSetMilliseconds=-4 * 3600 * 1000)
        local_midnight = 1_800_000_000 - (1_800_000_000 % 86400)
        quote["regularMarketTime"] = local_midnight + (12 * 3600 + 45 * 60) + 4 * 3600
        row = brief.volume_row(quote, 1.5)
        assert row["basis"] == "pace"
        assert row["pace"] == pytest.approx(1.0, abs=0.03)
        assert row["unusual"] is False

    def test_intraday_flags_a_genuine_surge(self):
        local_midnight = 1_800_000_000 - (1_800_000_000 % 86400)
        quote = self._quote(40_000_000, 40_000_000, state="REGULAR")
        quote["regularMarketTime"] = local_midnight + (12 * 3600 + 45 * 60) + 4 * 3600
        quote["gmtOffSetMilliseconds"] = -4 * 3600 * 1000
        row = brief.volume_row(quote, 1.5)
        assert row["pace"] == pytest.approx(2.0, abs=0.05)
        assert row["unusual"] is True

    def test_none_before_the_open_during_regular_state(self):
        local_midnight = 1_800_000_000 - (1_800_000_000 % 86400)
        quote = self._quote(1_000, 40_000_000, state="REGULAR")
        quote["regularMarketTime"] = local_midnight + 8 * 3600 + 4 * 3600
        quote["gmtOffSetMilliseconds"] = -4 * 3600 * 1000
        assert brief.volume_row(quote, 1.5) is None

    @pytest.mark.parametrize("volume,average", [(None, 40), (40, None), (0, 40), (40, 0)])
    def test_none_without_usable_numbers(self, volume, average):
        assert brief.volume_row(self._quote(volume, average), 1.5) is None


class TestVolVerdict:
    @pytest.mark.parametrize("ratio,expected", [
        (1.60, "rich"), (1.35, "rich"), (1.20, "fair"), (1.00, "fair"),
        (0.85, "cheap"), (0.60, "cheap"),
    ])
    def test_bands(self, ratio, expected):
        assert brief._vol_verdict(ratio) == expected


class TestRealizedVol:
    def _payload(self, closes):
        return json.dumps({"chart": {"result": [
            {"indicators": {"quote": [{"close": closes}]}}]}})

    def test_matches_the_annualised_standard_deviation(self):
        import math, statistics
        closes = [100 * (1.01 if i % 2 else 0.99) ** 1 for i in range(80)]
        # Rebuild as a compounding series so log returns are well defined.
        series, price = [], 100.0
        for i in range(80):
            price *= 1.01 if i % 2 else 0.99
            series.append(price)
        with mock.patch.object(brief, "_get", return_value=self._payload(series)):
            result = brief.realized_vol("AAPL", windows=(20,))
        returns = [math.log(series[i] / series[i - 1]) for i in range(1, len(series))]
        expected = statistics.stdev(returns[-20:]) * math.sqrt(252)
        assert result["hv20"] == pytest.approx(expected, abs=1e-4)

    def test_reports_each_requested_window(self):
        series = [100 + i * 0.5 + (i % 3) for i in range(120)]
        with mock.patch.object(brief, "_get", return_value=self._payload(series)):
            result = brief.realized_vol("AAPL")
        assert "hv20" in result and "hv60" in result

    def test_drops_windows_longer_than_the_history(self):
        series = [100 + (i % 5) for i in range(30)]
        with mock.patch.object(brief, "_get", return_value=self._payload(series)):
            result = brief.realized_vol("AAPL")
        assert "hv20" in result and "hv60" not in result

    def test_none_when_history_is_too_short(self):
        with mock.patch.object(brief, "_get", return_value=self._payload([100, 101])):
            assert brief.realized_vol("AAPL") is None

    def test_none_on_a_network_failure(self):
        with mock.patch.object(brief, "_get", side_effect=OSError("boom")):
            assert brief.realized_vol("AAPL") is None

    def test_ignores_null_bars(self):
        series = [100 + i for i in range(80)]
        with_gaps = series[:40] + [None] + series[40:]
        with mock.patch.object(brief, "_get", return_value=self._payload(with_gaps)):
            assert brief.realized_vol("AAPL", windows=(20,)) is not None


class TestVolatilityRows:
    def test_skips_indices_and_crypto(self):
        with mock.patch.object(brief, "_load_options_client", return_value=None), \
             mock.patch.object(brief, "realized_vol", return_value={"hv20": 0.2}):
            rows = brief.volatility_rows(["^GSPC", "BTC-USD", "AAPL"], 0.04, 12)
        assert [r["symbol"] for r in rows] == ["AAPL"]

    def test_works_without_the_options_skill_installed(self):
        with mock.patch.object(brief, "_load_options_client", return_value=None), \
             mock.patch.object(brief, "realized_vol", return_value={"hv20": 0.25}):
            rows = brief.volatility_rows(["AAPL"], 0.04, 12)
        assert rows[0]["hv20"] == 0.25
        assert "iv_hv_ratio" not in rows[0]      # no implied vol to compare against

    def test_computes_the_ratio_when_both_are_available(self):
        fake = mock.Mock()
        with mock.patch.object(brief, "_load_options_client", return_value=fake), \
             mock.patch.object(brief, "realized_vol", return_value={"hv20": 0.20}), \
             mock.patch.object(brief, "atm_iv", return_value={"atm_iv": 0.30, "iv_dte": 31.0}):
            rows = brief.volatility_rows(["AAPL"], 0.04, 12)
        assert rows[0]["iv_hv_ratio"] == 1.5
        assert rows[0]["verdict"] == "rich"

    def test_honours_the_limit(self):
        with mock.patch.object(brief, "_load_options_client", return_value=None), \
             mock.patch.object(brief, "realized_vol", return_value={"hv20": 0.2}):
            rows = brief.volatility_rows(["AAPL", "NVDA", "MSFT"], 0.04, 2)
        assert len(rows) == 2

    def test_symbol_with_no_data_is_dropped(self):
        with mock.patch.object(brief, "_load_options_client", return_value=None), \
             mock.patch.object(brief, "realized_vol", return_value=None):
            assert brief.volatility_rows(["AAPL"], 0.04, 12) == []


class TestAtmIv:
    def test_none_when_the_chain_cannot_be_fetched(self):
        oc = mock.Mock()
        oc.fetch_chain.side_effect = SystemExit("no such symbol")
        assert brief.atm_iv("NOPE", oc, 0.04) is None

    def test_picks_the_expiry_nearest_the_target_horizon(self):
        oc = mock.Mock()
        near, target, far = 1_000, 2_000, 3_000
        oc.fetch_chain.return_value = {"expirationDates": [near, target, far]}
        oc._spot_and_div.return_value = (100.0, 0.0)
        # 7, 30 and 90 days out respectively - the 30-day one must win.
        oc._years_to_expiry.side_effect = lambda ts: {near: 7, target: 30, far: 90}[ts] / 365.0
        oc.enrich.return_value = {"strike": 100.0, "iv": 0.25}
        brief.atm_iv("AAPL", oc, 0.04)
        assert oc.fetch_chain.call_args[0][1] == target
