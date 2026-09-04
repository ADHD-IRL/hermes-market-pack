"""Tests for the position-sizing skill. Pure arithmetic, no network."""

import pytest

import sizing


def args(**kwargs):
    return type("Args", (), kwargs)()


class TestShares:
    def test_sizes_from_the_stop_distance(self):
        result = sizing.cmd_shares(
            args(account=100000, risk_pct=1, entry=50, stop=47.50, target=None)
        )
        assert result["shares"] == 400          # $1,000 budget / $2.50 risk
        assert result["actual_risk"] == 1000.0
        assert result["direction"] == "long"

    def test_detects_a_short(self):
        result = sizing.cmd_shares(args(account=100000, risk_pct=1, entry=50, stop=52.50, target=None))
        assert result["direction"] == "short"
        assert result["shares"] == 400

    def test_truncates_rather_than_exceeding_the_budget(self):
        result = sizing.cmd_shares(args(account=10000, risk_pct=1, entry=50, stop=47, target=None))
        assert result["shares"] == 33           # 100/3 = 33.33, not 34
        assert result["actual_risk"] <= 100.0

    def test_warns_when_even_one_share_is_too_much(self):
        result = sizing.cmd_shares(args(account=100, risk_pct=1, entry=500, stop=400, target=None))
        assert result["shares"] == 0
        assert "warning" in result

    def test_reports_reward_to_risk_with_a_target(self):
        result = sizing.cmd_shares(args(account=100000, risk_pct=1, entry=50, stop=47.50, target=60))
        assert result["reward_risk_ratio"] == 4.0

    def test_a_wider_stop_means_fewer_shares(self):
        tight = sizing.cmd_shares(args(account=100000, risk_pct=1, entry=50, stop=49, target=None))
        wide = sizing.cmd_shares(args(account=100000, risk_pct=1, entry=50, stop=45, target=None))
        assert wide["shares"] < tight["shares"]

    def test_equal_entry_and_stop_is_rejected(self):
        with pytest.raises(SystemExit, match="cannot be equal"):
            sizing.cmd_shares(args(account=100000, risk_pct=1, entry=50, stop=50, target=None))

    @pytest.mark.parametrize("account,risk", [(0, 1), (-5000, 1), (100000, 0), (100000, 101)])
    def test_rejects_impossible_inputs(self, account, risk):
        with pytest.raises(SystemExit):
            sizing.cmd_shares(args(account=account, risk_pct=risk, entry=50, stop=45, target=None))


class TestContracts:
    def test_sizes_from_a_defined_max_loss(self):
        result = sizing.cmd_contracts(args(account=50000, risk_pct=2, max_loss=300, max_profit=None))
        assert result["contracts"] == 3         # $1,000 budget / $300
        assert result["actual_risk"] == 900.0

    def test_warns_when_one_contract_busts_the_budget(self):
        result = sizing.cmd_contracts(args(account=10000, risk_pct=1, max_loss=500, max_profit=None))
        assert result["contracts"] == 0
        assert "warning" in result

    def test_reports_reward_to_risk(self):
        result = sizing.cmd_contracts(args(account=50000, risk_pct=2, max_loss=300, max_profit=200))
        assert result["reward_risk_ratio"] == pytest.approx(0.67, abs=0.01)
        assert result["total_max_profit"] == 600.0

    def test_negative_max_loss_is_rejected(self):
        with pytest.raises(SystemExit, match="positive"):
            sizing.cmd_contracts(args(account=50000, risk_pct=2, max_loss=-300, max_profit=None))


class TestKelly:
    def test_matches_the_closed_form(self):
        # f* = (p*b - q) / b = (0.55*1.8 - 0.45) / 1.8 = 0.30
        result = sizing.cmd_kelly(args(win_rate=0.55, win_loss_ratio=1.8, fraction=0.25, account=None))
        assert result["full_kelly"] == pytest.approx(0.30, abs=1e-9)
        assert result["fractional_kelly"] == pytest.approx(0.075, abs=1e-9)

    def test_accepts_a_percentage_win_rate(self):
        as_pct = sizing.cmd_kelly(args(win_rate=55, win_loss_ratio=1.8, fraction=0.25, account=None))
        as_frac = sizing.cmd_kelly(args(win_rate=0.55, win_loss_ratio=1.8, fraction=0.25, account=None))
        assert as_pct["full_kelly"] == as_frac["full_kelly"]

    def test_negative_edge_sizes_to_zero_and_warns(self):
        result = sizing.cmd_kelly(args(win_rate=0.30, win_loss_ratio=1.0, fraction=0.25, account=None))
        assert result["full_kelly"] < 0
        assert result["suggested_risk_pct"] == 0.0
        assert "warning" in result

    def test_converts_to_dollars_with_an_account(self):
        result = sizing.cmd_kelly(args(win_rate=0.55, win_loss_ratio=1.8, fraction=0.25, account=100000))
        assert result["suggested_risk_dollars"] == pytest.approx(7500.0)

    def test_values_above_one_are_read_as_percentages(self):
        """1.5 means 1.5%, consistent with 55 meaning 55%."""
        result = sizing.cmd_kelly(args(win_rate=1.5, win_loss_ratio=1.8, fraction=0.25, account=None))
        assert result["win_rate"] == pytest.approx(0.015)

    @pytest.mark.parametrize("rate,ratio", [(0, 1.8), (150, 1.8), (-5, 1.8), (0.55, 0), (0.55, -1)])
    def test_rejects_impossible_inputs(self, rate, ratio):
        with pytest.raises(SystemExit):
            sizing.cmd_kelly(args(win_rate=rate, win_loss_ratio=ratio, fraction=0.25, account=None))


class TestHeat:
    def test_sums_open_risk(self):
        result = sizing.cmd_heat(
            args(account=100000, position=["AAPL:1000", "NVDA:1500", "SPY:800"], max_heat=6.0)
        )
        assert result["total_risk"] == 3300.0
        assert result["portfolio_heat_pct"] == 3.3
        assert result["assessment"] == "normal"
        assert result["remaining_budget"] == 2700.0

    @pytest.mark.parametrize("risk,expected", [
        (1000, "conservative"), (5000, "normal"), (9000, "elevated"), (15000, "excessive"),
    ])
    def test_classifies_the_heat_level(self, risk, expected):
        result = sizing.cmd_heat(args(account=100000, position=[f"X:{risk}"], max_heat=6.0))
        assert result["assessment"] == expected

    def test_remaining_budget_goes_negative_when_overexposed(self):
        result = sizing.cmd_heat(args(account=100000, position=["X:9000"], max_heat=6.0))
        assert result["remaining_budget"] < 0

    def test_symbols_containing_colons_keep_their_risk(self):
        result = sizing.cmd_heat(args(account=100000, position=["SPY 26-01-16 C500:1000"], max_heat=6.0))
        assert result["positions"][0]["risk"] == 1000.0

    @pytest.mark.parametrize("spec", ["AAPL", "AAPL:abc", "AAPL:-100"])
    def test_rejects_malformed_positions(self, spec):
        with pytest.raises(SystemExit):
            sizing.cmd_heat(args(account=100000, position=[spec], max_heat=6.0))


class TestRMultiple:
    def test_scores_a_long_winner(self):
        result = sizing.cmd_r_multiple(args(entry=50, stop=47.5, exit=57.5, shares=400))
        assert result["r_multiple"] == 3.0
        assert result["outcome"] == "win"
        assert result["total_pl"] == 3000.0

    def test_scores_a_short_winner(self):
        result = sizing.cmd_r_multiple(args(entry=50, stop=52.5, exit=45, shares=None))
        assert result["direction"] == "short"
        assert result["r_multiple"] == 2.0

    def test_a_full_stop_out_is_minus_one_r(self):
        result = sizing.cmd_r_multiple(args(entry=50, stop=47.5, exit=47.5, shares=None))
        assert result["r_multiple"] == -1.0
        assert result["outcome"] == "loss"

    def test_a_scratch_is_zero_r(self):
        assert sizing.cmd_r_multiple(args(entry=50, stop=47.5, exit=50, shares=None))["outcome"] == "breakeven"

    def test_equal_entry_and_stop_is_rejected(self):
        with pytest.raises(SystemExit):
            sizing.cmd_r_multiple(args(entry=50, stop=50, exit=60, shares=None))
