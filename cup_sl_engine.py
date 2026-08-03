#!/usr/bin/env python
"""
CupBreakout SL/Trail engine — the pure decision logic for managing an open
position's stop-loss, breakeven, partial-book and trailing.

Kept as a PURE function `step(state, ltp)` so it can be:
  1. driven by synthetic prices in a test (run this file directly), and
  2. called each cycle by the live exit manager (cup_exit_manager.py).

Rules (match the original cup-strategy scanner):
  - initial SL      = entry * (1 - SL_PCT)          [default 6% below]
  - breakeven       : ltp >= entry*(1+BE_TRIGGER)   [+10%] -> SL to entry
  - partial book    : ltp >= entry*(1+PARTIAL_TRIGGER) [+20%] -> sell half,
                      start trailing, SL = high*(1 - TRAIL_PCT) [10% below high]
  - trail           : as new highs print, raise SL = high*(1 - TRAIL_PCT)
  - exit            : ltp <= current SL -> sell remaining
"""
from dataclasses import dataclass, field

SL_PCT          = 0.06   # initial stop 6% below entry
BE_TRIGGER      = 0.10   # move to breakeven at +10%
PARTIAL_TRIGGER = 0.20   # book half + start trailing at +20%
TRAIL_PCT       = 0.10   # trail 10% below the running high


@dataclass
class PosState:
    entry: float
    qty_total: int
    qty_remaining: int
    phase: str = "ACTIVE"          # ACTIVE -> BE -> TRAILING -> CLOSED
    sl: float = 0.0                # current stop-loss level
    highest: float = 0.0           # highest price seen since entry

    def __post_init__(self):
        if self.sl == 0.0:
            self.sl = round(self.entry * (1 - SL_PCT), 2)
        if self.highest == 0.0:
            self.highest = self.entry


def step(state: PosState, ltp: float):
    """Advance one price tick. Returns (state, actions) where actions is a list
    of tuples: ('modify_sl', price) | ('book_partial', qty, price) | ('exit', qty, price)."""
    actions = []
    if state.phase == "CLOSED":
        return state, actions

    state.highest = max(state.highest, ltp)

    # 1) Stop-loss hit -> exit remaining
    if ltp <= state.sl:
        actions.append(("exit", state.qty_remaining, state.sl))
        state.phase = "CLOSED"
        state.qty_remaining = 0
        return state, actions

    # 2) Breakeven at +10% (only while still ACTIVE)
    if state.phase == "ACTIVE" and ltp >= state.entry * (1 + BE_TRIGGER):
        new_sl = round(state.entry, 2)
        if new_sl > state.sl:
            state.sl = new_sl
            actions.append(("modify_sl", state.sl))
        state.phase = "BE"

    # 3) Partial book + start trailing at +20% (from BE)
    if state.phase == "BE" and ltp >= state.entry * (1 + PARTIAL_TRIGGER):
        book = state.qty_remaining // 2
        if book > 0:
            actions.append(("book_partial", book, ltp))
            state.qty_remaining -= book
        state.sl = round(state.highest * (1 - TRAIL_PCT), 2)
        actions.append(("modify_sl", state.sl))
        state.phase = "TRAILING"

    # 4) Trail the stop up as new highs print
    if state.phase == "TRAILING":
        trail = round(state.highest * (1 - TRAIL_PCT), 2)
        if trail > state.sl:
            state.sl = trail
            actions.append(("modify_sl", state.sl))

    return state, actions


# ---------------------------------------------------------------------------
# Self-test: drive a synthetic price path and print SL/phase transitions.
# Run:  python cup_sl_engine.py
# ---------------------------------------------------------------------------
def _demo():
    entry = 100.0
    st = PosState(entry=entry, qty_total=100, qty_remaining=100)
    print(f"ENTRY @ {entry}  qty 100  | initial SL {st.sl} (-6%)  BE@110  Partial@120  trail 10%")
    print("-" * 78)
    # path: climb to +25%, pull back, trail-exit
    path = [102, 105, 110, 113, 118, 120, 122, 125, 123, 120, 118, 115, 112]
    for ltp in path:
        st, acts = step(st, ltp)
        tag = f"px {ltp:>6.1f}  phase {st.phase:<8} SL {st.sl:>6.2f}  high {st.highest:>6.1f}  qty {st.qty_remaining:>3}"
        note = "  ".join(f"[{a[0]} {a[1:]}]" for a in acts) if acts else ""
        print(f"{tag}   {note}")
        if st.phase == "CLOSED":
            print("-" * 78); print(f"CLOSED at {ltp}. Result: booked half near +20%, trailed the rest, exited on 10% pullback from high.")
            break

    print()
    # second path: quick stop-out below -6% before any profit
    print("Second scenario — drops straight to stop-loss:")
    st2 = PosState(entry=100.0, qty_total=50, qty_remaining=50)
    for ltp in [99, 97, 95, 94, 93]:
        st2, acts = step(st2, ltp)
        note = "  ".join(f"[{a[0]} {a[1:]}]" for a in acts) if acts else ""
        print(f"px {ltp:>5.1f}  phase {st2.phase:<8} SL {st2.sl:>6.2f}  {note}")
        if st2.phase == "CLOSED":
            break


if __name__ == "__main__":
    _demo()
