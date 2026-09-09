"""Tests for the options-strategy skill. Pure arithmetic, no network."""

import pytest

import strategy as st


def legs(*specs):
    return [st.parse_leg(s) for s in specs]


class TestLegParsing:
    def test_parses_a_full_leg(self):
        leg = st.parse_leg("sell,call,320,4.50,2")
        assert leg == {
            "side": "sell", "kind": "call", "strike": 320.0,
            "premium": 4.5, "qty": 2.0, "multiplier": 100,
        }

    def test_quantity_defaults_to_one(self):
        assert st.parse_leg("buy,put,100,2")["qty"] == 1.0

    def test_stock_legs_have_no_multiplier(self):
        assert st.parse_leg("buy,stock,150,0,100")["multiplier"] == 1

    def test_is_case_insensitive(self):
        assert st.parse_leg("BUY,CALL,100,5")["side"] == "buy"

    @pytest.mark.parametrize("spec", [
        "buy,call,100",              # too few fields
        "hold,call,100,5",           # bad side
        "buy,future,100,5",          # bad kind
        "buy,call,abc,5",            # non-numeric strike
        "buy,call,0,5",              # non-positive strike
        "buy,call,100,-5",           # negative premium
        "buy,call,100,5,0",          # zero quantity
    ])
    def test_rejects_malformed_legs(self, spec):
        with pytest.raises(st.LegError):
            st.parse_leg(spec)


class TestKnownPayoffs:
    """Textbook structures with hand-checkable answers."""

    def test_bull_call_spread(self):
        position = legs("buy,call,100,5", "sell,call,110,2")
        report = st.analyze(position)
        assert report["net_cash"] == -300.0          # $3.00 debit
        assert report["max_loss"] == -300.0
        assert report["max_profit"] == 700.0         # $10 wide less $3 paid
        assert report["breakevens"] == [103.0]
        assert report["risk_reward"] == pytest.approx(2.333, abs=0.001)

    def test_cash_secured_put_loses_all_the_way_to_zero(self):
        position = legs("sell,put,100,3")
        report = st.analyze(position)
        assert report["net_cash"] == 300.0
        assert report["max_profit"] == 300.0
        assert report["max_loss"] == -9700.0         # stock to zero
        assert report["breakevens"] == [97.0]

    def test_covered_call(self):
        position = legs("buy,stock,100,0,100", "sell,call,105,3")
        report = st.analyze(position)
        assert report["max_profit"] == 800.0          # $5 upside + $3 premium
        assert report["max_loss"] == -9700.0
        assert report["breakevens"] == [97.0]

    def test_long_straddle_has_unlimited_upside(self):
        position = legs("buy,call,100,5", "buy,put,100,5")
        report = st.analyze(position)
        assert report["max_loss"] == -1000.0
        assert report["max_profit"] == st.UNBOUNDED
        assert report["breakevens"] == [90.0, 110.0]

    def test_naked_short_call_has_unlimited_loss(self):
        report = st.analyze(legs("sell,call,100,5"))
        assert report["max_loss"] == st.UNBOUNDED
        assert report["max_profit"] == 500.0

    def test_iron_condor(self):
        position = legs(
            "buy,put,90,1", "sell,put,95,2", "sell,call,105,2", "buy,call,110,1"
        )
        report = st.analyze(position)
        assert report["net_cash"] == 200.0            # $2.00 credit
        assert report["max_profit"] == 200.0
        assert report["max_loss"] == -300.0           # $5 wing less $2 credit
        assert report["breakevens"] == [93.0, 107.0]

    def test_risk_reward_absent_when_a_tail_is_unbounded(self):
        assert "risk_reward" not in st.analyze(legs("sell,call,100,5"))


class TestLegPayoff:
    @pytest.mark.parametrize("price,expected", [(90, -500.0), (100, -500.0), (105, 0.0), (120, 1500.0)])
    def test_long_call(self, price, expected):
        assert st.leg_payoff(st.parse_leg("buy,call,100,5"), price) == expected

    @pytest.mark.parametrize("price,expected", [(120, 300.0), (100, 300.0), (97, 0.0), (80, -1700.0)])
    def test_short_put(self, price, expected):
        assert st.leg_payoff(st.parse_leg("sell,put,100,3"), price) == expected

    def test_short_stock_profits_when_price_falls(self):
        assert st.leg_payoff(st.parse_leg("sell,stock,100,0,50"), 90) == 500.0

    def test_quantity_scales_the_payoff(self):
        one = st.leg_payoff(st.parse_leg("buy,call,100,5,1"), 120)
        three = st.leg_payoff(st.parse_leg("buy,call,100,5,3"), 120)
        assert three == pytest.approx(3 * one)


class TestPresets:
    def test_iron_condor_rejects_crossed_strikes(self):
        args = type("A", (), {
            "qty": 1, "long_put_strike": 110, "short_put_strike": 95,
            "short_call_strike": 105, "long_call_strike": 90,
            "long_put_premium": 1, "short_put_premium": 2,
            "short_call_premium": 2, "long_call_premium": 1,
        })()
        with pytest.raises(SystemExit, match="ascend"):
            st.preset_legs("iron-condor", args)

    def test_unknown_preset_is_rejected(self):
        with pytest.raises(SystemExit, match="Unknown preset"):
            st.preset_legs("banana", type("A", (), {"qty": 1})())

    def test_short_straddle_inverts_the_structure(self):
        args = type("A", (), {"qty": 1, "strike": 100, "call_premium": 5,
                              "put_premium": 5, "short": True})()
        assert all(leg["side"] == "sell" for leg in st.preset_legs("straddle", args))


class TestProbabilityOfProfit:
    def test_long_call_profit_odds_are_between_zero_and_one(self):
        pop = st.probability_of_profit(legs("buy,call,100,5"), 100, 0.30, 30)
        assert 0.0 < pop["probability_of_profit"] < 1.0

    def test_far_otm_short_put_usually_expires_worthless(self):
        pop = st.probability_of_profit(legs("sell,put,70,1"), 100, 0.25, 30)
        assert pop["probability_of_profit"] > 0.95

    def test_higher_vol_lowers_the_odds_for_a_short_wing(self):
        calm = st.probability_of_profit(legs("sell,put,90,1"), 100, 0.15, 30)
        wild = st.probability_of_profit(legs("sell,put,90,1"), 100, 0.60, 30)
        assert wild["probability_of_profit"] < calm["probability_of_profit"]

    @pytest.mark.parametrize("spot,vol,days", [(None, 0.3, 30), (100, None, 30), (100, 0.3, None), (100, 0.3, 0)])
    def test_omitted_when_inputs_are_incomplete(self, spot, vol, days):
        assert st.probability_of_profit(legs("buy,call,100,5"), spot, vol, days) is None

    def test_analyze_omits_the_block_without_inputs(self):
        assert "probability" not in st.analyze(legs("buy,call,100,5"))


class TestReport:
    def test_pl_at_spot_is_reported_when_given(self):
        report = st.analyze(legs("buy,call,100,5"), spot=120)
        assert report["pl_at_spot"] == 1500.0

    def test_payoff_table_spans_a_range_of_prices(self):
        table = st.analyze(legs("buy,call,100,5"), spot=100)["payoff_table"]
        assert len(table) == 15
        assert table[0]["price"] < 100 < table[-1]["price"]

    def test_credit_and_debit_are_labelled(self):
        assert st.analyze(legs("sell,put,100,3"))["position"] == "credit"
        assert st.analyze(legs("buy,call,100,5"))["position"] == "debit"
