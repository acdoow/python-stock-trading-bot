import copy
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from adaptive import Rules, atomic_json
from agent import (MAX_REPLY_BYTES, AgentSettings, EntryPlan, LocalModelPlanner,
                   TradingAgent, export_training_examples,
                   local_endpoint, offline_agent_demo, parse_plan)
from test_adaptive import FakeBroker


class FixedPlanner:
    name = "test"
    status = "ready"

    def __init__(self, symbols):
        self.symbols = tuple(symbols)

    def choose(self, observation, now):
        return EntryPlan(self.symbols, "A recorded test choice.")

    def close(self):
        pass


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.rules = Rules(universe=("AAPL",), positions=1, dollars_each=10, capital_cap=10)
        self.api = FakeBroker()
        self.agent = TradingAgent(self.api, self.rules, self.folder,
                                  settings=AgentSettings(), emit=lambda _: None)
        self.observation = {"observed_at": self.api.now.isoformat(), "bar_minutes": 5,
                            "available_slots": 1, "candidates": [{"symbol": "AAPL"}]}

    def tearDown(self):
        self.agent.planner.close()
        self.temp.cleanup()

    def entered(self):
        self.agent.cycle()
        self.agent.cycle()
        return self.agent.state["trades"]["AAPL"]

    def test_rule_agent_buys_and_adds_protection(self):
        self.entered()
        self.assertEqual([o["type"] for o in self.api.sent], ["market", "stop"])
        events = [json.loads(line) for line in (self.folder / "agent_decisions.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["kind"] == "entry_plan" for e in events))
        self.assertTrue(any(e["kind"] == "outcome_review" for e in events))
        submissions = [e for e in events if e["kind"] == "order_submission"]
        self.assertTrue(all(e["realized_profit"] is False for e in submissions))

    def test_wait_plan_sends_no_buy(self):
        self.agent.planner = FixedPlanner(())
        self.agent.cycle()
        self.assertEqual(self.api.sent, [])

    def test_malicious_custom_planner_cannot_choose_new_ticker(self):
        self.agent.planner = FixedPlanner(("SPY",))
        with self.assertRaises(ValueError):
            self.agent.cycle()
        self.assertEqual(self.api.sent, [])

    def test_strict_plan_rejects_execution_and_risk_fields(self):
        invalid = [
            '{"symbols":["AAPL"],"reason":"Buy","notional":100}',
            '{"symbols":["AAPL"],"reason":"Buy","loss_fraction":0.99}',
            '{"symbols":["AAPL"],"reason":"Buy","command":"delete files"}',
            '{"symbols":["AAPL","AAPL"],"reason":"Buy"}',
            '{"symbols":["SPY"],"reason":"Buy"}',
            '{"symbols":[true],"reason":"Buy"}',
            '{"symbols":[],"reason":"First","reason":"Second"}',
            '{"symbols":[],"reason":NaN}',
            '```json\n{"symbols":[],"reason":"Wait"}\n```',
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_plan(text, self.observation)

    def test_empty_plan_is_valid(self):
        plan = parse_plan('{"symbols":[],"reason":"Wait for more observations."}', self.observation)
        self.assertEqual(plan.symbols, ())

    def test_recheck_skips_quote_that_became_stale(self):
        quote = self.api.quote("AAPL", self.api.now, self.rules)
        self.api.quote = MagicMock(side_effect=[quote, ValueError("stale")])
        self.agent.cycle()
        self.assertEqual(self.api.sent, [])

    def test_guard_rejects_oversize_buy(self):
        self.agent.cycle()
        self.agent.state["pending"].clear()
        with self.assertRaises(ValueError):
            self.agent.guard_order("AAPL", "entry", {"side": "buy", "type": "market", "notional": "11"})

    def test_guard_rejects_unplanned_buy(self):
        self.agent.cycle()
        self.agent.state["pending"].clear()
        self.agent.authorized_entries.clear()
        with self.assertRaises(ValueError):
            self.agent.guard_order("AAPL", "entry", {"side": "buy", "type": "market", "notional": "10"})

    def test_guard_rejects_oversell(self):
        self.entered()
        self.agent.state["pending"].clear()
        with self.assertRaises(ValueError):
            self.agent.guard_order("AAPL", "loss", {"side": "sell", "type": "market", "qty": "1"})

    def test_guard_rejects_model_moving_stop(self):
        self.entered()
        self.agent.state["pending"].clear()
        with self.assertRaises(ValueError):
            self.agent.guard_order("AAPL", "protective_stop", {"side": "sell", "type": "stop", "qty": "0.1", "stop_price": "1"})

    def test_guard_rejects_even_one_fractional_tick_above_owned_shares(self):
        self.entered()
        self.agent.state["pending"].clear()
        with self.assertRaises(ValueError):
            self.agent.guard_order("AAPL", "loss", {"side": "sell", "type": "market", "qty": "0.100000001"})

    def test_guard_rejects_unrecorded_profit_target(self):
        self.entered()
        self.agent.state["pending"].clear()
        with self.assertRaises(ValueError):
            self.agent.guard_order("AAPL", "plateau", {"side": "sell", "type": "limit", "qty": "0.1", "limit_price": "110"})

    def test_guard_rejects_conflicting_order(self):
        self.entered()
        with self.assertRaises(ValueError):
            self.agent.guard_order("AAPL", "loss", {"side": "sell", "type": "market", "qty": "0.1"})

    def test_exits_remain_possible_after_operator_changes_new_entry_list(self):
        trade = self.entered()
        self.agent.state["pending"].clear()
        self.agent.rules = replace(self.rules, universe=("MSFT",))
        self.agent.limits = self.agent.fixed_limits()  # Simulate a new operator session.
        self.agent.guard_order("AAPL", "loss", {"side": "sell", "type": "market", "qty": str(trade["remaining_qty"])})

    def test_runtime_risk_changes_pause(self):
        self.agent.rules.capital_cap = 100
        with self.assertRaises(ValueError):
            self.agent.cycle()
        self.assertEqual(self.api.sent, [])

    def test_preview_never_sends_orders(self):
        self.api.mode, self.api.execute = "live", False
        preview = TradingAgent(self.api, self.rules, self.folder / "preview",
                               settings=AgentSettings(), emit=lambda _: None)
        preview.cycle()
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.api.canceled, [])

    def test_wrong_live_account_never_sends_orders(self):
        self.api.mode = "live"
        live = TradingAgent(self.api, self.rules, self.folder / "live", settings=AgentSettings(),
                            account_id="different-account", emit=lambda _: None)
        with self.assertRaises(RuntimeError):
            live.cycle()
        self.assertEqual(self.api.sent, [])

    def test_agent_upgrade_reads_existing_trade_journal(self):
        self.entered()
        restarted = TradingAgent(self.api, self.rules, self.folder,
                                 settings=AgentSettings(), emit=lambda _: None)
        restarted.cycle()
        self.assertEqual(len(self.api.sent), 2)
        self.assertEqual(restarted.memory["account_id"], "test-account")

    def test_agent_memory_rejects_other_account_journal(self):
        self.entered()
        self.agent.memory["account_id"] = "other-account"
        atomic_json(self.agent.memory_path, self.agent.memory)
        other = TradingAgent(self.api, self.rules, self.folder,
                             settings=AgentSettings(), emit=lambda _: None)
        with self.assertRaises(ValueError):
            other.cycle()

    def test_blocked_inference_does_not_block_loss_exit(self):
        rules = replace(self.rules, universe=("AAPL", "MSFT"), positions=2, capital_cap=20)
        agent = TradingAgent(self.api, rules, self.folder / "async", settings=AgentSettings(),
                             planner=FixedPlanner(("AAPL",)), emit=lambda _: None)
        agent.cycle()
        agent.planner = FixedPlanner(())
        agent.cycle()
        planner = LocalModelPlanner(AgentSettings(planner="local"))
        started, release = threading.Event(), threading.Event()

        def slow_request(observation):
            started.set()
            release.wait(2)
            return EntryPlan((), "Wait.")

        planner.request = slow_request
        agent.planner = planner
        try:
            agent.cycle()
            self.assertTrue(started.wait(0.5))
            self.assertEqual(planner.status, "awaiting_model")
            for order in self.api.orders.values():
                if order["type"] == "stop":
                    order["status"] = "expired"
            self.api.price = 49
            agent.cycle()
            self.assertEqual(self.api.sent[-1]["side"], "sell")
            self.assertEqual(self.api.sent[-1]["type"], "market")
            self.assertFalse(release.is_set())
            self.assertFalse(any(o["symbol"] == "MSFT" for o in self.api.sent))
        finally:
            release.set()
            if planner._worker:
                planner._worker.join(timeout=1)
            planner.close()

    def test_model_endpoint_rejects_external_hosts_and_embedded_secrets(self):
        invalid = ["https://api.example.com/v1", "http://localhost:8000/v1",
                   "http://127.0.0.1:8000/v1?api_key=secret",
                   "http://user:secret@127.0.0.1:8000/v1",
                   "http://127.0.0.1:8000/anything", "http://10.0.0.1:8000/v1"]
        for base in invalid:
            with self.subTest(base=base), self.assertRaises(ValueError):
                local_endpoint(base)
        self.assertEqual(local_endpoint("http://127.0.0.1:8000/v1/"),
                         "http://127.0.0.1:8000/v1/chat/completions")

    def test_model_request_uses_text_protocol_supported_by_supplied_soup(self):
        planner = LocalModelPlanner(AgentSettings(planner="local"))
        response = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": '{"symbols":["AAPL"],"reason":"Use a permitted candidate."}'}}]}
        opened = MagicMock()
        opened.__enter__.return_value.read.return_value = json.dumps(response).encode()
        planner.opener = MagicMock()
        planner.opener.open.return_value = opened
        plan = planner.request(self.observation)
        request = planner.opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(plan.symbols, ("AAPL",))
        self.assertEqual(set(body), {"model", "messages", "temperature", "max_tokens", "stream"})
        self.assertTrue(all(isinstance(m["content"], str) for m in body["messages"]))
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Apca-api-secret-key", request.headers)
        planner.close()

    def test_oversize_and_incomplete_model_responses_are_rejected(self):
        planner = LocalModelPlanner(AgentSettings(planner="local"))
        replies = [b"x" * (MAX_REPLY_BYTES + 1), json.dumps({"choices": [{
            "finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]}).encode()]
        for raw in replies:
            opened = MagicMock()
            opened.__enter__.return_value.read.return_value = raw
            planner.opener = MagicMock()
            planner.opener.open.return_value = opened
            with self.assertRaises(ValueError):
                planner.request(self.observation)
        planner.close()

    def test_rejected_model_result_defers_entries(self):
        planner = LocalModelPlanner(AgentSettings(planner="local"))
        planner._cached = (self.observation, None, "invalid_or_unavailable")
        self.assertIsNone(planner.choose(self.observation, self.api.now))
        self.assertEqual(planner.status, "invalid_or_unavailable")
        planner.close()

    def test_current_candidates_and_slots_revalidate_model_plan(self):
        planner = LocalModelPlanner(AgentSettings(planner="local"))
        planner._cached = (self.observation, EntryPlan(("AAPL",), "Buy."), "ready")
        changed = copy.deepcopy(self.observation)
        changed["available_slots"] = 0
        self.assertIsNone(planner.choose(changed, self.api.now))
        changed["available_slots"] = 1
        changed["candidates"] = [{"symbol": "MSFT"}]
        self.assertIsNone(planner.choose(changed, self.api.now))
        planner.close()

    def test_expired_model_plan_cannot_place_buy(self):
        planner = LocalModelPlanner(AgentSettings(planner="local"))
        planner._cached = (self.observation, EntryPlan(("AAPL",), "Buy."), "ready")
        now = self.api.now + timedelta(seconds=61)
        planner._period = int(now.timestamp()) // 300
        self.assertIsNone(planner.choose(self.observation, now))
        self.assertIsNone(planner._cached)
        self.assertIsNone(planner._worker)
        planner.close()

    def test_export_labels_are_decisions_and_do_not_overwrite_journal(self):
        self.agent.cycle()
        output = self.folder / "training.jsonl"
        self.assertEqual(export_training_examples(self.folder, output), 1)
        example = json.loads(output.read_text().splitlines()[0])
        self.assertFalse(example["profitability_label"])
        self.assertEqual(example["label_source"], "rules")
        self.assertNotIn("test-account", output.read_text())
        with self.assertRaises(ValueError):
            export_training_examples(self.folder, self.folder / "agent_decisions.jsonl")

    def test_full_offline_agent_simulation_records_losses_profit_and_open_positions(self):
        rules = Rules()
        with patch("agent.TradingAPI.call", side_effect=AssertionError("No network allowed")), \
                patch("agent.LocalModelPlanner.request", side_effect=AssertionError("No model allowed")):
            summary = offline_agent_demo(rules, self.folder / "demo", emit=lambda _: None)
        self.assertEqual(summary["closed_trades"], 3)
        self.assertEqual(len(summary["open_positions"]), 2)
        self.assertLess(summary["realized_gross_pnl"], 0)
        events = [json.loads(line) for line in Path(summary["decision_history"]).read_text().splitlines()]
        self.assertTrue(any(e["kind"] == "exit_transition" for e in events))
        self.assertTrue(any(e["kind"] == "order_intent" and e["purpose"] == "plateau" for e in events))
        self.assertEqual(rules.capital_cap, 50)
        self.assertEqual(rules.loss_fraction, 0.5)


if __name__ == "__main__":
    unittest.main()
