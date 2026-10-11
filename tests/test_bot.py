import csv
import io
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from bot import (APIError, AlpacaPaper, Config, InstanceLock, PAPER_URL, PaperBot,
                 backtest, crossover, demo_bars, entry_budget, load_bars)


class FakeAPI:
    def __init__(self):
        self.positions = []
        self.orders = {}
        self.submissions = []
        self.is_open = True
        self.fail_post = False
        self.stale = False
        self.history_broken = False
        self.price_value = 6.0
        self.account = {"id": "paper-test", "status": "ACTIVE", "equity": "1000", "cash": "1000"}

    def call(self, path, *, params=None, payload=None, **kwargs):
        if payload is not None:
            self.submissions.append(payload.copy())
            if self.fail_post:
                raise APIError(None, "Simulated timeout")
            order = {**payload, "status": "new", "filled_qty": "0"}
            self.orders[payload["client_order_id"]] = order
            return order
        if path == "/v2/clock":
            return {"is_open": self.is_open, "timestamp": "2026-10-05T10:00:00-04:00", "next_open": "2026-10-06T09:30:00-04:00"}
        if path == "/v2/account":
            return self.account
        if path == "/v2/positions":
            return self.positions
        if path == "/v2/orders":
            return [o for o in self.orders.values() if o["status"] == "new"]
        if path == "/v2/calendar":
            return [{"date": "2026-10-02"}, {"date": "2026-10-05"}]
        if path.startswith("/v2/assets/"):
            return {"tradable": True, "fractionable": True, "status": "active"}
        if path == "/v2/orders:by_client_order_id":
            try:
                return self.orders[params["client_order_id"]]
            except KeyError:
                raise APIError(404, "Not found") from None
        raise AssertionError(path)

    def price(self, symbol, now, cfg):
        return self.price_value

    def daily_bars(self, symbol, cfg, day):
        if self.history_broken:
            raise APIError(500, "History unavailable")
        last = date(2026, 10, 1 if self.stale else 2)
        return [{"t": (last - timedelta(days=3-i)).isoformat() + "T04:00:00Z", "c": c}
                for i, c in enumerate([3, 2, 1, 6])]


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.cfg = Config(fast_days=2, slow_days=3)
        self.api = FakeAPI()
        self.bot = PaperBot(self.api, self.cfg, self.folder, emit=lambda _: None)

    def tearDown(self):
        self.temp.cleanup()

    def owned_position(self, entry=10, qty="2", symbol="SPY"):
        self.api.positions = [{"symbol": symbol, "qty": qty,
                               "avg_entry_price": str(entry), "market_value": "12"}]
        self.bot.state["managed"] = [symbol]

    def test_crossover_both_directions_and_warmup(self):
        self.assertEqual(crossover([3, 2, 1, 6], self.cfg), "buy")
        self.assertEqual(crossover([1, 2, 3, 0.5], self.cfg), "sell")
        self.assertEqual(crossover([1, 2, 3], self.cfg), "hold")

    def test_cash_cap_and_pending_orders_reserve_budget(self):
        pending = [{"symbol": "AAPL", "amount": 95}]
        self.assertEqual(entry_budget(self.cfg, 150, [], pending), 54)
        held = [{"symbol": "AAPL", "market_value": 295}]
        self.assertEqual(entry_budget(self.cfg, 1000, held, []), 5)
        three = [{"symbol": x, "market_value": 1} for x in ("A", "B", "C")]
        self.assertEqual(entry_budget(self.cfg, 1000, three, []), 0)

    def test_bad_config_is_rejected(self):
        for updates in ({"poll_seconds": 1}, {"stop_loss_fraction": 1},
                        {"dollars_per_buy": float("nan")}, {"fast_days": 20, "slow_days": 5},
                        {"symbols": ("SPY", "SPY")}, {"max_positions": 1.5}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                Config(**updates).validate()

    def test_one_virtual_buy_and_no_duplicate_after_restart(self):
        self.bot.cycle()
        self.assertEqual(len(self.api.submissions), 1)
        self.assertEqual(self.api.submissions[0]["notional"], "100.00")
        restarted = PaperBot(self.api, self.cfg, self.folder, emit=lambda _: None)
        restarted.cycle()
        self.assertEqual(len(self.api.submissions), 1)

    def test_daily_loss_pause_survives_recovery_and_restart(self):
        self.bot.state.update(day="2026-10-05", day_equity=1000)
        self.api.account["equity"] = "975"
        self.bot.cycle()
        self.assertEqual(self.api.submissions, [])
        self.api.account["equity"] = "1005"
        restarted = PaperBot(self.api, self.cfg, self.folder, emit=lambda _: None)
        restarted.cycle()
        self.assertEqual(self.api.submissions, [])
        self.assertEqual(restarted.state["halted_day"], "2026-10-05")

    def test_daily_pause_still_allows_owned_exit(self):
        self.owned_position()
        self.bot.state.update(day="2026-10-05", day_equity=1000, halted_day="2026-10-05")
        self.bot.cycle()
        self.assertEqual(self.api.submissions[0]["side"], "sell")
        self.assertEqual(self.api.submissions[0]["qty"], "2")

    def test_exit_is_checked_without_history_dependency(self):
        self.owned_position()
        self.api.history_broken = True
        self.bot.cycle()
        self.assertEqual(self.api.submissions[0]["side"], "sell")

    def test_removed_ticker_still_has_exit_checks(self):
        self.owned_position(symbol="AAPL")
        self.bot.state["halted_day"] = "2026-10-05"
        self.bot.cycle()
        self.assertTrue(any(p["symbol"] == "AAPL" and p["side"] == "sell" for p in self.api.submissions))

    def test_unowned_position_is_not_sold(self):
        self.owned_position()
        self.bot.state["managed"] = []
        self.bot.cycle()
        self.assertEqual(self.api.submissions, [])

    def test_no_orders_outside_market_hours(self):
        self.api.is_open = False
        self.bot.cycle()
        self.assertEqual(self.api.submissions, [])

    def test_stale_daily_history_prevents_entry(self):
        self.api.stale = True
        self.bot.cycle()
        self.assertEqual(self.api.submissions, [])

    def test_uncertain_post_is_journaled_and_never_blindly_retried(self):
        self.api.fail_post = True
        with self.assertRaises(APIError):
            self.bot.cycle()
        saved = json.loads(self.bot.state_path.read_text())
        self.assertEqual(len(saved["pending"]), 1)
        restarted = PaperBot(self.api, self.cfg, self.folder, emit=lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            restarted.cycle()
        self.assertEqual(len(self.api.submissions), 1)

    def test_lost_response_but_accepted_order_is_reconciled(self):
        self.bot.cycle()
        order = next(iter(self.api.orders.values()))
        order["status"], order["filled_qty"] = "filled", "16.666666666"
        self.api.positions = [{"symbol": "SPY", "qty": "16.666666666", "avg_entry_price": "6", "market_value": "100"}]
        restarted = PaperBot(self.api, self.cfg, self.folder, emit=lambda _: None)
        restarted.cycle()
        self.assertEqual(restarted.state["pending"], {})
        self.assertEqual(len(self.api.submissions), 1)

    def test_stop_file_blocks_new_order(self):
        (self.folder / "STOP_BOT").touch()
        self.bot.cycle()
        self.assertEqual(self.api.submissions, [])

    def test_stopped_event_blocks_submission(self):
        self.bot.stop.set()
        self.bot.submit("SPY", "buy", date(2026, 10, 5), "test", amount=100)
        self.assertEqual(self.api.submissions, [])

    def test_instance_lock_prevents_two_bots(self):
        with InstanceLock(self.folder / "lock"):
            with self.assertRaises(RuntimeError):
                with InstanceLock(self.folder / "lock"):
                    pass
        with InstanceLock(self.folder / "lock"):
            pass

    def test_different_account_cannot_reuse_state(self):
        self.bot.state["account_id"] = "different-account"
        with self.assertRaisesRegex(RuntimeError, "different paper account"):
            self.bot.cycle()
        self.assertEqual(self.api.submissions, [])

    def test_unrelated_open_order_prevents_new_orders(self):
        self.api.orders["manual"] = {"client_order_id": "manual", "status": "new"}
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            self.bot.cycle()
        self.assertEqual(self.api.submissions, [])

    def test_invalid_balances_prevent_orders(self):
        self.api.account["equity"] = "NaN"
        with self.assertRaisesRegex(ValueError, "Invalid account"):
            self.bot.cycle()
        self.assertEqual(self.api.submissions, [])

    def test_fixed_paper_endpoint_and_no_live_configuration(self):
        api = AlpacaPaper("dummy", "dummy-secret")
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{}')
        api.opener.open = MagicMock(return_value=response)
        api.call("/v2/orders", payload={"side": "buy"})
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, PAPER_URL + "/v2/orders")
        with self.assertRaises(TypeError):
            Config(live=True)

    def test_stale_or_future_prices_are_rejected(self):
        api = AlpacaPaper("dummy", "dummy")
        now = datetime.now(timezone.utc)
        for delta in (-301, 10):
            api.call = lambda *a, **k: {"trade": {"t": (now + timedelta(seconds=delta)).isoformat(), "p": 100}}
            with self.assertRaises(ValueError):
                api.price("SPY", now, self.cfg)

    def test_data_pagination_and_incomplete_day_exclusion(self):
        api = AlpacaPaper("dummy", "dummy")
        pages = [{"bars": {"SPY": [{"t": "2026-10-02T04:00:00Z", "c": 10}]}, "next_page_token": "next"},
                 {"bars": {"SPY": [{"t": "2026-10-05T04:00:00Z", "c": 500}]}, "next_page_token": None}]
        api.call = MagicMock(side_effect=pages)
        bars = api.daily_bars("SPY", self.cfg, date(2026, 10, 5))
        self.assertEqual([b["c"] for b in bars], [10])
        self.assertEqual(api.call.call_count, 2)

    def test_backtest_trade_is_after_signal_and_gap_is_not_capped(self):
        prices = [3, 2, 1, 6, 6, 4]
        rows = [{"date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                 "open": p, "close": p, "low": p, "high": p} for i, p in enumerate(prices)]
        result = backtest(rows, self.cfg, self.folder, emit=lambda _: None)
        with (self.folder / "trades.csv").open() as stream:
            trades = list(csv.DictReader(stream))
        self.assertEqual(trades[0]["date"], "2025-01-05")
        self.assertEqual(trades[1]["reason"], "stop gap")
        self.assertLess(result["realized_profit_loss"], -30)

    def test_invalid_historical_data_rejected(self):
        path = self.folder / "bad.csv"
        path.write_text("date,open,high,low,close\n2025-01-01,2,1,3,2\n")
        with self.assertRaises(ValueError):
            load_bars(path)

    def test_demo_includes_a_losing_scenario(self):
        result = backtest(demo_bars("falling"), Config(), self.folder, emit=lambda _: None)
        self.assertLess(result["ending_equity"], result["starting_virtual_cash"])


if __name__ == "__main__":
    unittest.main()
