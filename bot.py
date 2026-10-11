"""StockBot: a dependency-free, paper-only trading starter. Python 3.10+."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


@dataclass
class Config:
    symbols: tuple[str, ...] = ("SPY",)
    fast_days: int = 5
    slow_days: int = 20
    dollars_per_buy: float = 100.0
    max_total_exposure: float = 300.0
    max_positions: int = 3
    stop_loss_fraction: float = 0.03
    take_profit_fraction: float = 0.06
    daily_loss_dollars: float = 20.0
    poll_seconds: int = 60
    max_price_age_seconds: int = 300

    def validate(self):
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Choose distinct stock symbols.")
        if any(not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", s) for s in self.symbols):
            raise ValueError("Use uppercase stock symbols, such as SPY.")
        for name in ("fast_days", "slow_days", "max_positions", "poll_seconds", "max_price_age_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if not 1 < self.fast_days < self.slow_days <= 200 or self.poll_seconds < 15:
            raise ValueError("Use 1 < fast_days < slow_days <= 200 and poll_seconds >= 15.")
        for name in ("dollars_per_buy", "max_total_exposure", "daily_loss_dollars"):
            if isinstance(getattr(self, name), bool) or not math.isfinite(float(getattr(self, name))) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive finite number.")
        if self.dollars_per_buy < 1 or self.max_total_exposure < self.dollars_per_buy:
            raise ValueError("Buy amount must be >= $1 and <= total exposure limit.")
        for name in ("stop_loss_fraction", "take_profit_fraction"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between zero and one.")
        return self


def load_config(path=ROOT / "config.json"):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data["symbols"] = tuple(data["symbols"])
    return Config(**data).validate()


def crossover(closes, cfg):
    """Decide from completed closes only; never treat an unfinished day as data."""
    if len(closes) < cfg.slow_days + 1:
        return "hold"
    values = [float(x) for x in closes]
    if any(not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError("Prices must be positive and finite.")
    fast_before = sum(values[-cfg.fast_days - 1:-1]) / cfg.fast_days
    slow_before = sum(values[-cfg.slow_days - 1:-1]) / cfg.slow_days
    fast_now = sum(values[-cfg.fast_days:]) / cfg.fast_days
    slow_now = sum(values[-cfg.slow_days:]) / cfg.slow_days
    if fast_before <= slow_before and fast_now > slow_now:
        return "buy"
    if fast_before >= slow_before and fast_now < slow_now:
        return "sell"
    return "hold"


def exit_reason(price, entry, cfg):
    if price <= entry * (1 - cfg.stop_loss_fraction):
        return "stop loss"
    if price >= entry * (1 + cfg.take_profit_fraction):
        return "profit target"
    return None


def entry_budget(cfg, cash, positions, pending_buys):
    reserved = sum(float(o["amount"]) for o in pending_buys)
    symbols = {p["symbol"] for p in positions} | {o["symbol"] for o in pending_buys}
    if len(symbols) >= cfg.max_positions:
        return 0.0
    exposure = sum(abs(float(p["market_value"])) for p in positions) + reserved
    amount = min(cfg.dollars_per_buy, float(cash) - reserved - 1,
                 cfg.max_total_exposure - exposure)
    return max(0.0, math.floor((amount + 1e-9) * 100) / 100)


class APIError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirects(HTTPRedirectHandler):
    # Never forward secret headers to a redirected host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AlpacaPaper:
    """Fixed paper endpoint. There is deliberately no live-money mode."""
    def __init__(self, key, secret):
        if not key or not secret:
            raise ValueError("Enter your Alpaca PAPER API key and secret on your computer.")
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                        "Content-Type": "application/json"}
        self.opener = build_opener(NoRedirects())

    def call(self, path, *, data_api=False, params=None, payload=None):
        base = DATA_URL if data_api else PAPER_URL
        url = base + path + ("?" + urlencode(params) if params else "")
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(url, data=body, headers=self.headers,
                          method="POST" if payload is not None else "GET")
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            # Avoid printing request headers, credentials or response bodies.
            advice = {401: "Check your PAPER credentials.", 403: "Check account/data permissions.",
                      429: "Rate limit reached; wait before restarting."}.get(error.code, "Check your Alpaca paper dashboard.")
            raise APIError(error.code, f"Alpaca HTTP {error.code}. {advice}") from None
        except (URLError, TimeoutError, OSError):
            raise APIError(None, "Network request failed; no automatic order retry.") from None

    def daily_bars(self, symbol, cfg, session_day):
        params = {"symbols": symbol, "timeframe": "1Day", "feed": "iex", "adjustment": "split",
                  "start": (session_day - timedelta(days=cfg.slow_days * 3 + 30)).isoformat(),
                  "end": session_day.isoformat(), "limit": 10000, "sort": "asc"}
        bars = []
        while True:
            response = self.call("/v2/stocks/bars", data_api=True, params=params)
            bars.extend(response.get("bars", {}).get(symbol, []))
            token = response.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        # Daily bars are timestamped by session date. Explicitly exclude today.
        return sorted((b for b in bars if b["t"][:10] < session_day.isoformat()), key=lambda b: b["t"])

    def price(self, symbol, now, cfg):
        trade = self.call(f"/v2/stocks/{symbol}/trades/latest", data_api=True,
                          params={"feed": "iex"})["trade"]
        stamp = datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))
        age = (now - stamp).total_seconds()
        price = float(trade["p"])
        if not -5 <= age <= cfg.max_price_age_seconds or not math.isfinite(price) or price <= 0:
            raise ValueError(f"{symbol}: no sufficiently recent valid price; skipping.")
        return price


class InstanceLock:
    """Operating-system lock released even after a crash, on Windows and Unix."""
    def __init__(self, path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.file = open(self.path, "a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another bot holds this instance lock. Run one bot per account.") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class PaperBot:
    def __init__(self, api, cfg, folder, emit=print, stop=None):
        self.api, self.cfg, self.emit = api, cfg.validate(), emit
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.stop = stop or threading.Event()
        self.state_path = self.folder / "paper_state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "account_id": None, "managed": [], "pending": {}, "seen": {},
            "day": None, "day_equity": None, "halted_day": None}

    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def log(self, message):
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {message}"
        with (self.folder / "paper_log.txt").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        self.emit(line)

    def reconcile(self):
        for client_id in list(self.state["pending"]):
            try:
                order = self.api.call("/v2/orders:by_client_order_id", params={"client_order_id": client_id})
            except APIError as error:
                if error.status == 404:
                    raise RuntimeError("An order submission is unconfirmed. New orders are paused. "
                                       "Check paper_state.json and the Alpaca dashboard; read README recovery steps.") from None
                raise
            if order["status"] in TERMINAL:
                self.log(f"Order {client_id}: {order['status']}; filled quantity {order.get('filled_qty', '0')}.")
                del self.state["pending"][client_id]
                self.save()

    def submit(self, symbol, side, day, reason, *, amount=None, qty=None):
        if self.stop.is_set() or (self.folder / "STOP_BOT").exists():
            return
        identity = f"{day}:{symbol}:{side}:{reason}"
        client_id = "stockbot-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        payload = {"symbol": symbol, "side": side, "type": "market",
                   "time_in_force": "day", "client_order_id": client_id}
        if side == "buy":
            payload["notional"] = f"{amount:.2f}"
        else:
            payload["qty"] = str(qty)
        intent = {"symbol": symbol, "side": side, "amount": amount or 0, "reason": reason}
        # Journal before sending. If the POST response is lost, look up the same
        # client ID on restart instead of blindly placing the trade again.
        self.state["pending"][client_id] = intent
        self.state["seen"][f"{symbol}:{side}"] = day.isoformat()
        if side == "buy" and symbol not in self.state["managed"]:
            self.state["managed"].append(symbol)
        self.save()
        order = self.api.call("/v2/orders", payload=payload)
        self.log(f"Submitted PAPER {side.upper()} {symbol}: {reason}; broker status {order['status']}.")

    def cycle(self):
        self.reconcile()
        clock = self.api.call("/v2/clock")
        if not clock["is_open"]:
            self.log(f"Market closed. Next open: {clock.get('next_open', 'see broker dashboard')}.")
            return
        session_day = date.fromisoformat(clock["timestamp"][:10])
        account = self.api.call("/v2/account")
        if account.get("status") != "ACTIVE" or any(account.get(k) for k in
                ("trading_blocked", "account_blocked", "trade_suspended_by_user")):
            raise RuntimeError("Paper account is not active for trading.")
        if self.state["account_id"] not in (None, account["id"]):
            raise RuntimeError("These saved orders belong to a different paper account. Use a new folder.")
        self.state["account_id"] = account["id"]
        equity = float(account["equity"])
        if not math.isfinite(equity) or equity <= 0 or not math.isfinite(float(account["cash"])):
            raise ValueError("Invalid account balances; no orders submitted.")
        if self.state["day"] != session_day.isoformat():
            self.state.update(day=session_day.isoformat(), day_equity=equity, halted_day=None)
        daily_change = equity - self.state["day_equity"]
        if daily_change <= -self.cfg.daily_loss_dollars:
            self.state["halted_day"] = session_day.isoformat()
        self.save()
        positions = self.api.call("/v2/positions")
        if any(float(p["qty"]) < 0 for p in positions):
            raise RuntimeError("Use a dedicated paper account without short positions.")
        open_orders = self.api.call("/v2/orders", params={"status": "open", "limit": 500})
        if any(o.get("client_order_id") not in self.state["pending"] for o in open_orders):
            raise RuntimeError("An unrelated order is open. Use a dedicated paper account.")
        self.log(f"Paper equity ${equity:,.2f}; change since today's first check ${daily_change:+,.2f}."
                 + (" New buys paused by loss limit." if self.state["halted_day"] else ""))
        calendar = self.api.call("/v2/calendar", params={
            "start": (session_day - timedelta(days=14)).isoformat(), "end": session_day.isoformat()})
        previous_sessions = sorted(s["date"] for s in calendar if s["date"] < session_day.isoformat())
        if not previous_sessions:
            raise RuntimeError("Could not determine the previous trading session.")
        expected_date = previous_sessions[-1]
        # Continue monitoring owned positions even if removed from the watchlist.
        symbols = list(dict.fromkeys(list(self.cfg.symbols) + self.state["managed"]))
        by_symbol = {p["symbol"]: p for p in positions}
        for symbol in symbols:
            if self.stop.is_set() or (self.folder / "STOP_BOT").exists():
                return
            if any(o["symbol"] == symbol for o in self.state["pending"].values()):
                continue
            try:
                price = self.api.price(symbol, datetime.now(timezone.utc), self.cfg)
            except ValueError as error:
                self.log(str(error))
                continue
            position = by_symbol.get(symbol)
            reason = None
            if position and symbol in self.state["managed"]:
                reason = exit_reason(price, float(position["avg_entry_price"]), self.cfg)
            if reason:
                # Protective exits do not depend on a successful history download.
                if self.state["seen"].get(f"{symbol}:sell") != session_day.isoformat():
                    self.submit(symbol, "sell", session_day, reason, qty=position["qty"])
                continue
            bars = self.api.daily_bars(symbol, self.cfg, session_day)
            if len(bars) < self.cfg.slow_days + 1 or bars[-1]["t"][:10] != expected_date:
                self.log(f"{symbol}: daily history missing or stale; no trend order.")
                continue
            signal = crossover([b["c"] for b in bars], self.cfg)
            self.log(f"{symbol}: ${price:.2f}, {signal.upper()} trend signal from {expected_date}.")
            if position and symbol in self.state["managed"] and signal == "sell":
                if self.state["seen"].get(f"{symbol}:sell") != session_day.isoformat():
                    self.submit(symbol, "sell", session_day, "trend crossed down", qty=position["qty"])
            elif not position and symbol in self.cfg.symbols and signal == "buy":
                if self.state["halted_day"] or self.state["seen"].get(f"{symbol}:buy") == session_day.isoformat():
                    continue
                asset = self.api.call(f"/v2/assets/{symbol}")
                if not (asset.get("tradable") and asset.get("fractionable") and asset.get("status") == "active"):
                    self.log(f"{symbol}: not eligible for fractional paper trading.")
                    continue
                pending_buys = [o for o in self.state["pending"].values() if o["side"] == "buy"]
                amount = entry_budget(self.cfg, account["cash"], positions, pending_buys)
                if amount >= 1:
                    self.submit(symbol, "buy", session_day, "trend crossed up", amount=amount)
                else:
                    self.log(f"{symbol}: spending or position limit prevents a buy.")

    def run(self, *, once=False):
        with InstanceLock(self.folder / "paper_instance.lock"):
            # Read under the instance lock, in case another run has just ended.
            if self.state_path.exists():
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.log("PAPER ONLY. Stop loss and target are checked by this running process.")
            while not self.stop.is_set() and not (self.folder / "STOP_BOT").exists():
                try:
                    self.cycle()
                except (APIError, ValueError, RuntimeError, KeyError, TypeError) as error:
                    self.log(f"PAUSED: {error}")
                    raise
                if once:
                    break
                self.stop.wait(self.cfg.poll_seconds)
            self.log("Bot stopped. Existing orders/positions remain in the paper account.")


def demo_bars(scenario="mixed", count=180):
    """Invented prices only. Not a historical return or prediction."""
    rng = random.Random(41)
    start, previous = date(2025, 1, 2), 100.0
    rows = []
    offset = 0
    while len(rows) < count:
        day = start + timedelta(days=offset)
        offset += 1
        if day.weekday() >= 5:
            continue
        i = len(rows)
        drift = {"rising": 0.003, "falling": -0.003,
                 "mixed": 0.003 * math.sin(i / 10)}[scenario]
        opening = previous * (1 + rng.uniform(-0.004, 0.004))
        close = max(1, opening * (1 + drift + rng.uniform(-0.012, 0.012)))
        rows.append({"date": day.isoformat(), "open": opening, "close": close,
                     "high": max(opening, close) * 1.004, "low": min(opening, close) * 0.996})
        previous = close
    return rows


def load_bars(path):
    with open(path, newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError("CSV is empty.")
    previous_day = None
    for row in rows:
        day = date.fromisoformat(row["date"])
        if previous_day is not None and day <= previous_day:
            raise ValueError("CSV dates must be unique and in increasing order.")
        previous_day = day
        for key in ("open", "high", "low", "close"):
            row[key] = float(row[key])
            if not math.isfinite(row[key]) or row[key] <= 0:
                raise ValueError("CSV prices must be positive and finite.")
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]) or row["high"] < row["low"]:
            raise ValueError("Invalid OHLC prices.")
    return rows


def backtest(rows, cfg, output, emit=print, initial_cash=1000.0, slippage_bps=5.0, fee=0.05):
    cfg.validate()
    cash, qty, entry, closes = initial_cash, 0.0, 0.0, []
    peak, max_drawdown = initial_cash, 0.0
    trades, curve = [], []
    slip = slippage_bps / 10000
    wins, closed, pnl_total, purchase_cost = 0, 0, 0.0, 0.0
    for row in rows:
        # The signal uses yesterday's close. Execute at today's opening price.
        signal = crossover(closes, cfg)
        opening = row["open"]
        reason, sell_price = None, opening
        if qty:
            stop_price = entry * (1 - cfg.stop_loss_fraction)
            target = entry * (1 + cfg.take_profit_fraction)
            if opening <= stop_price:
                reason = "stop gap"
            elif opening >= target:
                reason, sell_price = "profit target", target
            elif signal == "sell":
                reason = "trend crossed down"
        if not qty and signal == "buy":
            amount = min(cfg.dollars_per_buy, cfg.max_total_exposure, cash - fee - 1)
            if amount >= 1:
                entry = opening * (1 + slip)
                qty, purchase_cost = amount / entry, amount + fee
                cash -= purchase_cost
                trades.append([row["date"], "buy", qty, entry, fee, "trend crossed up", ""])
                emit(f"{row['date']}  DEMO BUY {qty:.4f} shares at ${entry:.2f}")
        if qty and reason is None:
            stop_price, target = entry * (1 - cfg.stop_loss_fraction), entry * (1 + cfg.take_profit_fraction)
            # If both thresholds occur in one day, assume the adverse one first.
            if row["low"] <= stop_price:
                reason, sell_price = "stop loss", min(opening, stop_price)
            elif row["high"] >= target:
                reason, sell_price = "profit target", target
        if qty and reason:
            fill = sell_price * (1 - slip)
            proceeds = qty * fill - fee
            pnl = proceeds - purchase_cost
            cash += proceeds
            trades.append([row["date"], "sell", qty, fill, fee, reason, pnl])
            emit(f"{row['date']}  DEMO SELL at ${fill:.2f}: {reason}; P/L ${pnl:+.2f}")
            wins += pnl > 0
            closed += 1
            pnl_total += pnl
            qty, entry, purchase_cost = 0.0, 0.0, 0.0
        equity = cash + qty * row["close"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        curve.append([row["date"], equity, cash, qty * row["close"]])
        closes.append(row["close"])
    if not rows:
        raise ValueError("No prices supplied.")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    summary = {"data": "provided OHLC data; demo data are synthetic", "starting_virtual_cash": initial_cash,
               "ending_equity": equity, "return_percent": (equity / initial_cash - 1) * 100,
               "max_close_to_close_drawdown_percent": max_drawdown * 100, "closed_trades": closed,
               "winning_closed_trades": wins, "realized_profit_loss": pnl_total,
               "open_position_quantity": qty, "slippage_bps_each_side": slippage_bps,
               "assumed_fee_each_order": fee,
               "note": "Illustration only. Fees, gaps and fills are simplified. Not evidence of future profit."}
    for filename, header, records in (("trades.csv", ["date", "side", "quantity", "fill_price", "fee", "reason", "realized_pnl"], trades),
                                      ("equity.csv", ["date", "equity", "cash", "position_value"], curve)):
        with (output / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(records)
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    emit(f"Ending virtual equity ${equity:,.2f}; return {summary['return_percent']:+.2f}%; {closed} closed trades.")
    return summary


def main():
    parser = argparse.ArgumentParser(description="StockBot — offline demo and Alpaca PAPER trading only.")
    sub = parser.add_subparsers(dest="mode", required=True)
    demo = sub.add_parser("demo", help="Use invented prices; no API keys needed.")
    demo.add_argument("--scenario", choices=("mixed", "rising", "falling"), default="mixed")
    demo.add_argument("--output", default=str(ROOT / "demo_results"))
    historical = sub.add_parser("backtest", help="Use your CSV: date,open,high,low,close.")
    historical.add_argument("csv")
    historical.add_argument("--output", default=str(ROOT / "backtest_results"))
    paper = sub.add_parser("paper", help="Automatic virtual-money orders; computer must stay on.")
    paper.add_argument("--once", action="store_true", help="Perform one check, then stop.")
    paper.add_argument("--folder", default=str(ROOT / "paper_results"))
    args = parser.parse_args()
    try:
        cfg = load_config()
        if args.mode == "demo":
            backtest(demo_bars(args.scenario), cfg, args.output)
        elif args.mode == "backtest":
            backtest(load_bars(args.csv), cfg, args.output)
        else:
            api = AlpacaPaper(os.getenv("APCA_API_KEY_ID", ""), os.getenv("APCA_API_SECRET_KEY", ""))
            PaperBot(api, cfg, args.folder).run(once=args.once)
    except KeyboardInterrupt:
        print("Stopped. Existing paper positions and orders remain open.")
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
