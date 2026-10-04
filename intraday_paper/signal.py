"""Pure signal + exit-math for the 'open==low green 5-min bar' strategy.

Everything here is a pure function over finalized bar dicts — NO client, NO
network, NO io — so it is fully unit-testable and lookahead-free when fed bars
in chronological order.
"""
from __future__ import annotations

import math

DEFAULT_SL_POINTS = 2.0          # initial stop below signal bar low (index options)
DEFAULT_COST_POINTS = 2.0        # round-trip cost netted per trade, in points
DEFAULT_PRIOR_LOOKBACK = 20      # bars before the signal bar to source resistance
DEFAULT_THREE_BAR = 3            # '3-bar profit' exit count


# ============================== trigger ==============================
def detect_signal(bar: dict, epsilon: float = 1e-9) -> dict:
    """Detect the 'open==low green' trigger on a finalized bar.

    green = close > open, and open == low within `epsilon`.
    Returns a dict with `triggered`, plus `signal_bar_close`/`signal_bar_low`
    when the pattern fires.
    """
    if not (bar["close"] > bar["open"]):
        return {"triggered": False}
    if abs(bar["open"] - bar["low"]) > epsilon:
        return {"triggered": False}
    return {
        "triggered": True,
        "signal_bar_close": bar["close"],
        "signal_bar_low": bar["low"],
        "bar": bar,
    }


# ============================== geometry ==============================
def initial_stop(low: float, sl_points: float = DEFAULT_SL_POINTS) -> float:
    """SL = signal-bar low minus `sl_points` (CE index options, default 2.0)."""
    return low - sl_points


def compute_net_pnl(entry: float, exit: float,
                    cost_in_points: float = DEFAULT_COST_POINTS) -> float:
    """Net P&L in points: gross (exit-entry) minus round-trip cost."""
    return (exit - entry) - cost_in_points


def sample_prior_resistance(prior_bars) -> float | None:
    """Highest high among finalized bars before the signal bar (prior resistance)."""
    if not prior_bars:
        return None
    return max(b["high"] for b in prior_bars)


# ============================== exit modes ==============================
def _exit(mode, entry, sl_price, target, exit_price, bar_count):
    net = compute_net_pnl(entry, exit_price) if exit_price is not None else None
    return {
        "mode": mode,
        "exit_price": exit_price,
        "net_pnl": net,
        "sl_price": sl_price,
        "target": target,
        "bar_count": bar_count,
    }


def simulate_exits(signal: dict, prior_bars, later_bars,
                   cost_in_points: float = DEFAULT_COST_POINTS,
                   sl_points: float = DEFAULT_SL_POINTS,
                   prior_lookback: int = DEFAULT_PRIOR_LOOKBACK,
                   three_bar_count: int = DEFAULT_THREE_BAR) -> dict:
    """Evaluate the three exit modes over `later_bars` (bars finalized after
    the signal bar), in chronological order.

    Each mode is simulated independently so results can be compared:

      (a) prior_resistance — exit at prior swing high (resistance) when hit
      (b) trail            — ratchet SL to each new bar low; exit when price
                              falls back to the trailed stop
      (c) three_bar        — take profit at the close of the Nth bar after the
                              signal if it is green (in profit)

    `prior_bars` are the FINALIZED bars that came before the signal bar — they
    are only used to compute the prior-resistance target, never touched again.
    `later_bars` must be fed in chronological order; each mode advances
    bar-by-bar and never peeks past the bar it is currently evaluating.
    """
    entry = signal["close"]
    stop = initial_stop(signal["low"], sl_points)
    target = sample_prior_resistance(prior_bars[-prior_lookback:]) \
        if prior_lookback else sample_prior_resistance(prior_bars)

    # (a) prior resistance
    res_exit = None
    res_bars = 0
    if target is not None:
        for i, b in enumerate(later_bars):
            if b.get("high", -math.inf) >= target:
                res_exit = target
                res_bars = i + 1
                break

    # (b) trail
    trail = stop
    trail_exit = None
    trail_bars = 0
    for i, b in enumerate(later_bars):
        if b["low"] <= trail:
            trail_exit = trail
            trail_bars = i + 1
            break
        trail = max(trail, b["low"])   # ratchet up to each new bar low

    # (c) three-bar profit
    tb_exit = None
    tb_bars = 0
    for i, b in enumerate(later_bars):
        if i + 1 >= three_bar_count and b["close"] > entry:
            tb_exit = b["close"]
            tb_bars = i + 1
            break

    return {
        "prior_resistance": _exit("prior_resistance", entry, stop, target,
                                  res_exit, res_bars),
        "trail": _exit("trail", entry, stop, None, trail_exit, trail_bars),
        "three_bar": _exit("three_bar", entry, stop, None, tb_exit, tb_bars),
    }