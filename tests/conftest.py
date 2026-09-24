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
    """A controllable double matching the REAL broker.LiveBroker interface.

    feed() returns [] (fills happen "at the broker"), and positions()/open_orders()
    read from mutable internal state we manipulate per-test. Order-mutating calls
    are recorded so tests can assert what the bot requested.

    Failure semantics mirror the real fyers_client.py/broker.py layer:
    place_buy_stop / place_sell_stop / modify_stop / modify_stop_qty RAISE on
    failure (fyers_client._ok() raises FyersError; bot.py wraps in LiveOrderError).
    cancel() never raises — it returns a Fyers-style dict that may carry
    s="error" (cancel is deliberately best-effort at the real layer too), which
    bot._cancel_tolerant()/_cancel_ok() classify as tolerable or not.
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
        self.cancel_error_shape = {"s": "error", "code": 500, "message": "cancel failed"}

    # ---- controllable failure injection -------------------------------------
    def _should_fail(self, op, symbol=None):
        mode = self.fail.get(op)
        failed = False
        if mode in (True, "always"):
            if mode is True:
                self.fail[op] = False  # one-shot
            failed = True
        if symbol and self._symbol_fails(op, symbol):
            failed = True
        return failed

    def _symbol_fails(self, op, symbol):
        return symbol.upper() in {s.upper() for s in self.fail_symbols.get(op, ())}

    def _oid(self):
        oid = f"L{self._next_id:05d}"
        self._next_id += 1
        return oid

    # ---- order placement: RAISE on failure, return a bare string id on success
    # (matches the real fyers_client.py, which validates and raises FyersError
    # rather than returning an error-shaped dict for place_*/modify_*) --------
    def place_buy_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        self.calls.append("place_buy_stop")
        if self._should_fail("place_buy_stop", symbol):
            raise RuntimeError(f"buy-stop {symbol}: failed (s=error)")
        self.placed.append(("BUY", symbol, qty, trigger))
        oid = self._oid()
        self.pending.append({"id": oid, "symbol": symbol, "qty": qty,
                             "trigger": trigger, "side": "BUY", "status": "pending"})
        return oid

    def place_sell_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        self.calls.append("place_sell_stop")
        if self._should_fail("place_sell_stop", symbol):
            raise RuntimeError(f"sell-stop {symbol}: failed (s=error)")
        self.placed.append(("SELL", symbol, qty, trigger))
        oid = self._oid()
        self.pending.append({"id": oid, "symbol": symbol, "qty": qty,
                             "trigger": trigger, "side": "SELL", "status": "pending"})
        return oid

    def modify_stop(self, order_id, new_trigger):
        self.calls.append("modify_stop")
        if self._should_fail("modify_stop"):
            raise RuntimeError(f"modify {order_id}: failed (s=error)")
        self.modified.append((order_id, new_trigger))
        for o in self.pending:
            if o["id"] == order_id:
                o["trigger"] = new_trigger
        return {"s": "ok"}

    def modify_stop_qty(self, order_id, new_qty, new_trigger, symbol=None, order_tag=None):
        """Mirrors broker.LiveBroker.modify_stop_qty: atomic modify first; on a
        configured failure, falls back to cancel-then-replace (only if the
        cancel is confirmed), returning the NEW id, exactly like the real
        fallback path does."""
        self.calls.append("modify_stop_qty")
        if not self._should_fail("modify_stop_qty", symbol):
            self.modified.append((order_id, new_trigger))
            for o in self.pending:
                if o["id"] == order_id:
                    o["qty"], o["trigger"] = new_qty, new_trigger
            return {"s": "ok"}
        # atomic path "rejected" -> fall back to cancel-then-replace
        cancel_res = self.cancel(order_id)
        if not (isinstance(cancel_res, dict) and cancel_res.get("s") == "ok"):
            raise RuntimeError(f"cancel of {order_id} unconfirmed during qty-shrink")
        new_id = self.place_sell_stop(symbol, new_qty, new_trigger, order_tag=order_tag)
        return {"s": "ok", "id": new_id}

    def cancel(self, order_id):
        # cancel is deliberately best-effort at the real layer: it returns a
        # dict (possibly s="error") rather than raising.
        self.calls.append("cancel")
        self.cancel_attempts.append(order_id)
        if self._should_fail("cancel"):
            return dict(self.cancel_error_shape)
        self.cancelled.append(order_id)
        self.pending = [o for o in self.pending if o["id"] != order_id]
        return {"s": "ok"}

    def market_sell(self, symbol, qty, price, order_tag=None):
        self.calls.append("market_sell")
        if self._should_fail("market_sell", symbol):
            raise RuntimeError(f"market_sell {symbol}: failed (s=error)")
        self.sold.append((symbol, qty, price))
        return {"s": "ok"}

    # ---- queries ------------------------------------------------------------
    def feed(self, prices):
        return []  # fills happen at the broker, not in-process (live semantics)

    def positions(self):
        if self.positions_error:
            raise RuntimeError("positions unavailable (network/timeout)")
        return [{"symbol": s, **p} for s, p in self.positions_map.items()
                if p.get("qty", 0) != 0]

    def open_orders(self, symbol=None, mine_only=False):
        out = []
        for o in self.pending:
            if o.get("status") == "pending" and (symbol is None or o["symbol"] == symbol.upper()):
                out.append({"id": o["id"], "symbol": o["symbol"], "side": o["side"],
                            "trigger": o.get("trigger"), "qty": o.get("qty"), "mine": True})
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
