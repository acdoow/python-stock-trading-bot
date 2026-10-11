"""Invented stock paths and a tiny fake broker for the agent demo, never live data.

This is an illustrative execution model, not an exchange simulator or a broker
adapter. Stops/limits are processed on later steps, and cancellations are
instant here. The real engine still waits for reconciliation before replacing.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from adaptive import APIError, EPS
from bot import TERMINAL


class SimulatedBroker:
    mode = "paper"
    execute = True

    def __init__(self, rules):
        self.symbols = list(rules.universe[:rules.positions])
        self.start = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
        self.now = self.start
        self.rules = rules
        self.initial_cash = rules.capital_cap + rules.cash_buffer
        self.account = {"id": "invented-demo-account", "status": "ACTIVE",
                        "cash": str(self.initial_cash), "equity": str(self.initial_cash)}
        self.prices = {s: 100.0 for s in self.symbols}
        self.holdings, self.orders, self.sent = {}, {}, []
        self.history = {s: [] for s in self.symbols}
        if rules.momentum.enabled:
            # Already-completed opening observations, separate from future
            # demo paths. They permit an entry without looking ahead.
            opening = self.start.replace(hour=13, minute=30)
            count = rules.momentum.window_bars
            self.start = opening + timedelta(minutes=count * rules.bar_minutes)
            self.now = self.start
            for symbol in self.symbols:
                for i in range(count):
                    before = 99.0 + i / count
                    after = 99.0 + (i + 1) / count
                    self.history[symbol].append({
                        "t": (opening + timedelta(minutes=i * rules.bar_minutes)).isoformat(),
                        "o": before, "c": after, "h": after + 0.01, "l": before - 0.01})

    def fill(self, order, qty, price):
        sign = 1 if order["side"] == "buy" else -1
        if sign < 0 and qty > self.holdings.get(order["symbol"], 0) + EPS:
            raise ValueError("Simulated sell exceeds owned shares.")
        order.update(status="filled", filled_qty=str(qty), filled_avg_price=str(price),
                     filled_at=self.now.isoformat())
        self.holdings[order["symbol"]] = self.holdings.get(order["symbol"], 0) + sign * qty
        self.account["cash"] = str(float(self.account["cash"]) - sign * qty * price)

    def call(self, path, *, params=None, payload=None, method=None, **kwargs):
        if payload is not None:
            if path != "/v2/orders":
                raise ValueError("Only simulated order submission is supported.")
            order = {**copy.deepcopy(payload), "id": "sim-" + str(len(self.sent) + 1),
                     "status": "new", "filled_qty": "0", "filled_avg_price": None}
            self.sent.append(copy.deepcopy(payload))
            self.orders[payload["client_order_id"]] = order
            if order["type"] == "market":
                price = self.prices[order["symbol"]] + (0.01 if order["side"] == "buy" else 0)
                qty = float(order["notional"]) / price if order["side"] == "buy" else float(order["qty"])
                self.fill(order, qty, price)
            return copy.deepcopy(order)
        if method == "DELETE":
            for order in self.orders.values():
                if path == "/v2/orders/" + order["id"]:
                    if order["status"] not in TERMINAL:
                        order["status"] = "canceled"
                    return None
            raise APIError(404, "No simulated order.")
        if path == "/v2/account":
            self.account["equity"] = str(float(self.account["cash"]) + sum(
                qty * self.prices[symbol] for symbol, qty in self.holdings.items()))
            return copy.deepcopy(self.account)
        if path == "/v2/clock":
            return {"is_open": True, "timestamp": self.now.isoformat()}
        if path == "/v2/positions":
            return [{"symbol": s, "qty": str(qty), "market_value": str(qty * self.prices[s])}
                    for s, qty in self.holdings.items() if qty > EPS]
        if path == "/v2/orders":
            return [copy.deepcopy(o) for o in self.orders.values() if o["status"] not in TERMINAL]
        if path == "/v2/orders:by_client_order_id":
            try:
                return copy.deepcopy(self.orders[params["client_order_id"]])
            except KeyError:
                raise APIError(404, "No simulated order.") from None
        if path.startswith("/v2/assets/"):
            return {"status": "active", "tradable": True,
                    "fractionable": path.removeprefix("/v2/assets/") in self.symbols}
        if path == "/v2/calendar":
            first = datetime.fromisoformat(params["start"])
            return [{"date": (first + timedelta(days=i)).date().isoformat()}
                    for i in range(15) if (first + timedelta(days=i)).weekday() < 5]
        raise ValueError("Unknown simulated API request.")

    def quote(self, symbol, now, rules):
        price = self.prices[symbol]
        return {"bid": price, "ask": price + 0.01, "t": now.isoformat()}

    def bars(self, symbol, start, now, rules):
        return [copy.deepcopy(b) for b in self.history[symbol]
                if datetime.fromisoformat(b["t"]) >= start
                and datetime.fromisoformat(b["t"]) + timedelta(minutes=rules.bar_minutes) <= now]

    def advance(self, index):
        self.now = self.start + timedelta(minutes=(index + 1) * self.rules.bar_minutes)
        for scenario, symbol in enumerate(self.symbols):
            previous = self.prices[symbol]
            if scenario == 0:
                price = 100 if index < 3 else 49  # Gap through the stop.
            elif scenario == 1:
                price = 110 + (0.08 if index % 2 else -0.08)  # Profitable plateau.
            elif scenario == 2:
                price = 100 + 0.8 * (index + 1)  # Continuing rise.
            elif scenario == 3:
                price = (110 + (0.08 if index % 2 else -0.08)) if index < 19 else 125
            else:
                price = 100 + 0.2 * index if index < 6 else 90  # Open loss above the stop.
            self.prices[symbol] = price
            self.history[symbol].append({
                "t": (self.now - timedelta(minutes=self.rules.bar_minutes)).isoformat(),
                "o": previous, "c": price, "h": max(previous, price) + 0.01,
                "l": min(previous, price) - 0.01})
        for order in list(self.orders.values()):
            if order["status"] in TERMINAL or order["side"] != "sell":
                continue
            price = self.prices[order["symbol"]]
            if order["type"] == "stop" and price <= float(order["stop_price"]):
                self.fill(order, float(order["qty"]), price)
            elif order["type"] == "limit" and price >= float(order["limit_price"]):
                self.fill(order, float(order["qty"]), float(order["limit_price"]))
