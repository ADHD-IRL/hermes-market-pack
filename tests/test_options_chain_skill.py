"""Tests for the options-chain skill. No network: fetches are mocked."""

import math
from unittest import mock

import pytest

import options_client as oc


class TestBlackScholes:
    def test_atm_call_and_put_satisfy_put_call_parity(self):
        spot, strike, t, vol, rate, div = 100.0, 100.0, 0.5, 0.25, 0.04, 0.0
        call = oc.bs_price(spot, strike, t, vol, rate, div, "call")
        put = oc.bs_price(spot, strike, t, vol, rate, div, "put")
        # C - P = S*e^(-qT) - K*e^(-rT)
        expected = spot * math.exp(-div * t) - strike * math.exp(-rate * t)
        assert call - put == pytest.approx(expected, abs=1e-9)

    def test_delta_parity(self):
        args = (100.0, 100.0, 0.5, 0.25, 0.04, 0.0)
        call_delta = oc.greeks(*args, "call")["delta"]
        put_delta = oc.greeks(*args, "put")["delta"]
        assert call_delta - put_delta == pytest.approx(1.0, abs=1e-9)

    def test_gamma_and_vega_match_across_call_and_put(self):
        args = (100.0, 105.0, 0.25, 0.30, 0.04, 0.0)
        call = oc.greeks(*args, "call")
        put = oc.greeks(*args, "put")
        assert call["gamma"] == pytest.approx(put["gamma"], rel=1e-12)
        assert call["vega"] == pytest.approx(put["vega"], rel=1e-12)

    def test_deep_itm_call_delta_approaches_one(self):
        assert oc.greeks(200.0, 100.0, 0.25, 0.2, 0.04, 0.0, "call")["delta"] > 0.99

    def test_deep_otm_put_delta_approaches_zero(self):
        assert abs(oc.greeks(200.0, 100.0, 0.25, 0.2, 0.04, 0.0, "put")["delta"]) < 0.01

    def test_long_option_theta_is_negative(self):
        assert oc.greeks(100.0, 100.0, 0.25, 0.25, 0.04, 0.0, "call")["theta"] < 0

    def test_at_expiry_prices_to_intrinsic(self):
        assert oc.bs_price(110.0, 100.0, 0.0, 0.3, 0.04, 0.0, "call") == 10.0
        assert oc.bs_price(90.0, 100.0, 0.0, 0.3, 0.04, 0.0, "call") == 0.0
        assert oc.bs_price(90.0, 100.0, 0.0, 0.3, 0.04, 0.0, "put") == 10.0

    def test_at_expiry_greeks_are_flat(self):
        g = oc.greeks(110.0, 100.0, 0.0, 0.3, 0.04, 0.0, "call")
        assert g["gamma"] == 0.0 and g["theta"] == 0.0 and g["vega"] == 0.0
        assert g["delta"] == 1.0

    def test_higher_vol_raises_option_value(self):
        low = oc.bs_price(100.0, 100.0, 0.5, 0.15, 0.04, 0.0, "call")
        high = oc.bs_price(100.0, 100.0, 0.5, 0.45, 0.04, 0.0, "call")
        assert high > low


class TestImpliedVol:
    @pytest.mark.parametrize("vol", [0.10, 0.25, 0.60, 1.20])
    @pytest.mark.parametrize("kind", ["call", "put"])
    def test_round_trips_through_the_pricer(self, vol, kind):
        spot, strike, t, rate = 100.0, 105.0, 0.4, 0.04
        price = oc.bs_price(spot, strike, t, vol, rate, 0.0, kind)
        solved = oc.implied_vol(price, spot, strike, t, rate, 0.0, kind)
        assert solved == pytest.approx(vol, abs=1e-4)

    def test_returns_none_below_intrinsic(self):
        # A call worth less than S-K is a crossed or stale quote.
        assert oc.implied_vol(1.0, 120.0, 100.0, 0.5, 0.04, 0.0, "call") is None

    def test_itm_european_put_below_undiscounted_intrinsic_still_solves(self):
        """Regression: an ITM European put trades under K - S; that is not an error."""
        spot, strike, t, rate, vol = 100.0, 105.0, 0.4, 0.04, 0.10
        price = oc.bs_price(spot, strike, t, vol, rate, 0.0, "put")
        assert price < (strike - spot)  # below undiscounted intrinsic, as expected
        assert oc.implied_vol(price, spot, strike, t, rate, 0.0, "put") == pytest.approx(vol, abs=1e-4)

    def test_returns_none_for_nonpositive_price(self):
        assert oc.implied_vol(0.0, 100.0, 100.0, 0.5, 0.04) is None
        assert oc.implied_vol(None, 100.0, 100.0, 0.5, 0.04) is None

    def test_returns_none_at_expiry(self):
        assert oc.implied_vol(5.0, 100.0, 100.0, 0.0, 0.04) is None


class TestDividendYield:
    """Yahoo mixes percent and fraction fields; the fraction must win."""

    def test_prefers_the_fraction_field(self):
        spot, div = oc._spot_and_div(
            {"quote": {"regularMarketPrice": 320.0, "dividendYield": 0.33,
                       "trailingAnnualDividendYield": 0.0032}}
        )
        assert spot == 320.0
        assert div == pytest.approx(0.0032)

    def test_scales_the_percent_field_when_falling_back(self):
        _, div = oc._spot_and_div(
            {"quote": {"regularMarketPrice": 25.0, "dividendYield": 4.24}}
        )
        assert div == pytest.approx(0.0424)

    def test_absent_dividend_is_zero(self):
        _, div = oc._spot_and_div({"quote": {"regularMarketPrice": 350.0}})
        assert div == 0.0

    def test_implausible_yield_is_discarded(self):
        _, div = oc._spot_and_div(
            {"quote": {"regularMarketPrice": 10.0, "trailingAnnualDividendYield": 8.0}}
        )
        assert div == 0.0

    def test_missing_spot_is_fatal(self):
        with pytest.raises(SystemExit):
            oc._spot_and_div({"quote": {}})


class TestEnrich:
    def _contract(self, **overrides):
        base = {
            "contractSymbol": "TEST260116C00100000",
            "strike": 100.0,
            "expiration": 2_000_000_000,
            "bid": 5.0,
            "ask": 5.4,
            "lastPrice": 5.2,
            "impliedVolatility": 0.30,
            "volume": 10,
            "openInterest": 100,
            "inTheMoney": False,
        }
        base.update(overrides)
        return base

    def _now(self, days_before):
        return 2_000_000_000 + oc.EXPIRY_CUTOFF_SECONDS - days_before * 86400

    def test_mid_is_the_average_of_bid_and_ask(self):
        row = oc.enrich(self._contract(), 100.0, 0.04, 0.0, "call", now_ts=self._now(30))
        assert row["mid"] == pytest.approx(5.2)

    def test_solved_iv_reprices_to_the_mid(self):
        row = oc.enrich(self._contract(), 100.0, 0.04, 0.0, "call", now_ts=self._now(30))
        assert row["iv_source"] == "solved"
        assert row["theoretical"] == pytest.approx(row["mid"], abs=1e-3)

    def test_falls_back_to_vendor_iv_without_a_two_sided_quote(self):
        contract = self._contract(bid=0, ask=0, lastPrice=0)
        row = oc.enrich(contract, 100.0, 0.04, 0.0, "call", now_ts=self._now(30))
        assert row["iv_source"] == "yahoo"
        assert row["iv"] == pytest.approx(0.30)

    def test_absurd_vendor_iv_is_rejected(self):
        contract = self._contract(bid=0, ask=0, lastPrice=0, impliedVolatility=25.0)
        row = oc.enrich(contract, 100.0, 0.04, 0.0, "call", now_ts=self._now(30))
        assert row["iv"] is None and row["iv_source"] is None

    def test_mid_falls_back_to_last_trade(self):
        contract = self._contract(bid=None, ask=None, lastPrice=4.75)
        row = oc.enrich(contract, 100.0, 0.04, 0.0, "call", now_ts=self._now(30))
        assert row["mid"] == pytest.approx(4.75)

    def test_dte_is_reported_in_days(self):
        row = oc.enrich(self._contract(), 100.0, 0.04, 0.0, "call", now_ts=self._now(30))
        assert row["dte"] == pytest.approx(30.0, abs=0.1)


class TestSpreadPct:
    def test_computes_spread_relative_to_mid(self):
        assert oc._spread_pct({"bid": 1.0, "ask": 1.2, "mid": 1.1}) == pytest.approx(18.18, abs=0.01)

    def test_none_when_a_side_is_missing(self):
        assert oc._spread_pct({"bid": None, "ask": 1.2, "mid": 1.2}) is None


class TestExpiryResolution:
    def test_skips_expiries_past_their_cutoff(self):
        expired, live = 1_000_000_000, 4_000_000_000
        chosen = oc._resolve_expiry({"expirationDates": [expired, live]}, None)
        assert chosen == live

    def test_unlisted_expiry_is_rejected(self):
        with pytest.raises(SystemExit):
            oc._resolve_expiry({"expirationDates": [4_000_000_000]}, "1999-01-01")


class TestFetch:
    def test_vendor_error_is_surfaced(self):
        payload = '{"optionChain": {"error": "Not Found", "result": []}}'
        with mock.patch.object(oc, "_get", return_value=payload), \
             mock.patch.object(oc, "_fetch_crumb", return_value="x"):
            with pytest.raises(SystemExit, match="Yahoo error"):
                oc.fetch_chain("NOPE")

    def test_network_failure_is_reported_not_swallowed(self):
        with mock.patch.object(oc, "_get", side_effect=OSError("boom")), \
             mock.patch.object(oc, "_fetch_crumb", return_value="x"):
            with pytest.raises(SystemExit, match="Could not fetch"):
                oc.fetch_chain("AAPL")
