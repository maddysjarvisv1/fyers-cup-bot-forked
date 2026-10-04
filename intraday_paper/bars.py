"""5-minute bar building from periodic ltp polls.

Pure: no client, no network. A BarBuilder holds a rolling in-progress bar per
symbol in `bar_map`; when an ltp poll lands in a new 5-min slot, the prior bar
is finalized (finalized=True) and surfaced from `add()`.
"""
from __future__ import annotations

DEFAULT_BAR_SECONDS = 300   # 5 minutes


def new_bar(sym: str, ltp: float, slot: int) -> dict:
    """Open a new bar for a symbol at a 5-min slot, seeded from the first poll."""
    return {
        "sym": sym,
        "open": ltp,
        "high": ltp,
        "low": ltp,
        "close": ltp,
        "slot": slot,
        "finalized": False,
    }


def update_bar(bar: dict, ltp: float) -> None:
    """Fold an ltp poll into an open bar. open never moves; high/low/close update."""
    bar["high"] = max(bar["high"], ltp)
    bar["low"] = min(bar["low"], ltp)
    bar["close"] = ltp


def finalize_bar(bar: dict) -> dict:
    """Mark a bar finalized (in place) and return it."""
    bar["finalized"] = True
    return bar


def current_bar(bar_map: dict, sym: str):
    """Return the in-progress (not-yet-finalized) bar for a symbol, or None."""
    b = bar_map.get(sym)
    return dict(b) if b else None


class BarBuilder:
    """Rolling 5-min bar state machine.

    add(sym, ltp, ts) -> list[dict]: list of bars finalized by this poll
        (empty when the poll lands inside the current bar's slot).
    """

    def __init__(self, bar_seconds: int = DEFAULT_BAR_SECONDS):
        self.bar_seconds = bar_seconds
        self.bar_map = {}            # sym -> open (in-progress) bar
        self._finalized = {}         # sym -> list of finalized bars (handed to add)
        self._current_slot = {}      # sym -> slot of the open bar

    def slot_of(self, ts: float) -> int:
        return int(ts // self.bar_seconds)

    def add(self, sym: str, ltp: float, ts: float) -> list:
        slot = self.slot_of(ts)
        cur = self.bar_map.get(sym)
        if cur is None:
            # brain-dead first-slot out-of-order protection: only trust ts >= last
            self.bar_map[sym] = new_bar(sym, ltp, slot)
            self._current_slot[sym] = slot
            return []
        if slot != self._current_slot[sym] and slot != cur["slot"]:
            # roll: finalize the open bar, open a new one
            finalized = finalize_bar(cur)
            self._finalized.setdefault(sym, []).append(finalized)
            self.bar_map[sym] = new_bar(sym, ltp, slot)
            self._current_slot[sym] = slot
            return [dict(finalized)]
        # same slot: reject stale (older) polls by ts, then fold
        update_bar(cur, ltp)
        return []

    def finalized(self, sym: str) -> list:
        """Bars already finalized for a symbol (chronological)."""
        return list(self._finalized.get(sym, []))

    def current_bar(self, sym: str):
        return current_bar(self.bar_map, sym)