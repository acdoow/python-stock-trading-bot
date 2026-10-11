"""Price-path and order behavior checks for momentum entries, all offline."""
import copy
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from adaptive import BasketBot, Rules, load_rules
from agent import AgentSettings, EntryPlan, TradingAgent, offline_agent_demo
from momentum import Momentum, momentum_signal
from test_adaptive import FakeBroker


OPEN = datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc)


def history(closes, *, opening=100.0, start=OPEN):
    result = []
    previous = opening
    for i, close in enumerate(closes):
        result.append({"t": (start + timedelta(minutes=5 * i)).isoformat(),
                       "o": previous, "c": close, "h": max(previous, close) + 0.01,
                       "l": min(previous, close) - 0.01})
        previous = close
    return result


class MomentumPaths(unittest.TestCase):
    def setUp(self):
        self.settings = Momentum(enabled=True)
        self.now = OPEN + timedelta(minutes=30)
        self.rising = history([100.1, 100.2, 100.35, 100.5, 100.65, 100.8])

    def signal(self, bars=None, *, bid=100.8, now=None):
        return momentum_signal(self.rising if bars is None else bars, bid, now or self.now,
                               OPEN, 5, self.settings)

    def test_sustained_rise_qualifies(self):
        self.assertTrue(self.signal()["eligible"])

    def test_flat_or_falling_session_waits(self):
        for closes in ([100] * 6, [99.9, 99.8, 99.7, 99.6, 99.4, 99.2]):
            with self.subTest(closes=closes):
                self.assertFalse(self.signal(history(closes), bid=closes[-1])["eligible"])

    def test_a_single_price_jump_is_not_sustained_growth(self):
        signal = self.signal(history([100, 100, 100, 100, 100, 100.8]))
        self.assertFalse(signal["eligible"])
        self.assertIn("steps", signal["reason"])

    def test_some_fluctuations_can_still_qualify(self):
        self.assertTrue(self.signal(history([100.2, 100.1, 100.4, 100.3, 100.7, 100.8]))["eligible"])

    def test_up_for_the_day_but_falling_recently_waits(self):
        closes = [100.5, 101, 101.5, 102, 102.5, 103, 102.9, 102.8, 102.7, 102.6, 102.5, 102.4]
        signal = self.signal(history(closes), bid=102.4, now=OPEN + timedelta(minutes=60))
        self.assertFalse(signal["eligible"])
        self.assertIn("Recent growth", signal["reason"])

    def test_recent_recovery_below_the_session_open_waits(self):
        closes = [104, 103, 102, 101, 100.5, 100, 100.1, 100.2, 100.35, 100.5, 100.65, 100.8]
        signal = self.signal(history(closes, opening=105), now=OPEN + timedelta(minutes=60))
        self.assertFalse(signal["eligible"])
        self.assertIn("Session growth", signal["reason"])

    def test_pullback_after_a_high_spike_waits(self):
        bars = copy.deepcopy(self.rising)
        bars[3]["h"] = 102
        signal = self.signal(bars)
        self.assertFalse(signal["eligible"])
        self.assertIn("pulled back", signal["reason"])

    def test_reversal_in_the_fresh_quote_invalidates_old_growth(self):
        self.assertFalse(self.signal(bid=99.9)["eligible"])

    def test_first_thirty_minutes_must_finish(self):
        signal = self.signal(now=OPEN + timedelta(minutes=29, seconds=59))
        self.assertFalse(signal["eligible"])
        self.assertIn("completed", signal["reason"])

    def test_unfinished_future_bar_cannot_change_the_decision(self):
        future = {"t": self.now.isoformat(), "o": 100, "c": 1, "h": 1000, "l": 1}
        self.assertEqual(self.signal(self.rising + [future]), self.signal())

    def test_previous_session_prices_are_not_used(self):
        yesterday = history([100.1, 100.2, 100.35, 100.5, 100.65, 100.8], start=OPEN - timedelta(days=1))
        self.assertFalse(self.signal(yesterday)["eligible"])

    def test_missing_opening_bar_waits(self):
        self.assertFalse(self.signal(self.rising[1:])["eligible"])

    def test_gap_in_recent_bars_waits(self):
        bars = history([100.1, 100.2, 100.3, 100.4, 100.5, 100.65, 100.8])
        del bars[2]
        signal = self.signal(bars, now=OPEN + timedelta(minutes=35))
        self.assertFalse(signal["eligible"])
        self.assertIn("missing", signal["reason"])

    def test_stale_completed_bars_wait(self):
        signal = self.signal(now=OPEN + timedelta(minutes=50))
        self.assertFalse(signal["eligible"])
        self.assertIn("stale", signal["reason"])

    def test_duplicate_bars_wait(self):
        self.assertFalse(self.signal(self.rising + [self.rising[-1]])["eligible"])

    def test_invalid_prices_are_rejected(self):
        for price in (float("nan"), float("inf"), 0, -1):
            with self.subTest(price=price), self.assertRaises(ValueError):
                self.signal(bid=price)

    def test_invalid_threshold_settings_are_rejected(self):
        for values in ({"window_bars": 2}, {"window_bars": True}, {"enabled": 1},
                       {"min_session_gain_fraction": 0}, {"min_window_gain_fraction": float("nan")},
                       {"min_rising_step_fraction": 1.1}, {"max_pullback_fraction": -0.01}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(self.settings, **values).validate()


class WatchlistBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.now = OPEN + timedelta(minutes=30)
        self.prices = {"AAPL": 100.8, "MSFT": 101.2}
        self.paths = {"AAPL": history([100.1, 100.2, 100.35, 100.5, 100.65, 100.8]),
                      "MSFT": history([100.2, 100.4, 100.6, 100.8, 101, 101.2])}

    def quote(self, symbol, now, rules):
        price = self.prices[symbol]
        return {"bid": price, "ask": price + 0.01, "t": now.isoformat()}

    def bars(self, symbol, start, now, rules):
        return copy.deepcopy(self.paths[symbol])

    def call(self, path, *, payload=None, **kwargs):
        if payload is not None:
            self.price = self.prices[payload["symbol"]]
        if path == "/v2/positions" and payload is None:
            return [{"symbol": symbol, "qty": str(qty), "avg_entry_price": "100",
                     "market_value": str(qty * self.prices[symbol])}
                    for symbol, qty in self.holdings.items() if qty > 1e-10]
        return super().call(path, payload=payload, **kwargs)


class MomentumOrders(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.rules = Rules(universe=("AAPL", "MSFT"), positions=1, dollars_each=2,
                           capital_cap=10, momentum=Momentum(enabled=True))
        self.api = WatchlistBroker()
        self.messages = []
        self.agent = TradingAgent(self.api, self.rules, self.folder,
                                  settings=AgentSettings(), emit=self.messages.append)

    def tearDown(self):
        self.agent.planner.close()
        self.temp.cleanup()

    def test_strongest_recent_growth_wins_one_available_slot(self):
        self.agent.cycle()
        self.assertEqual([order["symbol"] for order in self.api.sent], ["MSFT"])
        self.assertEqual(self.api.sent[0]["notional"], "2.00")

    def test_engine_without_model_also_ranks_growth(self):
        bot = BasketBot(self.api, self.rules, self.folder / "engine", emit=lambda _: None)
        bot.cycle()
        self.assertEqual([order["symbol"] for order in self.api.sent], ["MSFT"])

    def test_no_qualifying_stock_leaves_cash_unused(self):
        for symbol in self.api.prices:
            self.api.prices[symbol] = 99.2
            self.api.paths[symbol] = history([99.9, 99.8, 99.7, 99.6, 99.4, 99.2])
        self.agent.cycle()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.agent.state["trades"], {})
        self.assertTrue(any("WAIT" in message for message in self.messages))

    def test_final_quote_reversal_prevents_a_previously_planned_buy(self):
        good = self.api.quote("AAPL", self.api.now, self.rules)
        bad = {"bid": 99.0, "ask": 99.01, "t": self.api.now.isoformat()}
        self.agent.rules = replace(self.rules, universe=("AAPL",))
        self.agent.limits = self.agent.fixed_limits()
        self.api.quote = MagicMock(side_effect=[good, bad])
        self.agent.cycle()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.agent.state["trades"], {})

    def test_custom_planner_cannot_select_a_stock_that_failed_growth(self):
        self.api.prices["AAPL"] = 99.2
        self.api.paths["AAPL"] = history([99.9, 99.8, 99.7, 99.6, 99.4, 99.2])
        planner = MagicMock(name="planner")
        planner.name, planner.status = "test", "ready"
        planner.choose.return_value = EntryPlan(("AAPL",), "Attempt an excluded stock.")
        self.agent.planner = planner
        with self.assertRaises(ValueError):
            self.agent.cycle()
        self.assertEqual(self.api.sent, [])

    def test_unguarded_growth_buy_is_rejected(self):
        self.agent.state["trades"]["AAPL"] = {"entry_qty": 0.0, "entry_cost": 0.0, "remaining_qty": 0.0}
        self.agent.authorized_entries = {"AAPL"}
        with self.assertRaisesRegex(ValueError, "growth check"):
            self.agent.guard_order("AAPL", "entry", {"side": "buy", "type": "market", "notional": "2"}, self.api.now)
        self.assertEqual(self.api.sent, [])

    def test_growth_parameters_cannot_be_changed_during_a_run(self):
        self.agent.rules.momentum = replace(self.rules.momentum, enabled=False)
        with self.assertRaisesRegex(ValueError, "Risk limits changed"):
            self.agent.cycle()
        self.assertEqual(self.api.sent, [])

    def test_loss_exit_remains_active_when_growth_fails(self):
        self.agent.cycle()
        self.agent.cycle()
        stop = next(order for order in self.api.orders.values() if order["type"] == "stop")
        stop["status"] = "canceled"
        self.api.prices["MSFT"] = 49
        self.agent.cycle()
        self.assertEqual(self.api.sent[-1]["side"], "sell")
        self.assertEqual(self.api.sent[-1]["symbol"], "MSFT")
        self.assertEqual(self.api.sent[-1]["type"], "market")

    def test_waiting_and_passing_reasons_are_saved(self):
        self.api.prices["AAPL"] = 99.2
        self.api.paths["AAPL"] = history([99.9, 99.8, 99.7, 99.6, 99.4, 99.2])
        self.agent.cycle()
        events = [json.loads(line) for line in (self.folder / "agent_decisions.jsonl").read_text().splitlines()]
        screens = {event["symbol"]: event["signal"]["eligible"]
                   for event in events if event["kind"] == "entry_screen"}
        self.assertEqual(screens, {"AAPL": False, "MSFT": True})

    def test_preview_remains_read_only_with_growth_enabled(self):
        self.api.mode, self.api.execute = "live", False
        preview = TradingAgent(self.api, self.rules, self.folder / "preview",
                               settings=AgentSettings(), emit=lambda _: None)
        preview.cycle()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.api.canceled, [])

    def test_opening_time_tracks_winter_exchange_timezone(self):
        winter_open = datetime(2026, 12, 7, 14, 30, tzinfo=timezone.utc)
        self.api.now = winter_open + timedelta(minutes=30)
        for symbol in self.api.paths:
            for bar in self.api.paths[symbol]:
                elapsed = datetime.fromisoformat(bar["t"]) - OPEN
                bar["t"] = (winter_open + elapsed).isoformat()
        self.agent.cycle()
        self.assertEqual([order["symbol"] for order in self.api.sent], ["MSFT"])

    def test_old_configuration_keeps_its_explicit_list_baseline(self):
        path = self.folder / "old_rules.json"
        data = asdict(self.rules)
        del data["momentum"]
        path.write_text(json.dumps(data))
        self.assertFalse(load_rules(path).momentum.enabled)

    def test_growth_demo_runs_with_ten_dollar_cap_and_independent_exits(self):
        rules = replace(Rules(), dollars_each=2, capital_cap=10, momentum=Momentum(enabled=True))
        summary = offline_agent_demo(rules, self.folder / "demo", emit=lambda _: None)
        events = [json.loads(line) for line in Path(summary["decision_history"]).read_text().splitlines()]
        entries = [event for event in events if event["kind"] == "order_intent" and event["purpose"] == "entry"]
        self.assertEqual(len(entries), 5)
        self.assertLessEqual(sum(float(event["payload"]["notional"]) for event in entries), 10)
        self.assertEqual(summary["closed_trades"], 3)
        self.assertEqual(len(summary["open_positions"]), 2)


if __name__ == "__main__":
    unittest.main()
