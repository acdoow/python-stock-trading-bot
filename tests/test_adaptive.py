import copy
import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from adaptive import (APIError, BasketBot, Plateau, Rules, TradingAPI, account_lock_path,
                      learning_report, make_demo_episodes, plateau_target,
                      price_up, replay_episode)
from bot import InstanceLock


class FakeBroker:
    def __init__(self, *, mode="paper", execute=True):
        self.mode, self.execute = mode, execute
        self.now = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
        self.account = {"id": "test-account", "status": "ACTIVE", "equity": "100", "cash": "100"}
        self.holdings, self.orders, self.sent, self.canceled = {}, {}, [], []
        self.price, self.is_open, self.fail_post, self.cancel_immediately = 100.0, True, False, True
        self.history = []

    def call(self, path, *, params=None, payload=None, method=None, **kwargs):
        if payload is not None:
            if not self.execute:
                raise AssertionError("A mutation reached preview")
            self.sent.append(copy.deepcopy(payload))
            if self.fail_post:
                raise APIError(None, "Simulated timeout")
            order = {**payload, "id": f"order-{len(self.sent)}", "status": "new", "filled_qty": "0", "filled_avg_price": None}
            self.orders[payload["client_order_id"]] = order
            if payload["side"] == "buy":
                self.fill(order, float(payload["notional"]) / self.price, self.price)
            return copy.deepcopy(order)
        if method == "DELETE":
            if not self.execute:
                raise AssertionError("A cancellation reached preview")
            self.canceled.append(path)
            for order in self.orders.values():
                if path.endswith(order["id"]) and self.cancel_immediately:
                    order["status"] = "canceled"
            return None
        if path == "/v2/account":
            return copy.deepcopy(self.account)
        if path == "/v2/clock":
            return {"is_open": self.is_open, "timestamp": self.now.isoformat(), "next_open": "2026-10-06T09:30:00-04:00"}
        if path == "/v2/positions":
            return [{"symbol": symbol, "qty": str(qty), "avg_entry_price": "100", "market_value": str(qty * self.price)}
                    for symbol, qty in self.holdings.items() if qty > 1e-10]
        if path == "/v2/orders":
            return [copy.deepcopy(o) for o in self.orders.values() if o["status"] not in ("filled", "expired", "canceled", "rejected")]
        if path == "/v2/orders:by_client_order_id":
            key = params["client_order_id"]
            if key not in self.orders:
                raise APIError(404, "Not found")
            return copy.deepcopy(self.orders[key])
        if path.startswith("/v2/assets/"):
            return {"status": "active", "tradable": True, "fractionable": True}
        if path == "/v2/calendar":
            first = datetime.fromisoformat(params["start"])
            return [{"date": (first + timedelta(days=i)).date().isoformat()} for i in range(15)
                    if (first + timedelta(days=i)).weekday() < 5]
        raise AssertionError(path)

    def fill(self, order, qty, price, *, terminal=True):
        previous_qty = float(order.get("filled_qty") or 0)
        previous_value = previous_qty * float(order.get("filled_avg_price") or 0)
        new_qty = previous_qty + qty
        order.update(status="filled" if terminal else "partially_filled", filled_qty=str(new_qty),
                     filled_avg_price=str((previous_value + qty * price) / new_qty), filled_at=self.now.isoformat())
        sign = 1 if order["side"] == "buy" else -1
        self.holdings[order["symbol"]] = self.holdings.get(order["symbol"], 0) + sign * qty
        self.account["cash"] = str(float(self.account["cash"]) - sign * qty * price)

    def quote(self, symbol, now, rules):
        return {"bid": self.price, "ask": self.price + 0.01, "t": now.isoformat()}

    def bars(self, symbol, start, now, rules):
        return [copy.deepcopy(x) for x in self.history if datetime.fromisoformat(x["t"]) >= start]


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.rules = Rules(universe=("AAPL",), positions=1, dollars_each=10, capital_cap=10)
        self.api = FakeBroker()
        self.messages = []
        self.bot = BasketBot(self.api, self.rules, self.folder, emit=self.messages.append)

    def tearDown(self):
        self.temp.cleanup()

    def entered(self):
        self.bot.cycle()
        self.bot.cycle()
        self.assertEqual(len(self.api.sent), 2)  # Entry, then a protective stop.
        return self.bot.state["trades"]["AAPL"]

    def plateau_history(self, trade, level=110):
        start = self.api.now - timedelta(minutes=60)
        trade["opened_at"] = (start - timedelta(minutes=1)).isoformat()
        self.api.history = [{"t": (start + timedelta(minutes=5*i)).isoformat(),
                             "o": level, "h": level + 0.12, "l": level - 0.12,
                             "c": level + (0.1 if i % 2 == 0 else -0.1)} for i in range(12)]
        self.api.price = level
        trade.pop("observed_bucket", None)

    def test_fluctuation_average_is_detected_near_observed_high(self):
        window = [110.1, 109.9] * 6
        target = plateau_target(window, 110.2, 100, 0.1, self.rules)
        self.assertAlmostEqual(target, 110)

    def test_continuous_rise_is_not_called_a_plateau(self):
        self.assertIsNone(plateau_target([105 + i for i in range(12)], 116, 100, 0.1, self.rules))

    def test_fluctuations_far_below_the_peak_are_not_a_top(self):
        self.assertIsNone(plateau_target([105.1, 104.9] * 6, 120, 100, 0.1, self.rules))

    def test_flat_or_unprofitable_observations_do_not_trigger_profit_order(self):
        for values in ([110] * 12, [100.1, 99.9] * 6):
            self.assertIsNone(plateau_target(values, max(values), 100, 0.1, self.rules))

    def test_estimated_costs_can_eliminate_a_small_gain(self):
        expensive = replace(self.rules, estimated_roundtrip_cost=2)
        self.assertIsNone(plateau_target([110.1, 109.9] * 6, 110.2, 100, 0.1, expensive))

    def test_five_independent_buys_fit_fifty_dollars(self):
        rules = Rules()
        bot = BasketBot(self.api, rules, self.folder, emit=lambda _: None)
        bot.cycle()
        self.assertEqual(len(self.api.sent), 5)
        self.assertLessEqual(sum(float(x["notional"]) for x in self.api.sent), 50)
        self.assertEqual(len(bot.state["trades"]), 5)

    def test_stop_uses_half_of_actual_entry_price(self):
        self.entered()
        stop = self.api.sent[1]
        self.assertEqual(stop["type"], "stop")
        self.assertEqual(stop["stop_price"], "50.00")
        self.assertEqual(float(stop["qty"]), 0.1)
        self.assertEqual(stop["time_in_force"], "day")

    def test_a_loss_sale_uses_only_that_stock(self):
        rules = Rules()
        bot = BasketBot(self.api, rules, self.folder, emit=lambda _: None)
        bot.cycle()
        bot.reconcile()
        self.api.price = 49
        bot.manage("AAPL", {"symbol": "AAPL", "qty": "0.1"}, self.api.now)
        self.assertEqual(self.api.sent[-1]["symbol"], "AAPL")
        self.assertEqual(self.api.sent[-1]["side"], "sell")
        self.assertEqual(self.api.sent[-1]["type"], "market")

    def test_profit_target_waits_until_stop_cancellation_is_confirmed(self):
        trade = self.entered()
        self.plateau_history(trade)
        self.api.cancel_immediately = False
        self.bot.cycle()
        self.assertEqual(len(self.api.sent), 2)
        self.assertEqual(len(self.api.canceled), 1)
        self.bot.cycle()
        self.assertEqual(len(self.api.sent), 2)
        stop = next(o for o in self.api.orders.values() if o["type"] == "stop")
        stop["status"] = "canceled"
        self.bot.cycle()
        self.assertEqual(self.api.sent[-1]["type"], "limit")
        self.assertEqual(self.api.sent[-1]["limit_price"], "110.00")

    def test_pending_profit_order_does_not_count_as_realized_profit(self):
        trade = self.entered()
        self.plateau_history(trade)
        self.bot.cycle()
        self.bot.cycle()
        self.assertEqual(self.api.sent[-1]["type"], "limit")
        self.assertEqual(self.bot.state["closed"], [])
        self.assertEqual(trade["proceeds"], 0)

    def test_partial_sell_and_cancel_recheck_the_remaining_quantity(self):
        trade = self.entered()
        self.plateau_history(trade)
        self.bot.cycle()
        self.bot.cycle()
        profit = next(o for o in self.api.orders.values() if o["type"] == "limit")
        self.api.fill(profit, 0.04, 110, terminal=False)
        self.api.price = 49
        self.bot.cycle()  # Reconcile partial fill; request cancellation.
        self.bot.cycle()  # Confirm cancellation; sell only 0.06 shares.
        self.assertEqual(self.api.sent[-1]["type"], "market")
        self.assertAlmostEqual(float(self.api.sent[-1]["qty"]), 0.06, places=8)
        self.assertEqual(len(self.bot.state["closed"]), 0)

    def test_completed_sell_records_gross_and_estimated_net_once(self):
        self.entered()
        stop = next(o for o in self.api.orders.values() if o["type"] == "stop")
        self.api.fill(stop, 0.1, 49)
        self.bot.cycle()
        self.bot.cycle()
        self.assertEqual(len(self.bot.state["closed"]), 1)
        self.assertAlmostEqual(self.bot.state["closed"][0]["realized_gross_pnl"], -5.1)
        self.assertEqual(self.bot.state["settlement_until"], "2026-10-06")
        self.assertEqual(len(self.api.sent), 2)  # No same-day replacement purchase.

    def test_fractional_stop_is_renewed_after_day_expiry(self):
        self.entered()
        stop = next(o for o in self.api.orders.values() if o["type"] == "stop")
        stop["status"] = "expired"
        self.api.now += timedelta(days=1)
        self.bot.cycle()
        self.assertEqual(self.api.sent[-1]["type"], "stop")
        self.assertEqual(len(self.api.sent), 3)

    def test_preview_makes_no_orders_or_cancellations(self):
        api = FakeBroker(mode="live", execute=False)
        bot = BasketBot(api, Rules(), self.folder, emit=self.messages.append)
        bot.cycle()
        self.assertEqual(api.sent, [])
        self.assertEqual(api.canceled, [])
        self.assertEqual(bot.state["trades"], {})
        self.assertEqual(sum("PREVIEW buy" in x for x in self.messages), 5)

    def test_live_execution_needs_matching_account_id(self):
        api = FakeBroker(mode="live")
        with self.assertRaises(ValueError):
            BasketBot(api, self.rules, self.folder)
        bot = BasketBot(api, self.rules, self.folder, account_id="wrong-id", emit=lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            bot.cycle()
        self.assertEqual(api.sent, [])

    def test_uncertain_submission_is_not_blindly_repeated(self):
        self.api.fail_post = True
        with self.assertRaises(APIError):
            self.bot.cycle()
        restarted = BasketBot(self.api, self.rules, self.folder, emit=lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            restarted.cycle()
        self.assertEqual(len(self.api.sent), 1)

    def test_missing_average_fill_price_cannot_create_zero_cost_holdings(self):
        self.bot.cycle()
        order = next(iter(self.api.orders.values()))
        order["filled_avg_price"] = None
        with self.assertRaisesRegex(ValueError, "fill price"):
            self.bot.reconcile()
        self.assertEqual(self.bot.state["trades"]["AAPL"]["entry_qty"], 0)
        self.assertEqual(len(self.api.sent), 1)

    def test_broker_adapter_blocks_mutations_in_live_preview(self):
        api = TradingAPI("dummy", "dummy", mode="live")
        api.opener.open = MagicMock()
        for kwargs in ({"payload": {"side": "buy"}}, {"method": "DELETE"}):
            with self.assertRaises(RuntimeError):
                api.call("/v2/orders", **kwargs)
        api.opener.open.assert_not_called()

    def test_mode_selects_the_correct_fixed_endpoint(self):
        api = TradingAPI("dummy", "dummy", mode="live", execute=True)
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{}')
        api.opener.open = MagicMock(return_value=response)
        api.call("/v2/orders", payload={"side": "buy"})
        self.assertEqual(api.opener.open.call_args.args[0].full_url, "https://api.alpaca.markets/v2/orders")

    def test_stop_file_prevents_all_submissions(self):
        (self.folder / "STOP_BOT").touch()
        self.bot.cycle()
        self.assertEqual(self.api.sent, [])

    def test_market_closure_prevents_entry(self):
        self.api.is_open = False
        self.bot.cycle()
        self.assertEqual(self.api.sent, [])

    def test_account_lock_blocks_another_results_folder_before_orders(self):
        other = BasketBot(self.api, self.rules, self.folder / "other", emit=lambda _: None)
        with InstanceLock(account_lock_path("paper", "test-account")):
            with self.assertRaises(RuntimeError):
                other.run(once=True)
        self.assertEqual(self.api.sent, [])

    def test_live_learning_needs_its_separate_local_switch_and_keeps_risk_fixed(self):
        episodes = make_demo_episodes(Rules(), 30)
        eligible = {"status": "evaluated", "eligible_for_paper_update": True,
                    "challenger": asdict(Plateau(window_bars=6)), "live_parameters_changed": False}
        for enabled in (False, True):
            rules = replace(Rules(), auto_adapt_live=enabled)
            api = FakeBroker(mode="live")
            bot = BasketBot(api, rules, self.folder / str(enabled), account_id="test-account", emit=lambda _: None)
            bot.state["closed"] = copy.deepcopy(episodes)
            before = (rules.dollars_each, rules.capital_cap, rules.loss_fraction)
            with patch("adaptive.learning_report", return_value=copy.deepcopy(eligible)):
                bot.update_research(api.now)
            self.assertEqual(bot.state["active_profile"]["window_bars"], 6 if enabled else 12)
            self.assertEqual((rules.dollars_each, rules.capital_cap, rules.loss_fraction), before)
            self.assertEqual(api.sent, [])

    def test_training_horizon_is_purged_if_its_final_bar_extends_into_validation(self):
        episodes = make_demo_episodes(self.rules, 30)
        boundary = datetime.fromisoformat(episodes[20]["opened_at"])
        for episode in episodes[:20]:
            episode["trace"][-1]["t"] = (boundary - timedelta(minutes=1)).isoformat()
        self.assertEqual(learning_report(episodes, self.rules)["status"], "collecting_nonoverlapping_data")

    def test_stale_quotes_and_wide_spreads_are_rejected(self):
        api = TradingAPI("dummy", "dummy")
        for age, bid, ask in ((61, 100, 100.01), (0, 100, 103), (0, 100, 99)):
            api.call = lambda *a, **k: {"quote": {"t": (self.api.now - timedelta(seconds=age)).isoformat(), "bp": bid, "ap": ask}}
            with self.assertRaises(ValueError):
                api.quote("AAPL", self.api.now, self.rules)

    def test_only_completed_regular_session_bars_are_used(self):
        api = TradingAPI("dummy", "dummy")
        times = ("2026-10-05T13:00:00+00:00", "2026-10-05T14:50:00+00:00", "2026-10-05T14:58:00+00:00")
        api.call = lambda *a, **k: {"bars": {"AAPL": [{"t": t, "o": 100, "h": 101, "l": 99, "c": 100} for t in times]}}
        bars = api.bars("AAPL", self.api.now - timedelta(hours=3), self.api.now, self.rules)
        self.assertEqual([x["t"] for x in bars], [times[1]])

    def test_a_session_gap_resets_the_plateau_window(self):
        trade = self.entered()
        trade["window"] = [110.1, 109.9] * 5
        trade["last_bar"] = (self.api.now - timedelta(days=1)).isoformat()
        self.api.history = [{"t": (self.api.now - timedelta(minutes=5)).isoformat(), "o": 110, "h": 111, "l": 109, "c": 110}]
        self.bot.observe(trade, self.api.now)
        self.assertEqual(trade["window"], [110])

    def test_no_learning_update_from_one_win_or_loss(self):
        episodes = make_demo_episodes(self.rules, count=5)
        report = learning_report(episodes, self.rules)
        self.assertEqual(report["status"], "collecting")
        self.assertFalse(report["eligible_for_paper_update"])

    def test_learning_uses_a_later_block_and_never_changes_capital_or_live_settings(self):
        rules = Rules()
        before = asdict(rules)
        report = learning_report(make_demo_episodes(rules), rules)
        self.assertEqual(report["status"], "evaluated")
        self.assertEqual(report["validation_episodes"], 10)
        self.assertFalse(report["live_parameters_changed"])
        self.assertFalse(report["capital_and_loss_limits_changed"])
        self.assertEqual(asdict(rules), before)

    def test_overlapping_train_and_validation_horizons_are_purged(self):
        episodes = make_demo_episodes(self.rules, 30)
        for episode in episodes:
            episode["opened_at"] = episodes[0]["opened_at"]
        report = learning_report(episodes, self.rules)
        self.assertEqual(report["status"], "collecting_nonoverlapping_data")

    def test_target_cannot_fill_using_the_bar_that_created_the_signal(self):
        rules = replace(self.rules, plateau=Plateau(window_bars=6), research_horizon_bars=60)
        start = self.api.now
        prices = [110.1, 109.9] * 3 + [100]
        episode = {"entry_cost": 10, "entry_qty": 0.1,
                   "trace": [{"t": (start + timedelta(minutes=5*i)).isoformat(),
                              "o": c, "h": c, "l": c, "c": c} for i, c in enumerate(prices)]}
        result = replay_episode(episode, rules, rules.plateau)
        self.assertLess(result, 0)  # Signal on sixth bar; seventh never touches its target.

    def test_demo_contains_a_gap_loss_and_a_plateau_before_another_rise(self):
        episodes = make_demo_episodes(self.rules, 5)
        self.assertLess(replay_episode(episodes[1], self.rules, self.rules.plateau), -0.5)
        early_sale = replay_episode(episodes[3], self.rules, self.rules.plateau)
        later_value = episodes[3]["trace"][-1]["c"] / 100 - 1
        self.assertLess(early_sale, later_value)

    def test_subpenny_target_rounding_does_not_lower_the_asked_price(self):
        self.assertEqual(price_up(100.001), "100.01")
        self.assertEqual(price_up(0.50001), "0.5001")


if __name__ == "__main__":
    unittest.main()
