"""
Offline regression tests for the live-mode fix pack. No Fyers, no network.

    python test_live_fixes.py

Covers the failure modes the patches exist for:
  1. stop re-arm      — stop cancelled at the broker, the exit-pass self-heal re-places it
  2. day roll         — yesterday's stop ids cleared on restart, stop re-placed
  3. adoption scope   — watchlist position adopted; a foreign one is left alone
  4. live feed        — LiveBroker reconciles an orderbook fill into an ENTRY event
  5. partial safety   — stop is shrunk before the booked half is sold
  6. exit fallback    — engine exit with no resting stop -> market sell
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cup_sl_engine as sl
from broker import PaperBroker, LiveBroker
from bot import CupBot

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail and not cond else ''}")


def tmp(name):
    return os.path.join(tempfile.mkdtemp(), name)


def fixed_price(px_map):
    return lambda syms: dict(px_map)


# ---------------------------------------------------------------------------
# 1 + 5 + 6 use a live position opened in the paper broker
# ---------------------------------------------------------------------------
def open_position(bot, broker, sym, px):
    """Drive one cycle at a price that fills the armed buy-stop."""
    bot.price_source = fixed_price({sym: px})
    bot.cycle()


def test_rearm_after_cancel():
    print("\n[1] stop re-arm after the broker stop disappears")
    b = PaperBroker(state_path=tmp("p.json"), reset=True)
    wl = [{"symbol": "ACME", "rim": 100, "trigger": 104, "qty": 10}]
    bot = CupBot(b, fixed_price({"ACME": 99.5}), wl, state_path=tmp("s.json"), verbose=False)

    bot.cycle()                                   # arms the buy-stop at 99.5
    open_position(bot, b, "ACME", 104)            # fills entry, places initial stop
    check("entry filled", len(b.positions()) == 1, f"positions={b.positions()}")
    check("initial stop placed", bot._has_live_stop("ACME"))

    sid = bot.pos_state["ACME"]["sell_stop_id"]
    b.cancel(sid)                                 # simulate exchange/manual cancel
    check("stop is gone", not bot._has_live_stop("ACME"))

    bot.price_source = fixed_price({"ACME": 105})
    bot.cycle()
    check("stop re-armed by the exit-pass self-heal", bot._has_live_stop("ACME"))
    check("new stop id stored", bot.pos_state["ACME"]["sell_stop_id"] != sid)


def test_day_roll():
    print("\n[2] day roll clears stale stop ids and re-places")
    sp, bp = tmp("p.json"), tmp("s.json")
    b = PaperBroker(state_path=sp, reset=True)
    wl = [{"symbol": "ACME", "rim": 100, "trigger": 104, "qty": 10}]
    bot = CupBot(b, fixed_price({"ACME": 99.5}), wl, state_path=bp, verbose=False)
    bot.cycle()
    open_position(bot, b, "ACME", 104)
    old_sid = bot.pos_state["ACME"]["sell_stop_id"]

    # rewrite state as if it were written yesterday, and expire the resting order
    blob = json.load(open(bp))
    blob["_day"] = "2000-01-01"
    json.dump(blob, open(bp, "w"))
    st = json.load(open(sp))
    for o in st["orders"]:
        if o["status"] == "pending":
            o["status"] = "expired"
    json.dump(st, open(sp, "w"))

    b2 = PaperBroker(state_path=sp)
    bot2 = CupBot(b2, fixed_price({"ACME": 105}), wl, state_path=bp, verbose=False)
    check("stale stop id cleared on load", bot2.pos_state["ACME"]["sell_stop_id"] is None)
    bot2.cycle()
    check("stop re-placed on first cycle", bot2._has_live_stop("ACME"))
    check("id differs from yesterday's", bot2.pos_state["ACME"]["sell_stop_id"] != old_sid)
    check("day stamp updated", json.load(open(bp))["_day"] != "2000-01-01")


def test_adoption_scope():
    print("\n[3] adoption is limited to watchlist symbols")
    # === MERGE NOTE ===
    # Adopting a position the bot never opened itself (simulated here with
    # PaperBroker._fill(..., reason="MANUAL")) is _reconcile_live's job, and
    # it's deliberately LIVE-only (see its docstring in bot.py): a PAPER
    # position can only ever come from the bot's own tracked buy-stop fills,
    # so there is nothing for a paper-mode equivalent to protect against.
    # PaperBroker is used here purely as a cheap stand-in broker under LIVE
    # semantics — exactly like tests/test_round2_live_risk.py's
    # test_foreign_position_gets_no_sell_stop does against LiveBrokerDouble.
    b = PaperBroker(state_path=tmp("p.json"), reset=True)
    # a position the bot never opened: one on the watchlist, one not
    b._fill("BUY", "ACME", 10, 100.0, reason="MANUAL")
    b._fill("BUY", "FOREIGN", 5, 50.0, reason="MANUAL")
    wl = [{"symbol": "ACME", "rim": 100, "trigger": 104, "qty": 10}]
    bot = CupBot(b, fixed_price({"ACME": 100, "FOREIGN": 50}), wl,
                 state_path=tmp("s.json"), verbose=False, mode="LIVE")
    bot.cycle()
    check("watchlist position adopted", "ACME" in bot.pos_state)
    check("watchlist position protected", bot._has_live_stop("ACME"))
    check("foreign position NOT adopted", "FOREIGN" not in bot.pos_state)
    check("foreign position NOT touched", not bot._has_live_stop("FOREIGN"))

    # and with the guard off, it is adopted
    b2 = PaperBroker(state_path=tmp("p2.json"), reset=True)
    b2._fill("BUY", "FOREIGN", 5, 50.0, reason="MANUAL")
    bot2 = CupBot(b2, fixed_price({"FOREIGN": 50}), wl, state_path=tmp("s2.json"),
                  verbose=False, mode="LIVE", adopt_watchlist_only=False)
    bot2.cycle()
    check("guard off -> adopted", "FOREIGN" in bot2.pos_state)


# ---------------------------------------------------------------------------
# 4. LiveBroker.feed against a fake Fyers client
# ---------------------------------------------------------------------------
class FakeFyers:
    """Minimal stand-in for FyersClient covering what LiveBroker calls."""
    def __init__(self, rows, positions=None):
        self.rows = rows
        self._positions = positions or []
        self.placed = []

    def orderbook(self):
        return {"s": "ok", "orderBook": self.rows}

    def positions(self):
        return {"s": "ok", "netPositions": self._positions}

    def place_sell_stop(self, symbol, qty, trigger, order_tag="cup"):
        self.placed.append(("SELL_STOP", symbol, qty, trigger, order_tag))
        return f"NEW{len(self.placed)}"

    def place_buy_stop(self, symbol, qty, trigger, order_tag="cup"):
        self.placed.append(("BUY_STOP", symbol, qty, trigger, order_tag))
        return f"NEW{len(self.placed)}"

    def place_market_sell(self, symbol, qty, order_tag="cup"):
        self.placed.append(("MKT_SELL", symbol, qty, order_tag))
        return f"NEW{len(self.placed)}"

    def modify_order(self, order_id, **kw):
        self.placed.append(("MODIFY", order_id, kw))
        return {"s": "ok"}

    def cancel_order(self, order_id):
        return {"s": "ok"}


def test_live_feed():
    print("\n[4] LiveBroker.feed reconciles orderbook fills")
    rows = [
        # ours, SL-M buy, filled -> ENTRY
        {"id": "1", "orderTag": "cup", "status": 2, "type": 3, "side": 1,
         "symbol": "NSE:ACME-EQ", "qty": 10, "filledQty": 10, "tradedPrice": 104.5},
        # ours, market sell (partial book) -> seen but no event
        {"id": "2", "orderTag": "cup", "status": 2, "type": 2, "side": -1,
         "symbol": "NSE:ACME-EQ", "qty": 5, "filledQty": 5, "tradedPrice": 125.0},
        # somebody else's filled order -> ignored
        {"id": "3", "orderTag": "manual", "status": 2, "type": 3, "side": 1,
         "symbol": "NSE:OTHER-EQ", "qty": 1, "filledQty": 1, "tradedPrice": 10.0},
        # ours, still pending -> no event
        {"id": "4", "orderTag": "cup", "status": 6, "type": 3, "side": -1,
         "symbol": "NSE:ACME-EQ", "qty": 5, "stopPrice": 112.0},
    ]
    lb = LiveBroker(FakeFyers(rows), tag="cup", state_path=tmp("live.json"))
    fills = lb.feed()
    check("one fill event", len(fills) == 1, f"got {fills}")
    check("it is the ENTRY", fills and fills[0]["reason"] == "ENTRY")
    check("symbol parsed", fills and fills[0]["symbol"] == "ACME")
    check("price from tradedPrice", fills and fills[0]["price"] == 104.5)
    check("partial market-sell not an exit", all(f["reason"] != "STOP_EXIT" for f in fills))
    check("foreign fill ignored", all(f["symbol"] != "OTHER" for f in fills))

    check("no replay on second call", lb.feed() == [])

    oo = lb.open_orders("ACME")
    check("open order listed", len(oo) == 1 and oo[0]["id"] == "4")
    check("open order marked mine", oo and oo[0]["mine"] is True)
    check("mine_only filters", len(lb.open_orders(mine_only=True)) == 1)

    # orderbook cache: repeated reads must not re-fetch
    fake = FakeFyers(rows)
    calls = {"n": 0}
    orig = fake.orderbook
    def counted():
        calls["n"] += 1
        return orig()
    fake.orderbook = counted
    lb2 = LiveBroker(fake, tag="cup", state_path=tmp("live2.json"))
    lb2.feed(); lb2.open_orders("ACME"); lb2.open_orders("ACME"); lb2.open_orders()
    check("orderbook fetched once per cycle", calls["n"] == 1, f"fetched {calls['n']}x")
    lb2.place_sell_stop("ACME", 5, 100)
    lb2.open_orders("ACME")
    check("cache invalidated after a write", calls["n"] == 2, f"fetched {calls['n']}x")


def test_live_positions_filter():
    print("\n[4b] LiveBroker.positions ignores short/zero legs")
    pos = [{"symbol": "NSE:ACME-EQ", "netQty": 10, "avgPrice": 100.0},
           {"symbol": "NSE:SHORT-EQ", "netQty": -5, "avgPrice": 50.0},
           {"symbol": "NSE:FLAT-EQ", "netQty": 0, "avgPrice": 20.0}]
    lb = LiveBroker(FakeFyers([], pos), tag="cup", state_path=tmp("live3.json"))
    out = lb.positions()
    check("only the long leg", len(out) == 1 and out[0]["symbol"] == "ACME", f"got {out}")


def test_partial_shrinks_stop_first():
    print("\n[5] partial book shrinks the stop before selling")
    b = PaperBroker(state_path=tmp("p.json"), reset=True)
    wl = [{"symbol": "ACME", "rim": 100, "trigger": 104, "qty": 10}]
    bot = CupBot(b, fixed_price({"ACME": 99.5}), wl, state_path=tmp("s.json"), verbose=False)
    bot.cycle()
    open_position(bot, b, "ACME", 104)
    sid = bot.pos_state["ACME"]["sell_stop_id"]

    order_calls = []
    real_modify_qty, real_sell = b.modify_stop_qty, b.market_sell
    b.modify_stop_qty = lambda *a, **k: (order_calls.append(("modify_qty",) + a), real_modify_qty(*a, **k))[1]
    b.market_sell = lambda *a, **k: (order_calls.append(("market_sell",) + a), real_sell(*a, **k))[1]

    bot.price_source = fixed_price({"ACME": 130})   # +25% -> BE then partial
    bot.cycle()
    bot.price_source = fixed_price({"ACME": 130})
    bot.cycle()

    kinds = [c[0] for c in order_calls]
    check("stop shrunk before the sell",
          "modify_qty" in kinds and "market_sell" in kinds
          and kinds.index("modify_qty") < kinds.index("market_sell"), f"order={kinds}")
    resting = [o for o in b.open_orders("ACME") if o["side"] == "SELL"]
    check("remainder still protected", len(resting) == 1, f"resting={resting}")
    if resting:
        check("stop qty matches what we kept",
              resting[0]["qty"] == bot.pos_state["ACME"]["state"]["qty_remaining"],
              f"stop qty {resting[0]['qty']} vs kept {bot.pos_state['ACME']['state']['qty_remaining']}")


def test_fallback_exit():
    print("\n[6] engine exit with no resting stop falls back to market sell")
    b = PaperBroker(state_path=tmp("p.json"), reset=True)
    wl = [{"symbol": "ACME", "rim": 100, "trigger": 104, "qty": 10}]
    bot = CupBot(b, fixed_price({"ACME": 99.5}), wl, state_path=tmp("s.json"), verbose=False)
    bot.cycle()
    open_position(bot, b, "ACME", 104)

    # kill the stop AND stop _ensure_stops from re-placing it, to isolate _exit_pass
    b.cancel(bot.pos_state["ACME"]["sell_stop_id"])
    bot._ensure_stops = lambda prices: None

    bot.price_source = fixed_price({"ACME": 90})    # below the -6% stop (97.76)
    bot.cycle()
    check("position closed at market", b.positions() == [], f"positions={b.positions()}")
    check("state dropped", "ACME" not in bot.pos_state)
    sells = [t for t in b.s["trades"] if t["side"] == "SELL"]
    check("a sell was recorded", len(sells) == 1, f"sells={sells}")


def test_engine_untouched():
    print("\n[0] SL engine constants unchanged")
    check("SL 6%", sl.SL_PCT == 0.06)
    check("BE +10%", sl.BE_TRIGGER == 0.10)
    check("partial +20%", sl.PARTIAL_TRIGGER == 0.20)
    check("trail 10%", sl.TRAIL_PCT == 0.10)


if __name__ == "__main__":
    test_engine_untouched()
    test_rearm_after_cancel()
    test_day_roll()
    test_adoption_scope()
    test_live_feed()
    test_live_positions_filter()
    test_partial_shrinks_stop_first()
    test_fallback_exit()

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)
    print("all good")
