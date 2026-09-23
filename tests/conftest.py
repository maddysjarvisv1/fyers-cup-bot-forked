"""Shared test fixtures: a controllable LiveBroker-style double + alert capture.

Mock-only. No fyers_apiv3, no requests, no network, no credentials, no real
orders. We inject a fake `notify` module into sys.modules so bot._alert()'s
lazy `import notify` resolves to it (the real notify.py imports `requests`,
which is not part of this test environment).
"""
import sys
import types

import pytest

# --- fake notify module (seeded before bot._alert ever imports it) -----------
_notify = types.ModuleType("notify")
sys.modules["notify"] = _notify

EVENTS = ["armed", "entry", "breakeven", "partial", "trail", "exit_", "error"]

# Seed no-op handlers so a test that runs a cycle without requesting the
# alert_log fixture never trips an AttributeError on the shared fake module.
for _n in EVENTS:
    setattr(_notify, _n, lambda *a, **k: None)


class LiveBrokerDouble:
    """A controllable double with the LiveBroker interface.

    feed() returns [] (fills happen "at the broker"), and positions()/open_orders()
    read from mutable internal state we manipulate per-test. Order-mutating calls
    are recorded so tests can assert what the bot requested, and any op can be made
    to fail (returning a Fyers-style error dict) to exercise the failure paths.
    """

    def __init__(self):
        self.positions_map = {}  # sym -> {"qty": netQty, "avg": price}
        self.pending = []        # open/pending orders (drives open_orders())
        self._next_id = 1

        # call records
        self.placed = []      # (side, symbol, qty, trigger)
        self.modified = []    # (order_id, new_trigger)
        self.cancelled = []   # order_id (successful cancels)
        self.cancel_attempts = []  # order_id (every cancel call, success or not)
        self.sold = []        # (symbol, qty, price)
        self.calls = []       # chronological op-name log (for call-order asserts)

        self.fail = {}        # op -> True (fail next call) | "always"
        self.fail_symbols = {}  # op -> set(symbol) that fails (per-symbol failure)
        self.positions_error = False  # when True, positions() raises
        self.dict_order_ids = False   # when True, place_* return {"s":"ok","id":oid}

    # ---- controllable failure injection -------------------------------------
    def _result(self, op):
        mode = self.fail.get(op)
        if mode in (True, "always"):
            if mode is True:
                self.fail[op] = False  # one-shot
            return {"s": "error", "code": 500, "message": f"{op} failed"}
        return {"s": "ok", "code": 200}

    def _oid(self):
        oid = f"L{self._next_id:05d}"
        self._next_id += 1
        return oid

    # ---- order placement (return order id on success) -----------------------
    def _symbol_fails(self, op, symbol):
        return symbol.upper() in {s.upper() for s in self.fail_symbols.get(op, ())}

    def place_buy_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        self.calls.append("place_buy_stop")
        r = self._result("place_buy_stop")
        if r["s"] == "error" or self._symbol_fails("place_buy_stop", symbol):
            return {"s": "error", "code": 500, "message": "place_buy_stop failed"}
        self.placed.append(("BUY", symbol, qty, trigger))
        oid = self._oid()
        self.pending.append({"id": oid, "symbol": symbol, "qty": qty,
                             "trigger": trigger, "side": "BUY", "status": "pending"})
        return {"s": "ok", "id": oid} if self.dict_order_ids else oid

    def place_sell_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        self.calls.append("place_sell_stop")
        r = self._result("place_sell_stop")
        if r["s"] == "error" or self._symbol_fails("place_sell_stop", symbol):
            return {"s": "error", "code": 500, "message": "place_sell_stop failed"}
        self.placed.append(("SELL", symbol, qty, trigger))
        oid = self._oid()
        self.pending.append({"id": oid, "symbol": symbol, "qty": qty,
                             "trigger": trigger, "side": "SELL", "status": "pending"})
        return {"s": "ok", "id": oid} if self.dict_order_ids else oid

    def modify_stop(self, order_id, new_trigger):
        self.calls.append("modify_stop")
        r = self._result("modify_stop")
        if r["s"] == "error":
            return r
        self.modified.append((order_id, new_trigger))
        return r

    def cancel(self, order_id):
        self.calls.append("cancel")
        self.cancel_attempts.append(order_id)
        r = self._result("cancel")
        if r["s"] == "error":
            return r
        self.cancelled.append(order_id)
        # a real broker cancel removes the resting order from the book
        self.pending = [o for o in self.pending if o["id"] != order_id]
        return r

    def market_sell(self, symbol, qty, price):
        self.calls.append("market_sell")
        r = self._result("market_sell")
        if r["s"] == "error":
            return r
        self.sold.append((symbol, qty, price))
        return r

    # ---- queries ------------------------------------------------------------
    def feed(self, prices):
        return []  # fills happen at the broker, not in-process (live semantics)

    def positions(self):
        if self.positions_error:
            raise RuntimeError("positions unavailable (network/timeout)")
        return [{"symbol": s, **p} for s, p in self.positions_map.items()
                if p.get("qty", 0) != 0]

    def open_orders(self, symbol=None):
        out = []
        for o in self.pending:
            if o.get("status") == "pending" and (symbol is None or o["symbol"] == symbol.upper()):
                out.append({"id": o["id"], "symbol": o["symbol"], "side": o["side"],
                            "trigger": o.get("trigger"), "qty": o.get("qty")})
        return out

    def report(self):
        return {"mode": "LIVE", "open_positions": self.positions()}


@pytest.fixture
def double():
    return LiveBrokerDouble()


@pytest.fixture
def alert_log():
    """Capture notify.<event>() calls as (mode, *args) tuples per event name."""
    log = {n: [] for n in EVENTS}
    for n in EVENTS:
        def rec(*a, _n=n, **k):
            log[_n].append(a)
        setattr(_notify, n, rec)
    return log


WL = [{"symbol": "WINNER", "rim": 100, "trigger": 104, "qty": 100}]
