"""Paper-trade state machine: on 'open==low green' signal, record entry, then
track the three exit modes to compare; persist every signal + outcome to a JSONL
ledger.

NO orders — this only books paper fills against finalized 5-min bars. The exit
math is delegated to intraday_paper.signal (pure), keeping the state machine
thin and lookahead-free (it never sees a bar before that bar is finalized).
"""
from __future__ import annotations

import json
import os
import time


def write_ledger_line(path: str, record: dict) -> None:
    """Append one JSON record per line to `path` (mkdir -p parent)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


class PaperTracker:
    def __init__(self, ledger_path: str = "intraday_paper/ledger.jsonl",
                 cost_in_points: float = 2.0,
                 sl_points: float = 2.0,
                 prior_lookback: int = 20,
                 three_bar_count: int = 3):
        self.ledger_path = ledger_path
        self.cost_in_points = cost_in_points
        self.sl_points = sl_points
        self.prior_lookback = prior_lookback
        self.three_bar_count = three_bar_count
        self.history = {}      # sym -> list of finalized bars (chronological)
        self.trades = {}       # sym -> open PaperTrade or None

    # ---- public feed: called once per finalized 5-min bar ----
    def on_finalized_bar(self, bar: dict, now_ts: float | None = None) -> list:
        """Process one finalized bar. Returns event dicts this bar produced
        (e.g. {'event': 'signal'/'exit', ...}); may be empty."""
        sym = bar["sym"]
        events = []
        self.history.setdefault(sym, []).append(bar)

        now = now_ts if now_ts is not None else time.time()

        trade = self.trades.get(sym)
        if trade is not None:
            ev = trade.on_bar(bar, now)
            if ev:
                events.append(ev)
                if ev["event"] == "exit":
                    self.trades[sym] = None    # trade closed
                    self._log_exit(ev)
            return events

        # no open trade: check the trigger on the just-finalized bar
        from intraday_paper.signal import detect_signal
        res = detect_signal(bar)
        if res["triggered"]:
            prior = list(self.history.get(sym, []))[:-1]   # bars BEFORE the signal
            trade = PaperTrade(sym=sym, signal_bar=res["bar"],
                               prior_bars=prior,
                               cost_in_points=self.cost_in_points,
                               sl_points=self.sl_points,
                               prior_lookback=self.prior_lookback,
                               three_bar_count=self.three_bar_count)
            self.trades[sym] = trade
            ev = trade.open_event(now)
            events.append(ev)
            self._log_signal(ev)
        return events

    def on_batch(self, bars: list, now_ts: float | None = None) -> list:
        """Process a batch of finalized bars (one per symbol from a poll cycle)."""
        evs = []
        for b in bars:
            evs.extend(self.on_finalized_bar(b, now_ts))
        return evs

    # ---- ledger persistence (paper fills only) ----
    def _log_signal(self, ev):
        record = {"t": ev["t"], "event": "signal", "sym": ev["sym"],
                  "slot": ev["slot"], "entry": ev["entry"],
                  "signal_low": ev["signal_low"],
                  "sl": ev["sl"], "sl_points": self.sl_points,
                  "cost_points": self.cost_in_points}
        write_ledger_line(self.ledger_path, record)

    def _log_exit(self, ev):
        write_ledger_line(self.ledger_path, ev)

    # ---- status ----
    def open_count(self) -> int:
        return sum(1 for t in self.trades.values() if t is not None)

    def symbol_finalized_bars(self, sym):
        return list(self.history.get(sym, []))


class PaperTrade:
    """One paper long on an ATM index option, seeded by an open==low green bar.

    Entry = signal-bar close. SL = signal low - sl_points. It then sits on
    every subsequently finalized bar and reports the three exit modes when the
    earliest of them triggers (simulated independently for comparison).
    """

    def __init__(self, sym, signal_bar, prior_bars, cost_in_points, sl_points,
                 prior_lookback, three_bar_count):
        self.sym = sym
        self.signal_bar = signal_bar
        self.signal_low = signal_bar["low"]
        self.entry = signal_bar["close"]
        self.later_bars = []
        self.sl_points = sl_points
        self.cost_in_points = cost_in_points
        self.prior_lookback = prior_lookback
        self.three_bar_count = three_bar_count
        self._prior = prior_bars
        self.last_modes = None

    def open_event(self, t):
        from intraday_paper.signal import initial_stop
        return {"t": t, "event": "signal", "sym": self.sym,
                "slot": self.signal_bar["slot"],
                "entry": self.entry, "signal_low": self.signal_low,
                "sl": initial_stop(self.signal_low, self.sl_points),
                "sl_points": self.sl_points,
                "cost_points": self.cost_in_points}

    def on_bar(self, bar, t):
        """Return (and close) the earliest-triggering exit, or None."""
        self.later_bars.append(bar)
        from intraday_paper.signal import simulate_exits
        modes = simulate_exits(self.signal_bar, self._prior, self.later_bars,
                               cost_in_points=self.cost_in_points,
                               sl_points=self.sl_points,
                               prior_lookback=self.prior_lookback,
                               three_bar_count=self.three_bar_count)
        self.last_modes = modes

        # earliest exit by bar_count among modes that have exited
        exited = {k: m for k, m in modes.items() if m["exit_price"] is not None}
        if not exited:
            return None
        k = min(exited, key=lambda kk: exited[kk]["bar_count"])
        m = exited[k]
        return {"t": t, "event": "exit", "sym": self.sym,
                "slot": bar["slot"], "mode": k,
                "entry": self.entry, "exit_price": m["exit_price"],
                "net_pnl": m["net_pnl"], "cost_points": self.cost_in_points,
                "modes": {kk: mm["exit_price"] for kk, mm in modes.items()},
                "sl_points": self.sl_points}