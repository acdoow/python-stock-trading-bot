"""Observed intraday price growth for entry screening, never a price forecast."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Momentum:
    # Old configurations without this section retain their list-only behavior.
    # The supplied rules.json explicitly enables this filter.
    enabled: bool = False
    window_bars: int = 6
    min_session_gain_fraction: float = 0.005
    min_window_gain_fraction: float = 0.003
    min_rising_step_fraction: float = 0.6
    max_pullback_fraction: float = 0.003

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("The momentum switch must be true or false.")
        if type(self.window_bars) is not int or not 3 <= self.window_bars <= 36:
            raise ValueError("The growth window must contain 3 to 36 completed bars.")
        for name in ("min_session_gain_fraction", "min_window_gain_fraction"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(float(value)) or not 0 < float(value) <= 0.5:
                raise ValueError(f"{name} must be above zero and no more than 0.5.")
        if (isinstance(self.min_rising_step_fraction, bool)
                or not math.isfinite(float(self.min_rising_step_fraction))
                or not 0 < self.min_rising_step_fraction <= 1):
            raise ValueError("The required rising-step fraction must be above zero and at most one.")
        if (isinstance(self.max_pullback_fraction, bool)
                or not math.isfinite(float(self.max_pullback_fraction))
                or not 0 <= self.max_pullback_fraction <= 0.1):
            raise ValueError("The permitted recent-high pullback must be between zero and 10%.")
        return self


def bar_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Growth observations need timezone-aware timestamps.")
    return result


def momentum_signal(bars, bid, now, session_open, bar_minutes, settings):
    """Require completed, consecutive current-session observations and a fresh bid.

    Quote age/spread checks belong to the broker adapter. This function never
    uses an unfinished bar, a previous day's opening price or future data.
    Current and completed-window growth must both pass, so a fresh reversal
    cannot reuse a formerly eligible trend.
    """
    settings.validate()
    signal = {"eligible": False, "reason": "Collecting opening-session observations.",
              "window_minutes": settings.window_bars * bar_minutes}
    if not settings.enabled:
        return {**signal, "eligible": True, "reason": "Configured-list entry selected."}
    if (now.tzinfo is None or session_open.tzinfo is None or now < session_open
            or now >= session_open + timedelta(minutes=390)):
        signal["reason"] = "Waiting for the regular trading session."
        return signal
    bid = float(bid)
    if not math.isfinite(bid) or bid <= 0:
        raise ValueError("The growth check needs a positive, finite bid.")
    interval = timedelta(minutes=bar_minutes)
    completed = []
    for bar in bars:
        when = bar_time(bar["t"])
        if when < session_open or when + interval > now:
            continue
        values = {key: float(bar[key]) for key in ("o", "h", "l", "c")}
        if any(not math.isfinite(value) or value <= 0 for value in values.values()):
            raise ValueError("Growth observations contain an invalid price.")
        if not values["l"] <= min(values["o"], values["c"]) <= max(values["o"], values["c"]) <= values["h"]:
            raise ValueError("Growth observations contain inconsistent prices.")
        if (when - session_open).total_seconds() % interval.total_seconds():
            signal["reason"] = "Waiting for aligned trading-session bars."
            return signal
        completed.append((when, values))
    completed.sort(key=lambda item: item[0])
    if not completed or completed[0][0] != session_open:
        signal["reason"] = "The opening-session price bar is missing."
        return signal
    if len({when for when, _ in completed}) != len(completed):
        signal["reason"] = "Duplicate price bars need to be resolved."
        return signal
    if len(completed) < settings.window_bars:
        signal["reason"] = f"Waiting for {settings.window_bars} completed price bars."
        return signal
    window = completed[-settings.window_bars:]
    if any(b[0] - a[0] != interval for a, b in zip(window, window[1:])):
        signal["reason"] = "The recent growth window has missing bars."
        return signal
    if now - window[-1][0] > 2 * interval:
        signal["reason"] = "The latest completed growth bar is stale."
        return signal
    opening_price = completed[0][1]["o"]
    window_open = window[0][1]["o"]
    last_close = window[-1][1]["c"]
    high = max(values["h"] for _, values in window)
    rising = sum(b[1]["c"] > a[1]["c"] for a, b in zip(window, window[1:]))
    steps = len(window) - 1
    required = math.ceil(steps * settings.min_rising_step_fraction - 1e-12)
    session_gain = bid / opening_price - 1
    window_gain = bid / window_open - 1
    completed_gain = last_close / window_open - 1
    pullback = max(0.0, (high - bid) / high)
    signal.update(session_gain_fraction=session_gain, window_gain_fraction=window_gain,
                  completed_window_gain_fraction=completed_gain, rising_steps=rising,
                  total_steps=steps, required_rising_steps=required,
                  pullback_fraction=pullback, latest_bar_at=window[-1][0].isoformat())
    if session_gain + 1e-12 < settings.min_session_gain_fraction:
        signal["reason"] = (f"Session growth {session_gain:.2%} is below "
                            f"{settings.min_session_gain_fraction:.2%}.")
    elif min(window_gain, completed_gain) + 1e-12 < settings.min_window_gain_fraction:
        signal["reason"] = (f"Recent growth {min(window_gain, completed_gain):.2%} is below "
                            f"{settings.min_window_gain_fraction:.2%}.")
    elif rising < required:
        signal["reason"] = f"Only {rising}/{steps} steps rose; require {required}/{steps}."
    elif pullback > settings.max_pullback_fraction + 1e-12:
        signal["reason"] = (f"Price pulled back {pullback:.2%} from the recent high; "
                            f"limit is {settings.max_pullback_fraction:.2%}.")
    else:
        signal.update(eligible=True,
                      reason=(f"Session +{session_gain:.2%}, recent +{window_gain:.2%}; "
                              f"{rising}/{steps} rising steps."))
    return signal
