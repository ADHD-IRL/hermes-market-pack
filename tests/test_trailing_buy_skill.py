"""Tests for the trailing-buy skill. Temp database, mocked quotes, no network."""

import json
import pathlib
import time
from unittest import mock

import pytest

import trailing_buy as tb


def args(**kwargs):
    defaults = dict(
        plan=1, price=None, allow_extended=False, no_context=True, notes=None,
        model=None, atr_multiple=None, resume=False, all=False, state=None,
        ticks=20, floor=None, reason=None, fill_price=None, payload=None,
    )
    defaults.update(kwargs)
    return type("Args", (), defaults)()


@pytest.fixture(autouse=True)
def isolated_halt_file(tmp_path, monkeypatch):
    """No test may reach the real kill switch under the user's Hermes home."""
    monkeypatch.setenv("TRAILING_BUY_HALT_FILE", str(tmp_path / "halt"))
    monkeypatch.delenv("TRAILING_BUY_DB", raising=False)


@pytest.fixture()
def conn(tmp_path):
    connection = tb.connect(tmp_path / "trailing_buy.db")
    yield connection
    connection.close()


def make_plan(conn, **overrides):
    payload = dict(
        symbol="AGNC", arm=9.98, floor=9.20, shares=200.0, atr_multiple=1.0,
        max_notional=2100.0, max_orders_per_day=2, max_orders_total=4,
        cooldown_seconds=0, limit_band_pct=0.5, limit_offset_pct=0.1,
        stale_seconds=180, autonomous=False, no_atr=True, notes=None,
    )
    payload.update(overrides)
    return tb.cmd_plan_create(args(**payload), conn)


def row(conn, plan_id=1, **overrides):
    """Fetch a plan row, optionally forcing columns first."""
    if overrides:
        sets = ", ".join(f"{k}=?" for k in overrides)
        conn.execute(f"UPDATE plans SET {sets} WHERE id=?", (*overrides.values(), plan_id))
        conn.commit()
    return conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()


def tick(conn, price, plan_id=1):
    return tb.cmd_tick(args(plan=plan_id, price=price), conn)


def quote(price, market_state="REGULAR", age=0.0, symbol="AGNC"):
    return {
        "symbol": symbol,
        "regularMarketPrice": price,
        "regularMarketTime": int(time.time() - age),
        "gmtOffSetMilliseconds": -4 * 3600 * 1000,
        "marketState": market_state,
    }


def bars(series):
    """A chart payload with the high/low/close the ATR maths needs."""
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "indicators": {
                            "quote": [
                                {
                                    "high": [h for h, _, _ in series],
                                    "low": [l for _, l, _ in series],
                                    "close": [c for _, _, c in series],
                                }
                            ]
                        }
                    }
                ]
            }
        }
    )


def trigger_plan(conn, **overrides):
    """Walk a plan all the way to TRIGGERED, which is where the model gets asked."""
    make_plan(conn, **overrides)
    tick(conn, 9.94)
    tick(conn, 9.71)
    tick(conn, 9.90)
    return row(conn)


class TestPlanCreate:
    def test_records_a_plan_in_the_armed_state(self, conn):
        plan = make_plan(conn)
        assert plan["id"] == 1
        assert plan["state"] == tb.ARMED
        assert plan["state_seq"] == 0

    def test_uppercases_the_symbol(self, conn):
        assert make_plan(conn, symbol="agnc")["symbol"] == "AGNC"

    def test_a_floor_above_the_arm_price_is_refused(self, conn):
        with pytest.raises(SystemExit, match="must sit below the arm price"):
            make_plan(conn, floor=10.50)

    def test_a_floor_equal_to_the_arm_price_is_refused(self, conn):
        with pytest.raises(SystemExit, match="must sit below the arm price"):
            make_plan(conn, floor=9.98)

    def test_negative_shares_are_refused(self, conn):
        with pytest.raises(SystemExit, match="Shares must be positive"):
            make_plan(conn, shares=-10)

    def test_a_zero_atr_multiple_is_refused(self, conn):
        with pytest.raises(SystemExit, match="ATR multiple must be positive"):
            make_plan(conn, atr_multiple=0)

    def test_a_size_over_the_notional_cap_is_refused_at_creation(self, conn):
        with pytest.raises(SystemExit, match="over the notional cap"):
            make_plan(conn, shares=5000, max_notional=2000)

    def test_a_plan_without_bars_warns_about_the_fallback_trail(self, conn):
        assert "No ATR yet" in make_plan(conn)["warning"]

    def test_autonomous_mode_says_so_out_loud(self, conn):
        plan = make_plan(conn, autonomous=True)
        assert plan["autonomous"] == 1
        assert "without waiting for `approve`" in plan["warning_autonomous"]


class TestEvaluate:
    """The pure transition function: no clock, no network, no model."""

    def test_arming_starts_the_trail_at_the_current_price(self, conn):
        make_plan(conn)
        state, low, trig, _ = tb.evaluate(row(conn), 9.94, 0.10)
        assert (state, low, trig) == (tb.TRAILING, 9.94, 10.04)

    def test_a_price_exactly_at_the_arm_level_arms(self, conn):
        make_plan(conn)
        assert tb.evaluate(row(conn), 9.98, 0.10)[0] == tb.TRAILING

    def test_a_price_above_the_arm_level_waits(self, conn):
        make_plan(conn)
        state, low, _, reason = tb.evaluate(row(conn), 10.20, 0.10)
        assert (state, low) == (tb.ARMED, None)
        assert "above the arm price" in reason

    def test_the_low_tracks_downwards(self, conn):
        make_plan(conn)
        plan = row(conn, state=tb.TRAILING, running_low=9.90)
        assert tb.evaluate(plan, 9.71, 0.10)[1] == 9.71

    def test_the_low_ignores_a_higher_price(self, conn):
        make_plan(conn)
        plan = row(conn, state=tb.TRAILING, running_low=9.71)
        assert tb.evaluate(plan, 9.75, 0.50)[1] == 9.71

    def test_a_bounce_of_one_atr_triggers(self, conn):
        make_plan(conn)
        plan = row(conn, state=tb.TRAILING, running_low=9.71)
        state, _, trig, _ = tb.evaluate(plan, 9.81, 0.10)
        assert state == tb.TRIGGERED
        assert trig == 9.81  # 9.71 low + 1.0 x 0.10 ATR

    def test_a_bounce_short_of_the_trigger_keeps_trailing(self, conn):
        make_plan(conn)
        plan = row(conn, state=tb.TRAILING, running_low=9.71)
        assert tb.evaluate(plan, 9.80, 0.10)[0] == tb.TRAILING

    def test_a_wider_multiple_needs_a_bigger_bounce(self, conn):
        make_plan(conn, atr_multiple=2.0)
        plan = row(conn, state=tb.TRAILING, running_low=9.71)
        assert tb.evaluate(plan, 9.85, 0.10)[0] == tb.TRAILING
        assert tb.evaluate(plan, 9.91, 0.10)[0] == tb.TRIGGERED

    def test_breaking_the_floor_halts_from_armed(self, conn):
        make_plan(conn)
        state, _, _, reason = tb.evaluate(row(conn), 9.19, 0.10)
        assert state == tb.HALTED
        assert "broke the floor" in reason

    def test_breaking_the_floor_halts_from_trailing(self, conn):
        make_plan(conn)
        plan = row(conn, state=tb.TRAILING, running_low=9.30)
        assert tb.evaluate(plan, 9.10, 0.10)[0] == tb.HALTED

    def test_a_price_exactly_on_the_floor_does_not_halt(self, conn):
        make_plan(conn)
        assert tb.evaluate(row(conn), 9.20, 0.10)[0] == tb.TRAILING

    def test_a_triggered_plan_keeps_the_low_but_does_not_retrigger(self, conn):
        make_plan(conn)
        plan = row(conn, state=tb.TRIGGERED, running_low=9.71, trigger_price=9.81)
        state, low, _, _ = tb.evaluate(plan, 9.65, 0.10)
        assert (state, low) == (tb.TRIGGERED, 9.65)


class TestTick:
    def test_a_tick_writes_a_row_and_moves_the_state(self, conn):
        make_plan(conn)
        result = tick(conn, 9.94)
        assert result["state"] == tb.TRAILING
        assert result["acted"] is True
        assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1

    def test_an_injected_price_is_labelled_as_such(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        assert conn.execute("SELECT source FROM ticks WHERE id=1").fetchone()[0] == "injected"

    def test_a_tick_that_changes_nothing_still_logs(self, conn):
        make_plan(conn)
        result = tick(conn, 10.50)
        assert result["acted"] is False
        assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1

    def test_the_full_walk_reaches_triggered(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        tick(conn, 9.71)
        result = tick(conn, 9.90)
        assert result["state"] == tb.TRIGGERED
        assert result["running_low"] == 9.71

    def test_a_floor_breach_tells_the_caller_not_to_resume(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        result = tick(conn, 9.10)
        assert result["state"] == tb.HALTED
        assert "will not resume on its own" in result["next"]

    def test_a_terminal_plan_refuses_to_be_polled(self, conn):
        make_plan(conn)
        tb.cmd_plan_cancel(args(), conn)
        with pytest.raises(SystemExit, match="nothing to poll"):
            tick(conn, 9.94)

    def test_a_live_tick_outside_the_session_observes_but_does_not_act(self, conn):
        make_plan(conn)
        with mock.patch.object(tb, "fetch_quote", return_value=quote(9.94, "CLOSED")), \
             mock.patch.object(tb, "atr", return_value=0.10):
            result = tb.cmd_tick(args(price=None), conn)
        assert result["acted"] is False
        assert result["state"] == tb.ARMED
        assert "outside the regular session" in result["reason"]

    def test_allow_extended_lets_an_after_hours_tick_act(self, conn):
        make_plan(conn)
        with mock.patch.object(tb, "fetch_quote", return_value=quote(9.94, "POST")), \
             mock.patch.object(tb, "atr", return_value=0.10):
            result = tb.cmd_tick(args(price=None, allow_extended=True), conn)
        assert result["state"] == tb.TRAILING

    def test_a_stale_quote_cannot_move_anything(self, conn):
        make_plan(conn)
        with mock.patch.object(tb, "fetch_quote", return_value=quote(9.94, age=600)), \
             mock.patch.object(tb, "atr", return_value=0.10):
            result = tb.cmd_tick(args(price=None), conn)
        assert result["acted"] is False
        assert "over the 180s limit" in result["reason"]

    def test_an_observed_only_tick_is_still_recorded(self, conn):
        make_plan(conn)
        with mock.patch.object(tb, "fetch_quote", return_value=quote(9.94, "CLOSED")), \
             mock.patch.object(tb, "atr", return_value=0.10):
            tb.cmd_tick(args(price=None), conn)
        assert conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1


class TestAtr:
    def test_wilder_smoothing_matches_a_hand_computed_series(self, conn):
        # Fifteen flat bars give fourteen true ranges of exactly 1.00, so the seed
        # average is 1.00. One 3.00-range bar then smooths to (1.00*13 + 3.00)/14.
        series = [(101.0, 100.0, 100.5)] * 15 + [(103.5, 100.5, 102.0)]
        with mock.patch.object(tb, "_get", return_value=bars(series)):
            assert tb.atr("AGNC", period=14) == pytest.approx(16 / 14, abs=1e-4)

    def test_a_widening_range_raises_the_atr_above_the_quiet_level(self, conn):
        quiet = [(101.0, 100.0, 100.5)] * 15
        loud = quiet + [(105.0, 100.0, 102.0)] * 3
        with mock.patch.object(tb, "_get", return_value=bars(loud)):
            assert tb.atr("AGNC", period=14) > 1.0

    def test_too_few_bars_gives_no_atr_rather_than_a_bad_one(self, conn):
        with mock.patch.object(tb, "_get", return_value=bars([(101.0, 100.0, 100.5)] * 5)):
            assert tb.atr("AGNC", period=14) is None

    def test_a_failed_fetch_is_none_not_an_exception(self, conn):
        with mock.patch.object(tb, "_get", side_effect=OSError("boom")):
            assert tb.atr("AGNC") is None

    def test_null_bars_are_dropped_rather_than_counted(self, conn):
        payload = json.loads(bars([(101.0, 100.0, 100.5)] * 16))
        payload["chart"]["result"][0]["indicators"]["quote"][0]["high"][3] = None
        with mock.patch.object(tb, "_get", return_value=json.dumps(payload)):
            assert tb.atr("AGNC", period=14) == pytest.approx(1.0, abs=1e-4)

    def test_the_trail_falls_back_to_one_percent_without_bars(self, conn):
        make_plan(conn)
        assert tb._effective_atr(conn, row(conn), 10.00, refresh=False) == 0.10


class TestExDividend:
    def _payload(self, dates_and_amounts):
        return json.dumps(
            {
                "chart": {
                    "result": [
                        {
                            "events": {
                                "dividends": {
                                    str(ts): {"date": ts, "amount": amount}
                                    for ts, amount in dates_and_amounts
                                }
                            }
                        }
                    ]
                }
            }
        )

    def test_a_monthly_payer_projects_the_next_ex_date(self, conn):
        # 2026-06-01, 07-01, 08-01 at roughly 30-day spacing.
        stamps = [(1780272000, 0.12), (1782864000, 0.12), (1785542400, 0.12)]
        with mock.patch.object(tb, "_get", return_value=self._payload(stamps)):
            out = tb.ex_dividend("AGNC", today="2026-08-05")
        assert out["payments_last_year"] == 3
        assert 28 <= out["cadence_days"] <= 32
        assert "Projected" in out["estimate_note"]

    def test_a_drop_on_the_ex_date_is_flagged(self, conn):
        stamps = [(1785542400, 0.12)]
        with mock.patch.object(tb, "_get", return_value=self._payload(stamps)):
            out = tb.ex_dividend("AGNC", today=out_date(1785542400))
        assert out["ex_div_today"] is True

    def test_a_stale_feed_still_projects_a_future_ex_date(self, conn):
        # Last payment six months ago: the projection rolls forward rather than
        # naming a date that has already been and gone.
        stamps = [(1772496000, 0.12), (1775088000, 0.12), (1777680000, 0.12)]
        with mock.patch.object(tb, "_get", return_value=self._payload(stamps)):
            out = tb.ex_dividend("AGNC", today="2026-09-17")
        assert out["estimated_next_ex_date"] >= "2026-09-17"
        assert out["estimated_days_away"] >= 0

    def test_no_dividend_history_is_none(self, conn):
        with mock.patch.object(tb, "_get", return_value=self._payload([])):
            assert tb.ex_dividend("AGNC") is None

    def test_a_failed_fetch_is_none(self, conn):
        with mock.patch.object(tb, "_get", side_effect=OSError("boom")):
            assert tb.ex_dividend("AGNC") is None


def out_date(ts):
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()


class TestDecide:
    def test_a_decision_with_no_evidence_is_refused(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="Cite the tick ids"):
            tb.cmd_decide(args(action="buy", rationale="because", evidence_tick_ids=""), conn)

    def test_a_decision_citing_a_tick_that_does_not_exist_is_refused(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="not on plan 1"):
            tb.cmd_decide(args(action="buy", rationale="because", evidence_tick_ids="999"), conn)

    def test_a_decision_citing_another_plans_ticks_is_refused(self, conn):
        trigger_plan(conn)
        make_plan(conn, symbol="NLY")
        tb.cmd_tick(args(plan=2, price=9.94), conn)
        foreign = conn.execute("SELECT MAX(id) FROM ticks WHERE plan_id=2").fetchone()[0]
        with pytest.raises(SystemExit, match=f"{foreign}"):
            tb.cmd_decide(
                args(plan=1, action="buy", rationale="because", evidence_tick_ids=str(foreign)), conn
            )

    def test_non_numeric_evidence_is_refused(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="comma-separated list"):
            tb.cmd_decide(args(action="hold", rationale="x", evidence_tick_ids="one,two"), conn)

    def test_an_empty_rationale_is_refused(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="needs a rationale"):
            tb.cmd_decide(args(action="hold", rationale="   ", evidence_tick_ids="1"), conn)

    def test_a_decision_may_not_move_a_guardrail(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="cannot change floor_price"):
            tb.cmd_decide(
                args(action="buy", rationale="x", evidence_tick_ids="1", floor_price=8.0), conn
            )

    def test_a_refused_decision_leaves_nothing_in_the_ledger(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit):
            tb.cmd_decide(args(action="buy", rationale="x", evidence_tick_ids="999"), conn)
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0

    def test_hold_goes_back_to_trailing(self, conn):
        trigger_plan(conn)
        out = tb.cmd_decide(args(action="hold", rationale="mechanical", evidence_tick_ids="1,2,3"), conn)
        assert out["state"] == tb.TRAILING

    def test_adjust_widens_the_trail(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(
            args(action="adjust", rationale="too jumpy", evidence_tick_ids="3", atr_multiple=2.5), conn
        )
        assert row(conn)["atr_multiple"] == 2.5

    def test_adjust_without_a_multiple_is_refused_and_writes_nothing(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="needs a positive --atr-multiple"):
            tb.cmd_decide(args(action="adjust", rationale="x", evidence_tick_ids="3"), conn)
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0

    def test_escalate_parks_the_plan_for_the_user(self, conn):
        trigger_plan(conn)
        out = tb.cmd_decide(args(action="escalate", rationale="odd news", evidence_tick_ids="3"), conn)
        assert out["state"] == tb.ESCALATED

    def test_buy_leaves_the_plan_triggered_so_the_spec_shares_its_state(self, conn):
        trigger_plan(conn)
        out = tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="1,2,3"), conn)
        assert out["state"] == tb.TRIGGERED
        assert out["evidence_tick_ids"] == [1, 2, 3]

    def test_buy_on_a_plan_that_is_not_triggered_is_refused(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        with pytest.raises(SystemExit, match="not TRIGGERED"):
            tb.cmd_decide(args(action="buy", rationale="early", evidence_tick_ids="1"), conn)

    def test_an_unknown_action_is_refused(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="Action must be one of"):
            tb.cmd_decide(args(action="sell", rationale="x", evidence_tick_ids="1"), conn)


class TestOrderGuardrails:
    def _spec(self, conn, **overrides):
        spec = tb.build_spec(row(conn), 9.85)
        spec.update(overrides)
        return spec

    def test_a_clean_order_has_no_refusals(self, conn):
        trigger_plan(conn)
        assert tb.check_order_guardrails(conn, row(conn), self._spec(conn), 9.85) == []

    def test_another_symbol_is_refused(self, conn):
        trigger_plan(conn)
        refusals = tb.check_order_guardrails(conn, row(conn), self._spec(conn, symbol="NLY"), 9.85)
        assert any("not this plan's symbol" in r for r in refusals)

    def test_more_shares_than_the_plan_allows_is_refused(self, conn):
        trigger_plan(conn)
        refusals = tb.check_order_guardrails(conn, row(conn), self._spec(conn, quantity=400), 9.85)
        assert any("exceeds the plan's" in r for r in refusals)

    def test_a_notional_over_the_cap_is_refused(self, conn):
        trigger_plan(conn)
        # Creation already refuses an oversized plan, so tighten the cap afterwards -
        # the same thing a user does when they decide to risk less.
        refusals = tb.check_order_guardrails(conn, row(conn, max_notional=1500), self._spec(conn), 9.85)
        assert any("exceeds the cap" in r for r in refusals)

    def test_a_price_under_the_floor_is_refused(self, conn):
        trigger_plan(conn)
        refusals = tb.check_order_guardrails(conn, row(conn), self._spec(conn), 9.10)
        assert any("below the floor" in r for r in refusals)

    def test_a_limit_outside_the_band_is_refused(self, conn):
        trigger_plan(conn)
        refusals = tb.check_order_guardrails(conn, row(conn), self._spec(conn, limit_price=10.50), 9.85)
        assert any("band is 0.5%" in r for r in refusals)

    def test_the_daily_cap_is_counted_from_the_ledger(self, conn):
        trigger_plan(conn, max_orders_per_day=1)
        tb.cmd_decide(args(action="buy", rationale="ok", evidence_tick_ids="3"), conn)
        tb.cmd_order_spec(args(price=9.85), conn)
        refusals = tb.check_order_guardrails(conn, row(conn), self._spec(conn), 9.85)
        assert any("cap is 1" in r for r in refusals)

    def test_the_lifetime_cap_is_counted_too(self, conn):
        trigger_plan(conn, max_orders_total=1, max_orders_per_day=9)
        tb.cmd_decide(args(action="buy", rationale="ok", evidence_tick_ids="3"), conn)
        tb.cmd_order_spec(args(price=9.85), conn)
        refusals = tb.check_order_guardrails(conn, row(conn), self._spec(conn), 9.85)
        assert any("on this plan, cap is 1" in r for r in refusals)

    def test_the_cooldown_blocks_a_second_order(self, conn):
        trigger_plan(conn, cooldown_seconds=600)
        tb.cmd_decide(args(action="buy", rationale="ok", evidence_tick_ids="3"), conn)
        tb.cmd_order_spec(args(price=9.85), conn)
        refusals = tb.check_order_guardrails(conn, row(conn), self._spec(conn), 9.85)
        assert any("cooldown" in r for r in refusals)

    def test_an_identical_order_at_the_same_state_is_refused(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="buy", rationale="ok", evidence_tick_ids="3"), conn)
        plan_before = row(conn)
        tb.cmd_order_spec(args(price=9.85), conn)
        refusals = tb.check_order_guardrails(conn, plan_before, tb.build_spec(plan_before, 9.85), 9.85)
        assert any("identical order already exists" in r for r in refusals)

    def test_every_broken_guard_is_reported_at_once(self, conn):
        trigger_plan(conn)
        spec = self._spec(conn, symbol="NLY", quantity=9999)
        refusals = tb.check_order_guardrails(conn, row(conn, max_notional=100), spec, 9.85)
        assert len(refusals) >= 3


class TestIdempotencyKey:
    def test_the_same_setup_hashes_the_same_key(self, conn):
        trigger_plan(conn)
        plan = row(conn)
        spec = tb.build_spec(plan, 9.85)
        assert tb.idempotency_key(plan, spec) == tb.idempotency_key(plan, spec)

    def test_a_new_state_gives_a_new_key(self, conn):
        trigger_plan(conn)
        plan = row(conn)
        spec = tb.build_spec(plan, 9.85)
        first = tb.idempotency_key(plan, spec)
        assert tb.idempotency_key(row(conn, state_seq=plan["state_seq"] + 1), spec) != first

    def test_a_different_price_gives_a_different_key(self, conn):
        trigger_plan(conn)
        plan = row(conn)
        assert tb.idempotency_key(plan, tb.build_spec(plan, 9.85)) != tb.idempotency_key(
            plan, tb.build_spec(plan, 9.60)
        )


class TestOrderSpec:
    def test_a_buy_decision_produces_a_spec_awaiting_approval(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="1,2,3"), conn)
        out = tb.cmd_order_spec(args(price=9.85), conn)
        assert out["state"] == tb.PENDING_APPROVAL
        assert out["order_spec"]["side"] == "buy"
        assert out["order_spec"]["quantity"] == 200.0
        assert len(out["order_spec"]["idempotency_key"]) == 32

    def test_the_limit_sits_just_above_the_reference_price(self, conn):
        trigger_plan(conn)
        assert tb.build_spec(row(conn), 9.85)["limit_price"] == 9.86

    def test_a_spec_without_a_buy_decision_is_refused(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="No unconsumed buy decision"):
            tb.cmd_order_spec(args(price=9.85), conn)

    def test_a_spec_from_the_wrong_state_is_refused(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        with pytest.raises(SystemExit, match="An order spec comes from a TRIGGERED plan"):
            tb.cmd_order_spec(args(price=9.85), conn)

    def test_a_decision_made_before_the_setup_moved_is_refused(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="3"), conn)
        row(conn, state_seq=row(conn)["state_seq"] + 1)
        with pytest.raises(SystemExit, match="The setup moved; decide again"):
            tb.cmd_order_spec(args(price=9.85), conn)

    def test_the_decision_is_consumed_so_it_cannot_be_reused(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="3"), conn)
        tb.cmd_order_spec(args(price=9.85), conn)
        assert conn.execute("SELECT consumed FROM decisions WHERE id=1").fetchone()[0] == 1

    def test_a_guardrail_refusal_says_not_to_work_around_it(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="3"), conn)
        row(conn, max_notional=500)
        with pytest.raises(SystemExit, match="do not work around it"):
            tb.cmd_order_spec(args(price=9.85), conn)

    def test_an_autonomous_plan_skips_the_approval_state(self, conn):
        trigger_plan(conn, autonomous=True)
        tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="3"), conn)
        assert tb.cmd_order_spec(args(price=9.85), conn)["state"] == tb.ORDERED


class TestApprovalFlow:
    def _to_pending(self, conn, **overrides):
        trigger_plan(conn, **overrides)
        tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="3"), conn)
        return tb.cmd_order_spec(args(price=9.85), conn)

    def test_approve_releases_the_order(self, conn):
        self._to_pending(conn)
        assert tb.cmd_approve(args(), conn)["state"] == tb.ORDERED

    def test_approving_nothing_is_refused(self, conn):
        trigger_plan(conn)
        with pytest.raises(SystemExit, match="nothing awaiting approval"):
            tb.cmd_approve(args(), conn)

    def test_reject_cancels_the_plan_by_default(self, conn):
        self._to_pending(conn)
        assert tb.cmd_reject(args(), conn)["state"] == tb.CANCELLED

    def test_reject_with_resume_goes_back_to_trailing(self, conn):
        self._to_pending(conn)
        assert tb.cmd_reject(args(resume=True), conn)["state"] == tb.TRAILING

    def test_a_rejected_order_stops_counting_against_the_caps(self, conn):
        self._to_pending(conn, max_orders_total=1)
        tb.cmd_reject(args(resume=True), conn)
        total = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE plan_id=1 AND stage!='rejected'"
        ).fetchone()[0]
        assert total == 0

    def test_recording_a_fill_closes_the_plan(self, conn):
        self._to_pending(conn)
        tb.cmd_approve(args(), conn)
        out = tb.cmd_record_order(args(stage="filled", fill_price=9.86), conn)
        assert out["state"] == tb.FILLED

    def test_a_fill_without_a_price_is_refused(self, conn):
        self._to_pending(conn)
        tb.cmd_approve(args(), conn)
        with pytest.raises(SystemExit, match="needs --fill-price"):
            tb.cmd_record_order(args(stage="filled"), conn)

    def test_a_broker_rejection_returns_the_plan_to_trailing(self, conn):
        self._to_pending(conn)
        tb.cmd_approve(args(), conn)
        out = tb.cmd_record_order(args(stage="rejected", payload='{"error":"no"}'), conn)
        assert out["state"] == tb.TRAILING

    def test_a_preview_stage_leaves_the_state_alone(self, conn):
        self._to_pending(conn)
        tb.cmd_approve(args(), conn)
        assert tb.cmd_record_order(args(stage="preview", payload="{}"), conn)["state"] == tb.ORDERED


class TestKillSwitch:
    def test_halt_writes_the_file(self, conn):
        out = tb.cmd_halt(args(reason="stepping away"), conn)
        assert pathlib.Path(out["file"]).exists()
        assert tb.halted() is True

    def test_a_tick_refuses_while_halted(self, conn):
        make_plan(conn)
        tb.cmd_halt(args(), conn)
        with pytest.raises(SystemExit, match="Kill switch is set"):
            tick(conn, 9.94)

    def test_a_decision_refuses_while_halted(self, conn):
        trigger_plan(conn)
        tb.cmd_halt(args(), conn)
        with pytest.raises(SystemExit, match="Kill switch is set"):
            tb.cmd_decide(args(action="buy", rationale="x", evidence_tick_ids="3"), conn)

    def test_a_spec_refuses_while_halted(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="buy", rationale="x", evidence_tick_ids="3"), conn)
        tb.cmd_halt(args(), conn)
        with pytest.raises(SystemExit, match="Kill switch is set"):
            tb.cmd_order_spec(args(price=9.85), conn)

    def test_resume_clears_it(self, conn):
        tb.cmd_halt(args(), conn)
        assert tb.cmd_resume(args(plan=None), conn)["kill_switch_cleared"] is True
        assert tb.halted() is False

    def test_resume_returns_an_escalated_plan_to_trailing(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="escalate", rationale="odd", evidence_tick_ids="3"), conn)
        tb.cmd_resume(args(), conn)
        assert row(conn)["state"] == tb.TRAILING

    def test_resume_will_not_clear_a_floor_breach(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        tick(conn, 9.10)
        with pytest.raises(SystemExit, match="does not clear with `resume`"):
            tb.cmd_resume(args(), conn)

    def test_rearm_brings_a_halted_plan_back(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        tick(conn, 9.10)
        out = tb.cmd_plan_rearm(args(floor=8.50), conn)
        assert out["state"] == tb.ARMED
        assert out["floor_price"] == 8.50
        assert out["running_low"] is None

    def test_rearm_refuses_a_running_plan(self, conn):
        make_plan(conn)
        with pytest.raises(SystemExit, match="already running"):
            tb.cmd_plan_rearm(args(), conn)


class TestContext:
    def test_fetched_text_is_stored_with_its_source(self, conn):
        make_plan(conn)
        out = tb.cmd_context(args(source="headline:reuters", body="Rates rally"), conn)
        assert out["source"] == "headline:reuters"
        assert conn.execute("SELECT body FROM context WHERE id=1").fetchone()[0] == "Rates rally"

    def test_empty_context_is_refused(self, conn):
        make_plan(conn)
        with pytest.raises(SystemExit, match="Nothing to store"):
            tb.cmd_context(args(source="x", body="   "), conn)


class TestReport:
    def _filled_plan(self, conn):
        trigger_plan(conn)
        tb.cmd_decide(args(action="buy", rationale="clean", evidence_tick_ids="1,2,3"), conn)
        tb.cmd_order_spec(args(price=9.85), conn)
        tb.cmd_approve(args(), conn)
        tb.cmd_record_order(args(stage="filled", fill_price=9.86), conn)

    def test_the_static_limit_fills_at_the_arm_price_on_the_first_tick_through(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        ticks = conn.execute("SELECT * FROM ticks WHERE plan_id=1 ORDER BY id").fetchall()
        base = tb.baseline_entry(row(conn), ticks)
        assert base["filled"] is True
        assert base["price"] == 9.98  # the resting limit, not the 9.94 print

    def test_a_limit_price_never_reached_never_fills(self, conn):
        make_plan(conn)
        tick(conn, 10.50)
        ticks = conn.execute("SELECT * FROM ticks WHERE plan_id=1 ORDER BY id").fetchall()
        assert tb.baseline_entry(row(conn), ticks)["filled"] is False

    def test_the_edge_is_the_static_limit_less_the_fill(self, conn):
        self._filled_plan(conn)
        ticks = conn.execute("SELECT * FROM ticks WHERE plan_id=1 ORDER BY id").fetchall()
        orders = conn.execute("SELECT * FROM orders WHERE plan_id=1 ORDER BY id").fetchall()
        out = tb.compare(row(conn), ticks, orders)
        assert out["edge_per_share"] == pytest.approx(0.12, abs=1e-4)  # 9.98 - 9.86
        assert out["total_edge"] == pytest.approx(24.0, abs=1e-2)      # x 200 shares

    def test_capture_is_the_share_of_the_drop_the_trail_caught(self, conn):
        self._filled_plan(conn)
        ticks = conn.execute("SELECT * FROM ticks WHERE plan_id=1 ORDER BY id").fetchall()
        orders = conn.execute("SELECT * FROM orders WHERE plan_id=1 ORDER BY id").fetchall()
        # 0.12 captured of the 0.27 between the 9.98 limit and the 9.71 low.
        assert tb.compare(row(conn), ticks, orders)["capture_pct"] == pytest.approx(44.4, abs=0.2)

    def test_no_entry_means_no_edge_rather_than_a_zero(self, conn):
        make_plan(conn)
        tick(conn, 9.94)
        ticks = conn.execute("SELECT * FROM ticks WHERE plan_id=1 ORDER BY id").fetchall()
        out = tb.compare(row(conn), ticks, [])
        assert out["edge_per_share"] is None
        assert out["total_edge"] is None

    def test_it_writes_a_self_contained_html_file(self, conn, tmp_path):
        self._filled_plan(conn)
        target = tmp_path / "report.html"
        out = tb.cmd_report(args(out=str(target)), conn)
        body = target.read_text(encoding="utf-8")
        assert out["plans"] == 1
        assert body.startswith("<!doctype html>")
        assert "AGNC" in body
        assert "<script" not in body  # nothing to execute, nothing to fetch

    def test_the_tick_table_carries_every_number_the_chart_plots(self, conn, tmp_path):
        self._filled_plan(conn)
        target = tmp_path / "report.html"
        tb.cmd_report(args(out=str(target)), conn)
        body = target.read_text(encoding="utf-8")
        assert body.count("TRAILING -&gt; TRIGGERED") == 1
        assert "9.71" in body

    def test_a_rationale_cannot_inject_markup_into_the_report(self, conn, tmp_path):
        trigger_plan(conn)
        tb.cmd_decide(
            args(action="hold", rationale="<script>alert(1)</script>", evidence_tick_ids="3"), conn
        )
        target = tmp_path / "report.html"
        tb.cmd_report(args(out=str(target)), conn)
        assert "<script>alert(1)</script>" not in target.read_text(encoding="utf-8")

    def test_reporting_with_no_plans_is_refused(self, conn, tmp_path):
        with pytest.raises(SystemExit, match="No plans to report on"):
            tb.cmd_report(args(plan=None, out=str(tmp_path / "x.html")), conn)


class TestPlanSelection:
    def test_a_single_active_plan_needs_no_flag(self, conn):
        make_plan(conn)
        assert tb._load_plan(conn, None)["id"] == 1

    def test_several_active_plans_demand_one_be_named(self, conn):
        make_plan(conn)
        make_plan(conn, symbol="NLY")
        with pytest.raises(SystemExit, match="say which with --plan"):
            tb._load_plan(conn, None)

    def test_a_finished_plan_does_not_count_as_active(self, conn):
        make_plan(conn)
        make_plan(conn, symbol="NLY")
        tb.cmd_plan_cancel(args(plan=2), conn)
        assert tb._load_plan(conn, None)["id"] == 1

    def test_an_unknown_id_is_refused(self, conn):
        with pytest.raises(SystemExit, match="No plan with id 7"):
            tb._load_plan(conn, 7)

    def test_list_hides_finished_plans_unless_asked(self, conn):
        make_plan(conn)
        tb.cmd_plan_cancel(args(), conn)
        assert tb.cmd_plan_list(args(), conn)["count"] == 0
        assert tb.cmd_plan_list(args(all=True), conn)["count"] == 1


class TestDatabaseLocation:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRAILING_BUY_DB", str(tmp_path / "custom.db"))
        assert tb.db_path() == tmp_path / "custom.db"

    def test_falls_back_to_hermes_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TRAILING_BUY_DB", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert tb.db_path() == tmp_path / "trailing_buy.db"

    def test_windows_default_is_local_appdata_not_dot_hermes(self, monkeypatch):
        """Hermes stores its data under %LOCALAPPDATA%\\hermes on Windows."""
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(tb.sys, "platform", "win32")
        local_appdata = r"C:\Users\Mako\AppData\Local"
        monkeypatch.setenv("LOCALAPPDATA", local_appdata)
        assert tb.hermes_home() == pathlib.Path(local_appdata) / "hermes"

    def test_posix_default_is_dot_hermes(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(tb.sys, "platform", "linux")
        assert tb.hermes_home().name == ".hermes"

    def test_blank_env_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "   ")
        monkeypatch.setattr(tb.sys, "platform", "linux")
        assert tb.hermes_home().name == ".hermes"

    def test_the_kill_switch_lives_beside_the_database(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TRAILING_BUY_HALT_FILE", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert tb.halt_path() == tmp_path / "trailing_buy.HALT"
