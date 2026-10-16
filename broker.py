"""
Broker layer with identical interface for PAPER and LIVE.

PaperBroker  — simulates fills against a price feed (live Fyers quotes OR a
               scripted path), tracks orders/positions/trades/P&L in JSON.
LiveBroker   — same methods, routed to real Fyers via FyersClient.

The strategy code (bot.py) is written against this interface, so flipping
paper -> live is a one-line change. NOTHING here places a real order unless
you construct a LiveBroker.

Order model (both): buy-stop and sell-stop are SL-M orders that rest until the
trigger is crossed, then fill at market. Sell-stops are used as the trailing
stop-loss (trigger modified as the SL moves up).
"""
import os
import json
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST

_DEFAULT_TICK_SIZE = 0.05
TICK_SIZE = float(os.getenv("TICK_SIZE", str(_DEFAULT_TICK_SIZE)))


def align_tick(price: float, tick: float | None = None) -> float:
    """Round ``price`` to the nearest valid exchange tick (default NSE equity 0.05).

    Overridable via explicit ``tick`` argument, ``broker.TICK_SIZE`` module
    constant, or the ``TICK_SIZE`` environment variable.
    """
    if tick is None:
        if TICK_SIZE != _DEFAULT_TICK_SIZE:
            tick = TICK_SIZE
        else:
            env_tick = os.getenv("TICK_SIZE")
            tick = float(env_tick) if env_tick else TICK_SIZE
    t = Decimal(str(tick))
    if t <= 0:
        return float(price)
    d = Decimal(str(float(price)))
    steps = (d / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(steps * t)


align_to_tick = align_tick
round_to_tick = align_tick


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")


class PaperBroker:
    def __init__(self, state_path="paper_state.json", capital=1_000_000.0, reset=False):
        self.state_path = state_path
        fresh = {"cash": capital, "start_capital": capital, "next_id": 1,
                 "orders": [], "positions": {}, "trades": [], "day": _today()}
        if reset or not os.path.exists(state_path):
            self.s = fresh
            self._save()
            return
        self.s = json.load(open(state_path))
        self.s.setdefault("day", _today())
        if self.s["day"] != _today():
            # New session: DAY-validity stops would have been cancelled by the exchange;
            # GTT stops survive across sessions.
            for o in self.s["orders"]:
                if o["status"] == "pending" and str(o.get("validity", "DAY")).upper() != "GTT":
                    o["status"] = "expired"
                    o["expired_at"] = _now()
            self.s["day"] = _today()
            self._save()

    def _save(self):
        json.dump(self.s, open(self.state_path, "w"), indent=2)

    def _oid(self):
        oid = f"P{self.s['next_id']:06d}"
        self.s["next_id"] += 1
        return oid

    # ---- order placement (return order id) ----
    def _place_stop(self, symbol, qty, trigger, side, order_tag="cupbot", validity="GTT"):
        if str(validity or "").upper().strip() != "GTT":
            raise ValueError(f"stop validity must be 'GTT', got {validity!r}")
        oid = self._oid()
        self.s["orders"].append({
            "id": oid, "symbol": symbol.upper(), "qty": int(qty),
            "side": side, "type": "SL-M", "trigger": align_tick(trigger),
            "validity": "GTT", "tag": order_tag, "status": "pending", "placed_at": _now(),
        })
        self._save()
        return oid

    def place_buy_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        return self._place_stop(symbol, qty, align_tick(trigger), "BUY", order_tag, validity="GTT")

    def place_sell_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        return self._place_stop(symbol, qty, align_tick(trigger), "SELL", order_tag, validity="GTT")

    def modify_stop(self, order_id, new_trigger, symbol=None, qty=None, order_tag=None):
        aligned = align_tick(new_trigger)
        for o in self.s["orders"]:
            if o["id"] == order_id and o["status"] == "pending":
                o["trigger"] = aligned
                if qty is not None:
                    o["qty"] = int(qty)
                o["modified_at"] = _now()
        self._save()

    def modify_stop_qty(self, order_id, new_qty, new_trigger, symbol=None, order_tag=None):
        """Shrink a resting stop's quantity and reset its trigger in one step.
        symbol/order_tag accepted (unused) for interface parity with LiveBroker,
        whose fallback path needs them to re-place."""
        aligned = align_tick(new_trigger)
        for o in self.s["orders"]:
            if o["id"] == order_id and o["status"] == "pending":
                o["qty"] = int(new_qty)
                o["trigger"] = aligned
                o["modified_at"] = _now()
        self._save()

    def cancel(self, order_id):
        for o in self.s["orders"]:
            if o["id"] == order_id and o["status"] == "pending":
                o["status"] = "cancelled"
        self._save()

    def market_sell(self, symbol, qty, price, order_tag=None):
        """Immediate sell (used for partial booking). order_tag accepted (unused)
        for interface parity with LiveBroker."""
        self._fill("SELL", symbol.upper(), int(qty), float(price), reason="PARTIAL")

    # ---- simulation step: process pending stops against a price map ----
    def feed(self, prices):
        """prices: {SYMBOL: ltp}. Fills any pending stop whose trigger is crossed.
        Returns list of fill dicts that happened this tick."""
        fills = []
        for o in self.s["orders"]:
            if o["status"] != "pending":
                continue
            ltp = prices.get(o["symbol"])
            if ltp is None:
                continue
            hit = (o["side"] == "BUY" and ltp >= o["trigger"]) or \
                  (o["side"] == "SELL" and ltp <= o["trigger"])
            if hit:
                o["status"] = "filled"
                o["filled_at"] = _now()
                o["fill_price"] = float(ltp)
                reason = "ENTRY" if o["side"] == "BUY" else "STOP_EXIT"
                self._fill(o["side"], o["symbol"], o["qty"], float(ltp), reason=reason, order_id=o["id"])
                fills.append({"symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                              "price": float(ltp), "reason": reason})
        self._save()
        return fills

    def _fill(self, side, symbol, qty, price, reason, order_id=None):
        pos = self.s["positions"].get(symbol)
        if side == "BUY":
            if pos:
                new_qty = pos["qty"] + qty
                pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / new_qty
                pos["qty"] = new_qty
            else:
                self.s["positions"][symbol] = {"qty": qty, "avg": price, "opened_at": _now()}
            self.s["cash"] -= price * qty
            self.s["trades"].append({"t": _now(), "symbol": symbol, "side": "BUY",
                                     "qty": qty, "price": price, "reason": reason, "pnl": 0})
        else:  # SELL
            entry = pos["avg"] if pos else price
            pnl = round((price - entry) * qty, 2)
            self.s["cash"] += price * qty
            self.s["trades"].append({"t": _now(), "symbol": symbol, "side": "SELL",
                                     "qty": qty, "price": price, "reason": reason, "pnl": pnl})
            if pos:
                pos["qty"] -= qty
                if pos["qty"] <= 0:
                    del self.s["positions"][symbol]

    # ---- queries ----
    def positions(self):
        return [{"symbol": s, **p} for s, p in self.s["positions"].items()]

    def open_orders(self, symbol=None, mine_only=False):
        # every paper order is ours, so mine_only is accepted for interface parity
        return [dict(o, mine=True) for o in self.s["orders"]
                if o["status"] == "pending" and (symbol is None or o["symbol"] == symbol.upper())]

    def report(self):
        trades = self.s["trades"]
        realized = round(sum(t["pnl"] for t in trades), 2)
        wins = [t for t in trades if t["side"] == "SELL" and t["pnl"] > 0]
        losses = [t for t in trades if t["side"] == "SELL" and t["pnl"] < 0]
        return {
            "mode": "PAPER",
            "start_capital": self.s["start_capital"],
            "cash": round(self.s["cash"], 2),
            "realized_pnl": realized,
            "open_positions": self.positions(),
            "trades": trades,
            "sell_wins": len(wins), "sell_losses": len(losses),
        }


class LiveBroker:
    """Same interface as PaperBroker, routed to real Fyers.

    Fills happen at the broker, so feed() reconciles the Fyers orderbook against
    the ids we have already processed and synthesises the fill events bot.py needs.
    bot.py additionally cross-checks against positions() every cycle (see
    CupBot._reconcile_live) — that is deliberate defense in depth: feed() gives an
    accurate fill price from the orderbook, positions()-diffing catches anything
    feed() misses (a tag that Fyers doesn't echo, an orderbook row that aged out
    before we polled). Neither is trusted alone.

    The orderbook is fetched at most once every ORDERBOOK_TTL seconds and is
    invalidated immediately after any order write, so a cycle that touches N
    symbols costs one API call instead of N (Fyers rate limits are per-second).

    GTT modify vs cancel+replace:
    Fyers v3 does NOT support ``modify_order`` (PATCH /orders/sync) on a GTT order
    identically to a DAY order (GTT orders in fyers_apiv3 use PATCH /gtt/orders/sync
    with mandatory ``orderInfo.leg1.{price, triggerPrice, qty}``). When ``modify_order``
    is rejected/unsupported on a GTT stop, both ``modify_stop`` and ``modify_stop_qty``
    replace-then-cancel: the replacement GTT stop is placed FIRST and confirmed before
    the old stop is cancelled, so there is NEVER a window without a protective stop.
    """

    # Fyers v3 order status codes: 1=Cancelled 2=Traded/Filled 4=Transit 5=Rejected 6=Pending
    OPEN_STATUS = {4, 6}
    FILLED_STATUS = 2
    TYPE_SLM = 3
    TYPE_MARKET = 2
    ORDERBOOK_TTL = 3.0        # seconds

    def __init__(self, client, tag="cup", state_path="live_state.json"):
        self.c = client
        self.tag = str(tag)[:20]
        self.state_path = state_path
        self.s = {"day": _today(), "seen": [], "ours": [], "stops": {}}
        if os.path.exists(state_path):
            try:
                self.s = json.load(open(state_path))
            except Exception:
                pass
        self.s.setdefault("seen", [])
        self.s.setdefault("ours", [])
        self.s.setdefault("stops", {})
        if self.s.get("day") != _today():
            # GTT stops survive overnight across sessions — preserve "ours" and "stops"
            # so prior-day GTT order ids remain recognized and modifiable.
            self.s["day"] = _today()
        self._ob = None
        self._ob_at = 0.0
        self._warned_untagged = False
        self._save()

    def _save(self):
        json.dump(self.s, open(self.state_path, "w"), indent=2)

    def _remember(self, order_id, meta=None):
        """Track ids we placed, so reconciliation still works if Fyers does not
        echo orderTag back in the orderbook."""
        oid = str(order_id or "")
        if not oid:
            raise RuntimeError("broker returned empty order id")
        if oid not in self.s["ours"]:
            self.s["ours"].append(oid)
        if meta is not None:
            self.s.setdefault("stops", {})[oid] = dict(meta)
        self._save()
        return oid

    # ---- orderbook cache ----
    def _invalidate(self):
        self._ob = None

    def _orderbook(self):
        """Cached orderbook rows. Returns [] (or the stale cache) on a read
        failure — never raises, since a transient read failure must not be
        confused with "no orders exist" by callers that diff against it."""
        if self._ob is not None and (time.time() - self._ob_at) < self.ORDERBOOK_TTL:
            return self._ob
        try:
            resp = self.c.orderbook()
        except Exception as e:
            print(f"[warn] orderbook read failed: {e}")
            return self._ob if self._ob is not None else []
        rows = (resp.get("orderBook") or []) if isinstance(resp, dict) else []
        self._ob, self._ob_at = rows, time.time()
        return rows

    @staticmethod
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _sym(raw):
        return str(raw or "").split(":")[-1].rsplit("-", 1)[0].upper()

    def _mine(self, o):
        """True if this orderbook row is ours: it carries our strategy tag, or its
        id is one we placed this session. Fyers echoes the tag as orderTag; some
        builds nest it — check both."""
        oid = str(o.get("id") or "")
        if oid and oid in self.s["ours"]:
            return True
        tag = o.get("orderTag") or o.get("orderTag1") or ""
        return bool(self.tag) and self.tag in str(tag)

    # ---- order placement (GTT stops) ----
    def place_buy_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        aligned = align_tick(trigger)
        oid = self.c.place_buy_stop(symbol, qty, aligned, order_tag=order_tag)
        self._invalidate()
        return self._remember(
            oid,
            {
                "symbol": self._sym(symbol),
                "qty": int(qty),
                "trigger": aligned,
                "side": "BUY",
                "order_tag": str(order_tag or self.tag)[:20],
                "validity": "GTT",
            },
        )

    def place_sell_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        aligned = align_tick(trigger)
        oid = self.c.place_sell_stop(symbol, qty, aligned, order_tag=order_tag)
        self._invalidate()
        return self._remember(
            oid,
            {
                "symbol": self._sym(symbol),
                "qty": int(qty),
                "trigger": aligned,
                "side": "SELL",
                "order_tag": str(order_tag or self.tag)[:20],
                "validity": "GTT",
            },
        )

    def _resolve_stop_meta(self, order_id, symbol=None, qty=None, order_tag=None):
        """Resolve (symbol, qty, order_tag) for a resting stop so a cancel+replace
        fallback can place the replacement stop before cancelling ``order_id``."""
        oid = str(order_id or "")
        stored = dict(self.s.get("stops", {}).get(oid) or {})
        sym = self._sym(symbol) if symbol else stored.get("symbol")
        resolved_qty = int(qty) if qty is not None else stored.get("qty")
        resolved_tag = str(order_tag or stored.get("order_tag") or self.tag)[:20]

        if not sym or not resolved_qty:
            for o in self._orderbook():
                if str(o.get("id") or "") == oid:
                    if not sym:
                        sym = self._sym(o.get("symbol"))
                    if not resolved_qty:
                        resolved_qty = self._int(o.get("qty"))
                    if not order_tag and o.get("orderTag"):
                        resolved_tag = str(o.get("orderTag"))[:20]
                    break

        if sym and not resolved_qty:
            for p in self.positions():
                if p.get("symbol") == sym and p.get("qty"):
                    resolved_qty = int(p["qty"])
                    break

        return sym, resolved_qty, resolved_tag

    @staticmethod
    def _is_cancel_confirmed(cancel_res):
        if not isinstance(cancel_res, dict):
            return False
        if str(cancel_res.get("s", "")).lower() == "ok":
            return True
        msg = str(cancel_res.get("message", "")).lower()
        return any(
            m in msg
            for m in ("not found", "no such", "does not exist", "not open", "already", "filled", "completed", "cancelled", "canceled")
        )

    def _replace_stop(self, order_id, new_qty, new_trigger, symbol=None, order_tag=None):
        """Replace a protective stop with ZERO unprotected window:
        1. Place the replacement GTT stop FIRST and confirm ``new_id`` is resting.
        2. Cancel the old stop ``order_id`` SECOND.
        3. If cancelling ``order_id`` fails, roll back ``new_id`` so two stops are
           never left stacked, while ``order_id`` remains resting, and RAISE.
        """
        aligned_trigger = align_tick(new_trigger)
        sym, resolved_qty, resolved_tag = self._resolve_stop_meta(
            order_id, symbol=symbol, qty=new_qty, order_tag=order_tag
        )
        if not sym or not resolved_qty or int(resolved_qty) <= 0:
            raise RuntimeError(
                f"cannot safely replace GTT stop {order_id}: missing symbol={sym!r} or qty={resolved_qty!r}"
            )
        # 1. Place the replacement GTT stop FIRST so the position is never unprotected
        new_id = self.place_sell_stop(sym, int(resolved_qty), aligned_trigger, order_tag=resolved_tag)
        if not new_id:
            raise RuntimeError(f"replacement GTT stop for {sym} returned no order id")

        # 2. Only once replacement is in place, cancel the old stop
        cancel_res = self.c.cancel_order(order_id)
        self._invalidate()
        if not self._is_cancel_confirmed(cancel_res):
            # Roll back the newly placed stop so we do not stack two active stops
            # while the uncancelled old stop remains resting.
            try:
                self.c.cancel_order(new_id)
                self.s.get("stops", {}).pop(str(new_id), None)
                self._invalidate()
                self._save()
            except Exception:
                pass
            raise RuntimeError(
                f"cancel of {order_id} unconfirmed during GTT stop replace "
                f"({cancel_res!r}); rolled back replacement {new_id}"
            )
        self.s.get("stops", {}).pop(str(order_id), None)
        self._save()
        return {"s": "ok", "id": new_id}

    def replace_stop(self, order_id, new_trigger, new_qty=None, symbol=None, order_tag=None):
        """Explicit replace-before-cancel entry point for a GTT protective stop."""
        return self._replace_stop(
            order_id,
            new_qty=new_qty,
            new_trigger=align_tick(new_trigger),
            symbol=symbol,
            order_tag=order_tag,
        )

    def modify_stop(self, order_id, new_trigger, symbol=None, qty=None, order_tag=None):
        """Modify a resting protective stop's trigger price.

        Tries in-place ``modify_order`` first (0 window). Because Fyers v3 does NOT
        support ``modify_order`` on GTT orders identically to DAY orders, falls back
        to ``_replace_stop`` (places replacement GTT stop BEFORE cancelling old stop,
        also 0 window without a protective stop). Raises if neither path succeeds.
        """
        aligned_trigger = align_tick(new_trigger)
        try:
            r = self.c.modify_order(order_id, stop_price=aligned_trigger)
            if not (isinstance(r, dict) and str(r.get("s", "")).lower() == "ok"):
                raise RuntimeError(f"modify_order {order_id} returned {r!r}")
            self._invalidate()
            oid = str(order_id)
            if oid in self.s.get("stops", {}):
                self.s["stops"][oid]["trigger"] = aligned_trigger
                self._save()
            return r
        except Exception as e:
            sym, resolved_qty, resolved_tag = self._resolve_stop_meta(
                order_id, symbol=symbol, qty=qty, order_tag=order_tag
            )
            if not sym or not resolved_qty:
                raise
            print(f"[warn] GTT stop modify rejected ({e}); "
                  f"replacing-then-cancelling for {sym} (no unprotected window)")
            return self._replace_stop(
                order_id,
                new_qty=resolved_qty,
                new_trigger=aligned_trigger,
                symbol=sym,
                order_tag=resolved_tag,
            )

    def modify_stop_qty(self, order_id, new_qty, new_trigger, symbol=None, order_tag=None):
        """Shrink/update a resting stop's qty and trigger together.

        Tries a single combined in-place modify first (0 window). Because Fyers v3
        does NOT support ``modify_order`` on GTT orders identically to DAY orders,
        falls back to ``_replace_stop``: places the replacement GTT stop FIRST, and
        only cancels ``order_id`` once the replacement is confirmed resting (never
        leaving a window with no protective stop). Returns a dict carrying the new
        order id under "id" on the replacement path.
        """
        aligned_trigger = align_tick(new_trigger)
        try:
            r = self.c.modify_order(order_id, stop_price=aligned_trigger, qty=int(new_qty))
            if not (isinstance(r, dict) and str(r.get("s", "")).lower() == "ok"):
                raise RuntimeError(f"modify_order {order_id} returned {r!r}")
            self._invalidate()
            oid = str(order_id)
            if oid in self.s.get("stops", {}):
                self.s["stops"][oid]["qty"] = int(new_qty)
                self.s["stops"][oid]["trigger"] = aligned_trigger
                self._save()
            return r
        except Exception as e:
            sym, _, resolved_tag = self._resolve_stop_meta(
                order_id, symbol=symbol, qty=new_qty, order_tag=order_tag
            )
            if not sym:
                raise
            print(f"[warn] combined qty+trigger modify rejected ({e}); "
                  f"replacing-then-cancelling for {sym} (no unprotected window)")
            return self._replace_stop(
                order_id,
                new_qty=int(new_qty),
                new_trigger=aligned_trigger,
                symbol=sym,
                order_tag=resolved_tag,
            )

    def cancel(self, order_id):
        r = self.c.cancel_order(order_id)
        self._invalidate()
        if self._is_cancel_confirmed(r):
            self.s.get("stops", {}).pop(str(order_id), None)
            self._save()
        return r

    def market_sell(self, symbol, qty, price=None, order_tag=None):
        oid = self.c.place_market_sell(symbol, qty, order_tag=order_tag or self.tag)
        self._invalidate()
        return self._remember(oid)

    # ---- fill reconciliation (bot.py's primary source of ENTRY/STOP_EXIT events;
    # bot.py._reconcile_live() is the secondary safety net — see class docstring) ----
    def feed(self, prices=None):
        """Returns fill dicts for tagged orders that completed since the last call.

        Only SL-M (type 3) rows produce events:
          BUY  type 3 -> ENTRY       (our breakout buy-stop)
          SELL type 3 -> STOP_EXIT   (our protective stop)
        SELL type 2 (our own partial-book market sell) is marked seen and ignored,
        otherwise bot.py would treat a partial as a full exit and drop the state.
        """
        fills = []
        rows = self._orderbook()
        untagged_fills = 0

        for o in rows:
            oid = str(o.get("id") or "")
            if not oid or oid in self.s["seen"]:
                continue
            status = self._int(o.get("status"))
            if not self._mine(o):
                if status == self.FILLED_STATUS:
                    untagged_fills += 1
                continue
            if status != self.FILLED_STATUS:
                continue

            self.s["seen"].append(oid)          # mark before deciding — never replay
            otype = self._int(o.get("type"))
            side = self._int(o.get("side"))
            if otype != self.TYPE_SLM:
                continue                         # partial-book market sell etc.

            qty = self._int(o.get("filledQty")) or self._int(o.get("qty")) or 0
            px = o.get("tradedPrice") or o.get("avgPrice") or o.get("stopPrice")
            try:
                px = float(px)
            except (TypeError, ValueError):
                px = 0.0
            if qty <= 0 or px <= 0:
                print(f"[warn] filled order {oid} has qty={qty} px={px}; skipping event")
                continue

            fills.append({
                "symbol": self._sym(o.get("symbol")),
                "side": "BUY" if side == 1 else "SELL",
                "qty": qty,
                "price": px,
                "reason": "ENTRY" if side == 1 else "STOP_EXIT",
            })

        if untagged_fills and not self._warned_untagged:
            self._warned_untagged = True
            print(f"[warn] {untagged_fills} filled order(s) in the book carry no '{self.tag}' "
                  f"orderTag and were not placed by this session — ignored by reconciliation. "
                  f"If these are ours, the orderTag is not being echoed back; check the orderbook "
                  f"field names with test_live_order.py.")

        self._save()
        return fills

    # ---- queries ----
    def positions(self):
        resp = self.c.positions()
        out = []
        for p in ((resp.get("netPositions") or []) if isinstance(resp, dict) else []):
            netqty = self._int(p.get("netQty")) or 0
            if netqty > 0:
                avg = p.get("avgPrice") or p.get("netAvg") or 0
                out.append({"symbol": self._sym(p.get("symbol")),
                            "qty": int(netqty), "avg": float(avg or 0)})
        return out

    def open_orders(self, symbol=None, mine_only=False):
        out = []
        for o in self._orderbook():
            if self._int(o.get("status")) not in self.OPEN_STATUS:
                continue
            sym = self._sym(o.get("symbol"))
            if symbol is not None and sym != str(symbol).upper():
                continue
            mine = self._mine(o)
            if mine_only and not mine:
                continue
            out.append({"id": str(o.get("id")), "symbol": sym,
                        "side": "BUY" if self._int(o.get("side")) == 1 else "SELL",
                        "trigger": o.get("stopPrice"), "qty": o.get("qty"),
                        "mine": mine})
        return out

    def report(self):
        return {"mode": "LIVE", "realized_pnl": 0, "sell_wins": 0, "sell_losses": 0,
                "open_positions": self.positions(),
                "note": "live realized P&L: check Fyers tradebook"}
