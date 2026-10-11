"""Five independent fractional positions, plateau exits and bounded learning.

Run 'python adaptive.py demo' offline. Broker orders require the operator's
--execute flag; this module does not access any broker merely by importing it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import tempfile
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from momentum import Momentum, momentum_signal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot import APIError, AlpacaPaper, DATA_URL, InstanceLock, ROOT, TERMINAL

EPS = 1e-8


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Broker timestamps must include a timezone.")
    return result


def ny_zone():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        raise RuntimeError("Install timezone data: python -m pip install tzdata") from None


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def finite(value, name, *, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"Invalid {name}.")
    return result


def price_up(price):
    tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return str(Decimal(str(price)).quantize(tick, rounding=ROUND_UP))


def quantity_down(quantity):
    return str(Decimal(str(quantity)).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN))


def account_lock_path(mode, account_id):
    directory = Path(tempfile.gettempdir()) / "stockbot-account-locks"
    directory.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(f"{mode}:{account_id}".encode()).hexdigest()[:32]
    return directory / (token + ".lock")


@dataclass(frozen=True)
class Plateau:
    window_bars: int = 12
    max_range_fraction: float = 0.01
    max_slope_fraction: float = 0.0025
    near_high_fraction: float = 0.015
    min_turns: int = 3

    def validate(self):
        if type(self.window_bars) is not int or not 6 <= self.window_bars <= 60:
            raise ValueError("Plateau window must be 6 to 60 bars.")
        if type(self.min_turns) is not int or not 1 <= self.min_turns < self.window_bars - 1:
            raise ValueError("Invalid minimum fluctuation count.")
        for name in ("max_range_fraction", "max_slope_fraction", "near_high_fraction"):
            if not 0 < finite(getattr(self, name), name) <= 0.10:
                raise ValueError(f"Invalid {name}.")
        return self


@dataclass
class Rules:
    # These company tickers are editable examples, not an investment ranking.
    universe: tuple[str, ...] = ("AAPL", "MSFT", "AMZN", "GOOGL", "META")
    selection: str = "ordered"
    random_seed: int = 41
    positions: int = 5
    dollars_each: float = 10.0
    capital_cap: float = 50.0
    cash_buffer: float = 0.01
    loss_fraction: float = 0.50
    min_gain_fraction: float = 0.02
    min_net_profit_dollars: float = 0.01
    estimated_roundtrip_cost: float = 0.02
    bar_minutes: int = 5
    poll_seconds: int = 15
    max_quote_age_seconds: int = 60
    max_spread_fraction: float = 0.01
    research_horizon_bars: int = 120
    auto_adapt_paper: bool = True
    auto_adapt_live: bool = False
    plateau: Plateau = field(default_factory=Plateau)
    momentum: Momentum = field(default_factory=Momentum)

    def validate(self):
        import re
        if not self.universe or len(set(self.universe)) != len(self.universe):
            raise ValueError("Choose distinct example or user-selected tickers.")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", x) for x in self.universe):
            raise ValueError("Invalid ticker.")
        if self.selection not in ("ordered", "random"):
            raise ValueError("selection must be ordered or random.")
        for name in ("positions", "bar_minutes", "poll_seconds", "max_quote_age_seconds", "research_horizon_bars", "random_seed"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer.")
        if not 1 <= self.positions <= 5 or len(self.universe) < self.positions:
            raise ValueError("Choose enough tickers for 1 to 5 positions.")
        if self.bar_minutes not in (1, 5, 15) or self.poll_seconds < 15 or self.max_quote_age_seconds < 1:
            raise ValueError("Use 1, 5 or 15 minute bars and polling >= 15 seconds.")
        if not 60 <= self.research_horizon_bars <= 500:
            raise ValueError("Research horizon must be 60 to 500 bars.")
        for name in ("dollars_each", "capital_cap"):
            if finite(getattr(self, name), name, positive=True) < 1:
                raise ValueError(f"{name} must be at least $1.")
        if self.dollars_each * self.positions > self.capital_cap + EPS:
            raise ValueError("The basket must fit within the capital cap.")
        for name in ("cash_buffer", "min_net_profit_dollars", "estimated_roundtrip_cost"):
            if finite(getattr(self, name), name) < 0:
                raise ValueError(f"{name} cannot be negative.")
        for name in ("loss_fraction", "min_gain_fraction", "max_spread_fraction"):
            if not 0 < finite(getattr(self, name), name) < 1:
                raise ValueError(f"{name} must be between zero and one.")
        if type(self.auto_adapt_paper) is not bool or type(self.auto_adapt_live) is not bool:
            raise ValueError("Learning switches must be true or false.")
        self.plateau.validate()
        self.momentum.validate()
        if self.momentum.window_bars * self.bar_minutes > 390:
            raise ValueError("The growth window must fit within a regular trading session.")
        return self


def load_rules(path=ROOT / "rules.json"):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["universe"] = tuple(data["universe"])
    data["plateau"] = Plateau(**data.get("plateau", {}))
    data["momentum"] = Momentum(**data.get("momentum", {}))
    return Rules(**data).validate()


def plateau_target(closes, peak, entry_price, quantity, rules, profile=None):
    """Use only observations already available. No prediction of the true top."""
    profile = profile or rules.plateau
    if len(closes) < profile.window_bars:
        return None
    values = [finite(v, "price", positive=True) for v in closes[-profile.window_bars:]]
    mean = sum(values) / len(values)
    peak = finite(peak, "observed peak", positive=True)
    if mean < entry_price * (1 + rules.min_gain_fraction):
        return None
    if mean < peak * (1 - profile.near_high_fraction):
        return None
    if (max(values) - min(values)) / mean > profile.max_range_fraction:
        return None
    half = len(values) // 2
    slope = abs(sum(values[half:]) / len(values[half:]) - sum(values[:half]) / half) / mean
    if slope > profile.max_slope_fraction:
        return None
    directions = [1 if b > a else -1 for a, b in zip(values, values[1:]) if b != a]
    turns = sum(a != b for a, b in zip(directions, directions[1:]))
    if turns < profile.min_turns:
        return None
    estimated_net = quantity * (mean - entry_price) - rules.estimated_roundtrip_cost
    return mean if estimated_net >= rules.min_net_profit_dollars else None


class TradingAPI(AlpacaPaper):
    """Broker adapter. Mutation is disabled unless explicitly enabled locally."""
    def __init__(self, key, secret, *, mode="paper", execute=False):
        super().__init__(key, secret)
        if mode not in ("paper", "live"):
            raise ValueError("Mode must be paper or live.")
        self.mode, self.execute = mode, bool(execute)
        self.base = "https://paper-api.alpaca.markets" if mode == "paper" else "https://api.alpaca.markets"

    def call(self, path, *, params=None, payload=None, data_api=False, method=None):
        verb = method or ("POST" if payload is not None else "GET")
        if verb not in ("GET", "POST", "DELETE") or not path.startswith("/v2/"):
            raise ValueError("Unsupported API request.")
        if verb != "GET" and (not self.execute or data_api):
            raise RuntimeError("Order submission/cancellation is disabled in preview mode.")
        url = (DATA_URL if data_api else self.base) + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=self.headers, method=verb)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as error:
            raise APIError(error.code, f"Broker HTTP {error.code}. Check credentials, account permissions and paper/live dashboard.") from None
        except (TimeoutError, URLError, OSError):
            raise APIError(None, "Network request failed. The journal will be reconciled before another order.") from None

    def quote(self, symbol, now, rules):
        quote = self.call(f"/v2/stocks/{symbol}/quotes/latest", data_api=True,
                          params={"feed": "iex"})["quote"]
        age = (now - stamp(quote["t"])).total_seconds()
        bid, ask = finite(quote["bp"], "bid", positive=True), finite(quote["ap"], "ask", positive=True)
        if not -30 <= age <= rules.max_quote_age_seconds or ask < bid:
            raise ValueError(f"{symbol}: quote is missing, stale or invalid.")
        if (ask - bid) / ((ask + bid) / 2) > rules.max_spread_fraction:
            raise ValueError(f"{symbol}: spread exceeds the configured limit.")
        return {"bid": bid, "ask": ask, "t": quote["t"]}

    def bars(self, symbol, start, now, rules):
        params = {"symbols": symbol, "timeframe": f"{rules.bar_minutes}Min", "feed": "iex",
                  "adjustment": "raw", "sort": "asc", "limit": 10000,
                  "start": start.isoformat(), "end": now.isoformat()}
        bars = []
        while True:
            page = self.call("/v2/stocks/bars", data_api=True, params=params)
            bars.extend(page.get("bars", {}).get(symbol, []))
            if not page.get("next_page_token"):
                break
            params["page_token"] = page["next_page_token"]
        valid = []
        for bar in sorted(bars, key=lambda b: b["t"]):
            when = stamp(bar["t"])
            local = when.astimezone(ny_zone())
            minutes = local.hour * 60 + local.minute
            if when < start or when + timedelta(minutes=rules.bar_minutes) > now:
                continue
            if local.weekday() >= 5 or not 570 <= minutes < 960:
                continue
            values = {k: finite(bar[k], k, positive=True) for k in ("o", "h", "l", "c")}
            if not values["l"] <= min(values["o"], values["c"]) <= max(values["o"], values["c"]) <= values["h"]:
                raise ValueError("Invalid OHLC data from the feed.")
            valid.append({"t": bar["t"], **values})
        return valid


def replay_episode(episode, rules, profile):
    """Counterfactual exit test: a target first fills in a LATER bar.

    Returns and fills are estimates. All candidates get the same future
    observation horizon, including prices collected after the actual sale.
    """
    entry = episode["entry_cost"] / episode["entry_qty"]
    qty = episode["entry_qty"]
    stop_price = entry * (1 - rules.loss_fraction)
    peak, closes, pending = entry, [], None
    last_time, pending_day = None, None
    bars = episode["trace"][:rules.research_horizon_bars]
    if not bars:
        raise ValueError("An episode needs an observed price trace.")
    for bar in bars:
        when = stamp(bar["t"])
        day = when.astimezone(ny_zone()).date()
        if last_time is not None and (when - last_time).total_seconds() > rules.bar_minutes * 90:
            closes = []
        if pending is not None and day != pending_day:
            pending = None  # Fractional DAY targets expire.
        last_time = when
        # Conservative when a daily/intraday OHLC bar touches both thresholds.
        if bar["l"] <= stop_price:
            fill = min(bar["o"], stop_price) * 0.9995
            return (qty * fill - episode["entry_cost"] - rules.estimated_roundtrip_cost) / episode["entry_cost"]
        if pending is not None and bar["h"] >= pending:
            return (qty * pending - episode["entry_cost"] - rules.estimated_roundtrip_cost) / episode["entry_cost"]
        peak = max(peak, bar["h"])
        closes.append(bar["c"])
        if pending is None:
            target = plateau_target(closes, peak, entry, qty, rules, profile)
            if target is not None:
                pending = float(price_up(target))
                pending_day = day
    # Mark at horizon; this is not a claim that a sale actually occurred.
    return (qty * bars[-1]["c"] - episode["entry_cost"] - rules.estimated_roundtrip_cost) / episode["entry_cost"]


def learning_report(episodes, rules, baseline=None):
    baseline = baseline or rules.plateau
    complete = sorted((x for x in episodes if len(x.get("trace", [])) >= rules.research_horizon_bars),
                      key=lambda x: stamp(x["opened_at"]))
    report = {"completed_episodes": len(complete), "minimum_episodes": 30,
              "status": "collecting", "eligible_for_paper_update": False,
              "live_parameters_changed": False, "capital_and_loss_limits_changed": False,
              "note": "Exit-only research, estimated fills/costs, and correlated observations; no guarantee of improvement."}
    if len(complete) < 30:
        return report
    # One chronological training block and a later validation block. Select
    # exactly one challenger on training; do not pick winners on validation.
    cut = len(complete) - 10
    train, validation = complete[:cut], complete[cut:]
    if len(validation) < 10:
        return report
    # Purge overlapping observation horizons between training and validation.
    validation_start = stamp(validation[0]["opened_at"])
    train = [e for e in train if stamp(e["trace"][rules.research_horizon_bars - 1]["t"])
             + timedelta(minutes=rules.bar_minutes) <= validation_start]
    if len(train) < 20:
        report["status"] = "collecting_nonoverlapping_data"
        return report
    candidates = [replace(baseline, window_bars=w, max_range_fraction=band)
                  for w in (6, 12, 18) for band in (0.005, 0.01, 0.02)]
    average = lambda profile, data: sum(replay_episode(e, rules, profile) for e in data) / len(data)
    challenger = max(candidates, key=lambda p: average(p, train))
    original = [replay_episode(e, rules, baseline) for e in validation]
    proposed = [replay_episode(e, rules, challenger) for e in validation]
    improvements = [new - old for new, old in zip(proposed, original)]
    mean_improvement = sum(improvements) / len(improvements)
    improvement_share = sum(x > EPS for x in improvements) / len(improvements)
    eligible = (challenger != baseline and mean_improvement >= 0.005 and improvement_share >= 0.6
                and min(proposed) >= min(original) - EPS)
    report.update(status="evaluated", training_episodes=len(train), validation_episodes=len(validation),
                  baseline=asdict(baseline), challenger=asdict(challenger),
                  validation_mean_improvement_fraction=mean_improvement,
                  validation_improved_episode_fraction=improvement_share,
                  eligible_for_paper_update=eligible)
    return report


def make_demo_episodes(rules, count=45):
    """Invented independent price paths. Never presented as real market data."""
    rng = random.Random(73)
    episodes = []
    for i in range(count):
        start_day = date(2025, 1, 2) + timedelta(days=i * 4)
        while start_day.weekday() >= 5:
            start_day += timedelta(days=1)
        start = datetime.combine(start_day, time(9, 30), ny_zone()).astimezone(timezone.utc)
        when = start
        kind = ("rise_then_flat", "big_loss", "keep_rising", "flat_then_rise", "rise_then_fall")[i % 5]
        price, bars = 100.0, []
        for j in range(rules.research_horizon_bars):
            opening = price
            if kind == "big_loss":
                price = max(40, 100 - j * 3) + rng.uniform(-0.08, 0.08)
            elif kind == "keep_rising":
                price = 100 + j * 0.2
            elif kind == "flat_then_rise":
                price = 104 + rng.uniform(-0.12, 0.12) if j < 22 else 104 + (j - 22) * 0.22
            elif kind == "rise_then_fall":
                price = 100 + j * 0.7 if j < 18 else max(60, 112.6 - (j - 18) * 0.7)
            else:
                price = 100 + j * 0.6 if j < 20 else 112 + rng.uniform(-0.14, 0.14)
            local = when.astimezone(ny_zone())
            if local.hour * 60 + local.minute >= 960:
                next_day = local.date() + timedelta(days=1)
                while next_day.weekday() >= 5:
                    next_day += timedelta(days=1)
                when = datetime.combine(next_day, time(9, 30), ny_zone()).astimezone(timezone.utc)
            bars.append({"t": when.isoformat(),
                         "o": opening, "c": price, "h": max(opening, price) + 0.01,
                         "l": min(opening, price) - 0.01})
            when += timedelta(minutes=rules.bar_minutes)
        episodes.append({"trade_id": f"demo-{i}", "symbol": rules.universe[i % len(rules.universe)],
                         "opened_at": start.isoformat(), "entry_qty": rules.dollars_each / 100,
                         "entry_cost": rules.dollars_each, "scenario": kind, "trace": bars})
    return episodes


def offline_demo(rules, folder, emit=print):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    episodes = make_demo_episodes(rules)
    results = []
    for episode in episodes[:rules.positions]:
        value = episode["entry_cost"] * (1 + replay_episode(episode, rules, rules.plateau))
        results.append({"symbol": episode["symbol"], "scenario": episode["scenario"],
                        "initial_virtual_value": rules.dollars_each, "estimated_exit_or_horizon_value": value})
        emit(f"SYNTHETIC {episode['symbol']}: {episode['scenario']}; ${rules.dollars_each:.2f} → ${value:.2f}.")
    report = learning_report(episodes, rules)
    atomic_json(folder / "synthetic_episodes.json", episodes)
    atomic_json(folder / "learning_report.json", report)
    summary = {"data": "invented; not actual stock performance", "positions": results,
               "total_initial_virtual_value": rules.dollars_each * rules.positions,
               "estimated_total_exit_or_horizon_value": sum(x["estimated_exit_or_horizon_value"] for x in results),
               "learning_status": report["status"], "learning_applied": False}
    atomic_json(folder / "demo_summary.json", summary)
    emit("A flat region can be followed by another rise. The detector cannot identify the final top in advance.")
    return summary


class BasketBot:
    def __init__(self, api, rules, folder, *, account_id=None, emit=print, stop=None):
        self.api, self.rules = api, rules.validate()
        self.expected_account = account_id
        if api.mode == "live" and api.execute and not account_id:
            raise ValueError("Live orders require the exact brokerage account ID entered locally.")
        self.emit, self.stop = emit, stop or threading.Event()
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "basket_state.json"
        self.state = self.read_state()
        self.entry_bar_cache = {}
        self.entry_checks = {}
        self.entry_screen_keys = {}

    def read_state(self):
        state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "schema": 2, "mode": self.api.mode, "account_id": None, "trades": {},
            "pending": {}, "closed": [], "seen_buys": {}, "settlement_until": None,
            "learning_count": 0, "active_profile": asdict(self.rules.plateau)}
        if state.get("schema") != 2 or state.get("mode") != self.api.mode:
            raise ValueError("Use a separate results folder for this version and each account mode.")
        return state

    def save(self):
        atomic_json(self.path, self.state)

    def log(self, message):
        text = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {message}"
        with (self.folder / "events.txt").open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")
        self.emit(text)

    def export(self):
        # Canonical closed trades are saved atomically in state. CSV is a view,
        # regenerated rather than appended to avoid duplicate crash entries.
        with (self.folder / "trades.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["trade_id", "symbol", "opened_at", "closed_at", "entry_cost", "proceeds",
                             "realized_gross_pnl", "estimated_net_pnl", "reason"])
            for trade in self.state["closed"]:
                writer.writerow([trade.get(k, "") for k in ("trade_id", "symbol", "opened_at", "closed_at", "entry_cost", "proceeds",
                                                          "realized_gross_pnl", "estimated_net_pnl", "reason")])

    def next_session(self, timestamp):
        day = stamp(timestamp).astimezone(ny_zone()).date()
        calendar = self.api.call("/v2/calendar", params={"start": day.isoformat(),
                                                          "end": (day + timedelta(days=14)).isoformat()})
        dates = sorted(s["date"] for s in calendar if s["date"] > day.isoformat())
        if not dates:
            raise RuntimeError("Could not determine the next settlement session; entries remain paused.")
        return dates[0]

    def finish_trade(self, symbol, order):
        trade = self.state["trades"][symbol]
        closed_at = order.get("filled_at") or datetime.now(timezone.utc).isoformat()
        settlement = self.next_session(closed_at)
        record = {**trade, "closed_at": closed_at, "reason": order.get("_purpose", "exit"),
                  "realized_gross_pnl": trade["proceeds"] - trade["entry_cost"],
                  "estimated_net_pnl": trade["proceeds"] - trade["entry_cost"] - self.rules.estimated_roundtrip_cost}
        if not any(x["trade_id"] == trade["trade_id"] for x in self.state["closed"]):
            self.state["closed"].append(record)
        self.state["settlement_until"] = max(settlement, self.state["settlement_until"] or settlement)
        del self.state["trades"][symbol]
        self.log(f"CLOSED {symbol}: gross ${record['realized_gross_pnl']:+.4f}; estimated net ${record['estimated_net_pnl']:+.4f}.")

    def reconcile(self):
        for client_id in list(self.state["pending"]):
            item = self.state["pending"][client_id]
            try:
                order = self.api.call("/v2/orders:by_client_order_id", params={"client_order_id": client_id})
            except APIError as error:
                if error.status == 404:
                    raise RuntimeError(f"Submission {client_id} is unconfirmed. Check the dashboard; do not clear or resend blindly.") from None
                raise
            item["broker_id"], item["status"] = order["id"], order["status"]
            qty = finite(order.get("filled_qty") or 0, "filled quantity")
            average_price = finite(order.get("filled_avg_price") or 0, "fill price", positive=qty > EPS)
            value = qty * average_price
            if qty < item["last_qty"] - EPS or value < item["last_value"] - EPS:
                raise RuntimeError("A broker fill was revised backwards; review the journal manually.")
            change_qty, change_value = qty - item["last_qty"], value - item["last_value"]
            trade = self.state["trades"].get(item["symbol"])
            if trade is None and change_qty > EPS:
                raise RuntimeError("A fill does not match an owned trade; review account activity.")
            if trade is not None:
                if item["purpose"] == "entry":
                    trade["entry_qty"] += change_qty
                    trade["remaining_qty"] += change_qty
                    trade["entry_cost"] += change_value
                    if change_qty > EPS and not trade["opened_at"]:
                        trade["opened_at"] = order.get("filled_at") or order.get("updated_at") or trade["requested_at"]
                else:
                    trade["remaining_qty"] -= change_qty
                    trade["proceeds"] += change_value
                    if trade["remaining_qty"] < -EPS:
                        raise RuntimeError("Sold quantity exceeds the recorded owned shares.")
            item["last_qty"], item["last_value"] = qty, value
            if order["status"] in TERMINAL:
                if item["purpose"] == "entry" and trade and trade["entry_qty"] <= EPS:
                    del self.state["trades"][item["symbol"]]
                elif trade and trade["remaining_qty"] <= EPS and item["purpose"] != "entry":
                    self.finish_trade(item["symbol"], {**order, "_purpose": item["purpose"]})
                elif trade and item["purpose"] == "plateau" and not item.get("cancel_requested"):
                    trade["exit_plan"] = None  # Expired/unfilled: reassess and re-protect.
                del self.state["pending"][client_id]
                self.log(f"{item['symbol']} {item['purpose']}: {order['status']}, filled {qty:.9f} shares.")
                self.save()
                if item["purpose"] in ("loss", "protective_stop") and order["status"] == "rejected":
                    raise RuntimeError("A loss/protection order was rejected. Inspect the brokerage account immediately.")
            else:
                self.save()
        self.export()

    def submit(self, symbol, purpose, payload, now):
        if not self.api.execute:
            self.log(f"PREVIEW {symbol}: {purpose} {json.dumps(payload)}")
            return False
        if self.stop.is_set() or (self.folder / "STOP_BOT").exists():
            return False
        trade = self.state["trades"][symbol]
        trade["order_sequence"] += 1
        identity = f"{self.state['account_id']}:{trade['trade_id']}:{purpose}:{trade['order_sequence']}"
        client_id = "basket-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        body = {"symbol": symbol, "time_in_force": "day", "client_order_id": client_id, **payload}
        self.state["pending"][client_id] = {"symbol": symbol, "purpose": purpose, "payload": body,
            "broker_id": None, "status": "unconfirmed", "last_qty": 0.0, "last_value": 0.0,
            "requested_at": now.isoformat(), "cancel_requested": False}
        self.save()  # Intent survives a missing POST response or process crash.
        response = self.api.call("/v2/orders", payload=body)
        self.state["pending"][client_id]["broker_id"] = response["id"]
        self.save()
        self.log(f"{self.api.mode.upper()} submitted {purpose} {symbol}; acceptance is not a fill.")
        return True

    def cancel_symbol(self, symbol, now):
        for item in self.state["pending"].values():
            if item["symbol"] != symbol:
                continue
            if item["cancel_requested"]:
                if (now - stamp(item["cancel_at"])).total_seconds() > 120:
                    raise RuntimeError("Cancellation remains unconfirmed. Check the broker before sending another exit.")
                continue
            if not self.api.execute:
                self.log(f"PREVIEW cancel {symbol} {item['purpose']} before changing exits.")
                continue
            if self.stop.is_set() or (self.folder / "STOP_BOT").exists():
                return
            item["cancel_requested"], item["cancel_at"] = True, now.isoformat()
            self.save()
            # Never send a replacement until a later GET confirms cancellation
            # or execution and owned quantity has been checked again.
            self.api.call(f"/v2/orders/{item['broker_id']}", method="DELETE")
            self.log(f"Cancellation requested for {symbol}; waiting for broker confirmation.")

    def observe(self, trade, now):
        if not trade.get("opened_at"):
            return
        bucket = int(now.timestamp()) // (self.rules.bar_minutes * 60)
        if trade.get("observed_bucket") == bucket:
            return
        start = stamp(trade.get("last_bar") or trade["opened_at"])
        bars = self.api.bars(trade["symbol"], start, now, self.rules)
        for bar in bars:
            if trade.get("last_bar") and stamp(bar["t"]) <= stamp(trade["last_bar"]):
                continue
            if trade.get("last_bar") and (stamp(bar["t"]) - stamp(trade["last_bar"])).total_seconds() > self.rules.bar_minutes * 90:
                trade["window"] = []
            trade["last_bar"] = bar["t"]
            if len(trade["trace"]) < self.rules.research_horizon_bars:
                trade["trace"].append(bar)
            trade["window"].append(bar["c"])
            trade["window"] = trade["window"][-60:]
            trade["peak"] = max(trade["peak"], bar["h"])
        trade["observed_bucket"] = bucket

    def update_research(self, now):
        for episode in self.state["closed"]:
            if len(episode["trace"]) < self.rules.research_horizon_bars:
                self.observe(episode, now)
        complete_count = sum(len(e["trace"]) >= self.rules.research_horizon_bars for e in self.state["closed"])
        # After a validation block has been evaluated, require ten new complete
        # episodes before evaluating again. Live adaptation is a separate,
        # operator-enabled setting, disabled by default.
        previous = self.state["learning_count"]
        if complete_count < 30 or complete_count >= previous + 10:
            active = Plateau(**self.state["active_profile"])
            report = learning_report(self.state["closed"], self.rules, active)
            if report["status"] == "evaluated":
                self.state["learning_count"] = complete_count
                learning_enabled = (self.rules.auto_adapt_paper if self.api.mode == "paper"
                                    else self.rules.auto_adapt_live)
                if report["eligible_for_paper_update"] and self.api.execute and learning_enabled:
                    self.state["active_profile"] = report["challenger"]
                    report[self.api.mode + "_parameters_changed"] = True
                    self.log(f"{self.api.mode.upper()} learning update applies only to NEW trades; budgets and loss threshold stay fixed.")
            atomic_json(self.folder / "learning_report.json", report)
        self.save()

    def pending_for(self, symbol):
        return [x for x in self.state["pending"].values() if x["symbol"] == symbol]

    def fresh_positions(self):
        positions = self.api.call("/v2/positions")
        for position in positions:
            qty = finite(position["qty"], "position quantity", positive=True)
            trade = self.state["trades"].get(position["symbol"])
            if trade is None or abs(qty - trade["remaining_qty"]) > EPS:
                # A fill may have occurred between the first reconciliation and
                # the positions request; give the broker one chance to reconcile.
                self.reconcile()
                positions = self.api.call("/v2/positions")
                break
        for position in positions:
            qty = finite(position["qty"], "position quantity", positive=True)
            trade = self.state["trades"].get(position["symbol"])
            if trade is None or abs(qty - trade["remaining_qty"]) > EPS:
                raise RuntimeError("Use a dedicated account: holdings do not match this bot's recorded fills.")
        held = {p["symbol"] for p in positions}
        if any(t["remaining_qty"] > EPS and s not in held for s, t in self.state["trades"].items()):
            self.reconcile()
            positions = self.api.call("/v2/positions")
            held = {p["symbol"] for p in positions}
            if any(t["remaining_qty"] > EPS and s not in held for s, t in self.state["trades"].items()):
                raise RuntimeError("An owned position disappeared without a recorded fill; inspect the account.")
        return positions

    def manage(self, symbol, position, now):
        trade = self.state["trades"][symbol]
        if trade["entry_qty"] <= EPS:
            return
        pending = self.pending_for(symbol)
        # A partly filled opening market order must finish/cancel before any
        # closing order, to avoid accidental overselling or self-trading.
        try:
            quote = self.api.quote(symbol, now, self.rules)
        except ValueError as error:
            self.log(str(error) + " Existing broker-held orders remain responsible for protection.")
            return
        entry = trade["entry_cost"] / trade["entry_qty"]
        qty = min(finite(position["qty"], "quantity", positive=True), trade["remaining_qty"])
        at_loss = quote["bid"] <= entry * (1 - self.rules.loss_fraction)
        if at_loss:
            if pending:
                if not any(x["purpose"] in ("loss", "protective_stop") for x in pending):
                    self.cancel_symbol(symbol, now)
                return
            self.submit(symbol, "loss", {"side": "sell", "type": "market", "qty": quantity_down(qty)}, now)
            return
        if any(x["purpose"] in ("entry", "loss") or x["cancel_requested"] for x in pending):
            return
        # Protect the holding immediately, without depending on history/data
        # downloads. Fractional stop orders are DAY orders and need renewal.
        if not pending and trade.get("exit_plan") is None:
            self.submit(symbol, "protective_stop", {"side": "sell", "type": "stop", "qty": quantity_down(qty),
                "stop_price": price_up(entry * (1 - self.rules.loss_fraction))}, now)
            return
        self.observe(trade, now)
        # Include checked quotes in the high-water mark, not in the closed-bar
        # fluctuation window. Repeated polling of one quote never creates a plateau.
        trade["peak"] = max(trade["peak"], quote["bid"])
        profile = Plateau(**trade["profile"])
        target = plateau_target(trade["window"], trade["peak"], entry, qty, self.rules, profile)
        if not trade.get("last_bar") or (now - stamp(trade["last_bar"])).total_seconds() > self.rules.bar_minutes * 180:
            target = None  # A fresh quote must not revive a stale plateau window.
        if pending:
            if target is not None and any(x["purpose"] == "protective_stop" for x in pending):
                trade["exit_plan"] = {"limit_price": price_up(target), "observed_at": now.isoformat()}
                self.save()
                self.cancel_symbol(symbol, now)
            return
        plan = trade.get("exit_plan")
        if plan is not None:
            net = qty * (float(plan["limit_price"]) - entry) - self.rules.estimated_roundtrip_cost
            if net >= self.rules.min_net_profit_dollars:
                self.submit(symbol, "plateau", {"side": "sell", "type": "limit", "qty": quantity_down(qty),
                                               "limit_price": plan["limit_price"]}, now)
            else:
                trade["exit_plan"] = None
        self.save()

    def configured_symbols(self):
        symbols = list(self.rules.universe)
        if self.rules.selection == "random":
            random.Random(self.rules.random_seed).shuffle(symbols)
        return symbols

    def record_entry_screen(self, symbol, signal, now):
        self.log(f"ENTRY {symbol}: {'PASS' if signal['eligible'] else 'WAIT'}. {signal['reason']}")

    def check_entry_momentum(self, symbol, quote, now):
        """Recompute eligibility with each fresh quote; reuse only recent bars."""
        if not self.rules.momentum.enabled:
            return {"eligible": True, "reason": "Configured-list entry selected."}
        local = now.astimezone(ny_zone())
        session_open = local.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
        bucket = int(now.timestamp()) // (self.rules.bar_minutes * 60)
        cached = self.entry_bar_cache.get(symbol)
        if (cached is None or cached["session_open"] != session_open or cached["bucket"] != bucket
                or not 0 <= (now - cached["fetched_at"]).total_seconds() < 60):
            cached = {"session_open": session_open, "bucket": bucket, "fetched_at": now,
                      "bars": self.api.bars(symbol, session_open, now, self.rules)}
            self.entry_bar_cache[symbol] = cached
        signal = momentum_signal(cached["bars"], quote["bid"], now, session_open,
                                 self.rules.bar_minutes, self.rules.momentum)
        self.entry_checks[symbol] = {"signal": signal, "observed_at": now.isoformat(),
                                     "profile": asdict(self.rules.momentum)}
        key = (session_open.isoformat(), bucket, signal["eligible"], signal["reason"] if not signal["eligible"] else "pass")
        if self.entry_screen_keys.get(symbol) != key:
            self.entry_screen_keys[symbol] = key
            self.record_entry_screen(symbol, signal, now)
        return signal

    def entry_candidates(self, account, positions, day, now, *, limit=None):
        occupied = set(self.state["trades"]) | {p["symbol"] for p in positions}
        candidates = []
        for symbol in self.configured_symbols():
            if symbol in occupied or self.state["seen_buys"].get(symbol) == day:
                continue
            if self.stop.is_set() or (self.folder / "STOP_BOT").exists():
                return []
            try:
                quote = self.api.quote(symbol, now, self.rules)
                signal = self.check_entry_momentum(symbol, quote, now)
            except ValueError as error:
                self.log(str(error))
                continue
            if not signal["eligible"]:
                continue
            asset = self.api.call(f"/v2/assets/{symbol}")
            if not (asset.get("status") == "active" and asset.get("tradable") and asset.get("fractionable")):
                continue
            candidate = {"symbol": symbol, "bid": quote["bid"], "ask": quote["ask"],
                         "spread_fraction": (quote["ask"] - quote["bid"]) / ((quote["ask"] + quote["bid"]) / 2)}
            if self.rules.momentum.enabled:
                candidate["momentum"] = signal
            candidates.append(candidate)
            if limit is not None and len(candidates) >= limit:
                break
        if self.rules.momentum.enabled:
            candidates.sort(key=lambda c: (-c["momentum"]["window_gain_fraction"],
                                           -c["momentum"]["session_gain_fraction"], c["symbol"]))
        return candidates

    def entry_symbols(self, account, positions, day, now):
        """Planner seam. Entries still recheck quotes, growth, assets and caps."""
        if not self.rules.momentum.enabled:
            return self.configured_symbols()
        return [c["symbol"] for c in self.entry_candidates(account, positions, day, now)]

    def entries(self, account, positions, day, now):
        if self.state["settlement_until"] and day < self.state["settlement_until"]:
            self.log(f"New buys paused until settlement session {self.state['settlement_until']}.")
            return
        symbols = self.entry_symbols(account, positions, day, now)
        if len(set(symbols)) != len(symbols) or any(s not in self.rules.universe for s in symbols):
            raise ValueError("An entry planner returned duplicate or unapproved tickers.")
        cash = finite(account["cash"], "cash")
        market_value = sum(abs(finite(p["market_value"], "market value")) for p in positions)
        planned_count, planned_reserve = 0, 0.0
        for symbol in symbols:
            if len(self.state["trades"]) + planned_count >= self.rules.positions:
                break
            if symbol in self.state["trades"] or self.state["seen_buys"].get(symbol) == day:
                continue
            if self.stop.is_set() or (self.folder / "STOP_BOT").exists():
                break
            reserved = sum(max(0, float(x["payload"].get("notional", 0)) - x["last_value"])
                           for x in self.state["pending"].values() if x["purpose"] == "entry")
            reserved += planned_reserve
            outstanding_cost = sum(t["entry_cost"] * t["remaining_qty"] / t["entry_qty"]
                                   for t in self.state["trades"].values() if t["entry_qty"] > EPS)
            budget = min(self.rules.dollars_each, cash - reserved - self.rules.cash_buffer,
                         self.rules.capital_cap - outstanding_cost - reserved,
                         self.rules.capital_cap - market_value - reserved)
            amount = math.floor((budget + 1e-10) * 100) / 100
            if amount < 1:
                break
            try:
                quote = self.api.quote(symbol, now, self.rules)
                if not self.check_entry_momentum(symbol, quote, now)["eligible"]:
                    continue
            except ValueError as error:
                self.log(str(error))
                continue
            asset = self.api.call(f"/v2/assets/{symbol}")
            if not (asset.get("status") == "active" and asset.get("tradable") and asset.get("fractionable")):
                self.log(f"{symbol}: not eligible for fractional orders.")
                continue
            payload = {"side": "buy", "type": "market", "notional": f"{amount:.2f}"}
            if not self.api.execute:
                self.log(f"PREVIEW buy up to ${amount:.2f} of {symbol}; ask ${quote['ask']:.4f}. No order sent.")
                planned_count += 1
                planned_reserve += amount
                continue
            import uuid
            learning_enabled = self.rules.auto_adapt_paper if self.api.mode == "paper" else self.rules.auto_adapt_live
            profile = (self.state["active_profile"] if learning_enabled
                       else asdict(self.rules.plateau))
            self.state["trades"][symbol] = {"trade_id": str(uuid.uuid4()), "symbol": symbol,
                "requested_at": now.isoformat(), "opened_at": None, "entry_qty": 0.0,
                "entry_cost": 0.0, "remaining_qty": 0.0, "proceeds": 0.0, "order_sequence": 0,
                "peak": quote["ask"], "window": [], "trace": [], "profile": profile,
                "exit_plan": None, "last_bar": None}
            self.state["seen_buys"][symbol] = day
            self.submit(symbol, "entry", payload, now)

    def cycle(self):
        account = self.api.call("/v2/account")
        actual_id = account["id"]
        if self.expected_account and self.expected_account != actual_id:
            raise RuntimeError("Credentials do not match the account ID you entered; no order sent.")
        if self.state["account_id"] not in (None, actual_id):
            raise RuntimeError("Saved trades belong to another account; use its original folder.")
        if account.get("status") != "ACTIVE" or any(account.get(k) for k in
                ("account_blocked", "trading_blocked", "trade_suspended_by_user")):
            raise RuntimeError("Broker account is not active for trading.")
        finite(account["equity"], "account equity", positive=True)
        finite(account["cash"], "account cash")
        first_bind = self.state["account_id"] is None
        self.state["account_id"] = actual_id
        if first_bind and self.api.mode == "live" and not self.api.execute:
            self.log(f"Live account UUID: {actual_id}. Enter this locally to bind live order execution.")
        self.save()
        self.reconcile()
        clock = self.api.call("/v2/clock")
        if not clock["is_open"]:
            self.log(f"Market closed. Existing DAY orders may expire. Next open: {clock.get('next_open', 'check dashboard')}.")
            return
        now = stamp(clock["timestamp"])
        day = now.astimezone(ny_zone()).date().isoformat()
        positions = self.fresh_positions() if self.api.execute else self.api.call("/v2/positions")
        self.log(f"{self.api.mode.upper()} account: {len(positions)} holdings; cash ${float(account['cash']):.2f}.")
        open_orders = self.api.call("/v2/orders", params={"status": "open", "limit": 500})
        if self.api.execute and any(x.get("client_order_id") not in self.state["pending"] for x in open_orders):
            raise RuntimeError("An unrelated order is open. Use a dedicated account.")
        for position in positions:
            if position["symbol"] in self.state["trades"]:
                self.manage(position["symbol"], position, now)
        self.update_research(now)
        # Balances may have changed while pending fills were being reconciled.
        refreshed_account = self.api.call("/v2/account")
        self.entries(refreshed_account, positions, day, now)
        self.save()

    def run(self, *, once=False):
        with InstanceLock(self.folder / "instance.lock"):
            self.state = self.read_state()
            account = self.api.call("/v2/account")
            # An account-wide OS lock also catches copies using different
            # result folders on the same computer. This is read-only so far.
            with InstanceLock(account_lock_path(self.api.mode, account["id"])):
                self.log(f"{self.api.mode.upper()} | {'ORDERS ENABLED' if self.api.execute else 'READ-ONLY PREVIEW'} | ${self.rules.dollars_each:.2f} per position, ${self.rules.capital_cap:.2f} cap.")
                while not self.stop.is_set() and not (self.folder / "STOP_BOT").exists():
                    try:
                        self.cycle()
                    except (RuntimeError, ValueError, KeyError, TypeError) as error:
                        self.log(f"PAUSED: {error}")
                        raise
                    if once:
                        break
                    self.stop.wait(self.rules.poll_seconds)
                self.log("Stopped. Existing broker orders and positions remain; fractional DAY exits expire.")


def main():
    parser = argparse.ArgumentParser(description="Five-position plateau bot. Live defaults to read-only; live learning is off by default.")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Invented five-stock paths and learning evaluation; no network.")
    demo.add_argument("--output", default=str(ROOT / "adaptive_demo"))
    learn = sub.add_parser("learn", help="Evaluate a JSON list of completed observed episodes.")
    learn.add_argument("episodes")
    learn.add_argument("--output", default=str(ROOT / "learning_report.json"))
    run = sub.add_parser("run", help="Connect to your broker with credentials kept on your computer.")
    run.add_argument("--mode", choices=("paper", "live"), default="paper")
    run.add_argument("--execute", action="store_true", help="Enable automatic orders; with live this uses REAL MONEY.")
    run.add_argument("--account-id", help="Exact live account UUID, not the public account number.")
    run.add_argument("--enable-live-learning", action="store_true", help="Allow bounded exit updates after research gates; does not change budget/loss limits.")
    run.add_argument("--once", action="store_true")
    run.add_argument("--folder")
    args = parser.parse_args()
    try:
        rules = load_rules()
        if args.command == "demo":
            offline_demo(rules, args.output)
        elif args.command == "learn":
            report = learning_report(json.loads(Path(args.episodes).read_text()), rules)
            atomic_json(args.output, report)
            print(json.dumps(report, indent=2))
        else:
            if args.enable_live_learning:
                if args.mode != "live" or not args.execute:
                    raise ValueError("Live learning requires live execution selected explicitly.")
                rules.auto_adapt_live = True
            api = TradingAPI(os.getenv("APCA_API_KEY_ID", ""), os.getenv("APCA_API_SECRET_KEY", ""),
                             mode=args.mode, execute=args.execute)
            folder = args.folder or str(ROOT / "results" / (args.mode + ("" if args.execute else "-preview")))
            BasketBot(api, rules, folder, account_id=args.account_id).run(once=args.once)
    except KeyboardInterrupt:
        print("Stopped. Existing orders and holdings remain in the broker account.")
    except (RuntimeError, ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
