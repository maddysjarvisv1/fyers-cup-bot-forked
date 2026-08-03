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
from datetime import datetime


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class PaperBroker:
    def __init__(self, state_path="paper_state.json", capital=1_000_000.0, reset=False):
        self.state_path = state_path
        if reset or not os.path.exists(state_path):
            self.s = {"cash": capital, "start_capital": capital, "next_id": 1,
                      "orders": [], "positions": {}, "trades": []}
            self._save()
        else:
            self.s = json.load(open(state_path))

    def _save(self):
        json.dump(self.s, open(self.state_path, "w"), indent=2)

    def _oid(self):
        oid = f"P{self.s['next_id']:06d}"
        self.s["next_id"] += 1
        return oid

    # ---- order placement (return order id) ----
    def _place_stop(self, symbol, qty, trigger, side):
        oid = self._oid()
        self.s["orders"].append({
            "id": oid, "symbol": symbol.upper(), "qty": int(qty),
            "side": side, "type": "SL-M", "trigger": float(trigger),
            "status": "pending", "placed_at": _now(),
        })
        self._save()
        return oid

    def place_buy_stop(self, symbol, qty, trigger):
        return self._place_stop(symbol, qty, trigger, "BUY")

    def place_sell_stop(self, symbol, qty, trigger):
        return self._place_stop(symbol, qty, trigger, "SELL")

    def modify_stop(self, order_id, new_trigger):
        for o in self.s["orders"]:
            if o["id"] == order_id and o["status"] == "pending":
                o["trigger"] = float(new_trigger)
                o["modified_at"] = _now()
        self._save()

    def cancel(self, order_id):
        for o in self.s["orders"]:
            if o["id"] == order_id and o["status"] == "pending":
                o["status"] = "cancelled"
        self._save()

    def market_sell(self, symbol, qty, price):
        """Immediate sell (used for partial booking)."""
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

    def open_orders(self, symbol=None):
        return [o for o in self.s["orders"]
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
    """Same interface, routed to real Fyers. Fills happen at the broker, so
    feed() is a no-op (positions/orders are read from Fyers)."""
    def __init__(self, client):
        self.c = client  # FyersClient

    def place_buy_stop(self, symbol, qty, trigger):
        return self.c.place_buy_stop(symbol, qty, trigger)

    def place_sell_stop(self, symbol, qty, trigger):
        return self.c.place_sell_stop(symbol, qty, trigger)

    def modify_stop(self, order_id, new_trigger):
        return self.c.modify_order(order_id, stopPrice=float(new_trigger))

    def cancel(self, order_id):
        return self.c.cancel_order(order_id)

    def market_sell(self, symbol, qty, price):
        # real market sell for partial book
        from fyers_client import nse
        return self.c.fy.place_order({"symbol": nse(symbol), "qty": int(qty), "type": 2,
                                      "side": -1, "productType": "CNC", "validity": "DAY",
                                      "limitPrice": 0, "stopPrice": 0, "offlineOrder": False})

    def feed(self, prices):
        return []  # broker fills natively

    def report(self):
        return {"mode": "LIVE", "realized_pnl": 0, "sell_wins": 0, "sell_losses": 0,
                "open_positions": self.positions(),
                "note": "live realized P&L: check Fyers tradebook"}

    # Fyers v3 order status codes: 1=Cancelled 2=Traded/Filled 4=Transit 5=Rejected 6=Pending
    OPEN_STATUS = {4, 6}

    def positions(self):
        resp = self.c.positions()
        out = []
        for p in (resp.get("netPositions") or []):
            netqty = p.get("netQty") or 0
            if netqty:
                sym = p.get("symbol", "").split(":")[-1].replace("-EQ", "")
                out.append({"symbol": sym, "qty": int(netqty), "avg": float(p.get("avgPrice", p.get("netAvg", 0)))})
        return out

    def open_orders(self, symbol=None):
        resp = self.c.orderbook()
        out = []
        for o in (resp.get("orderBook") or []):
            try:
                status = int(o.get("status"))
            except (TypeError, ValueError):
                status = None
            if status in self.OPEN_STATUS:
                sym = o.get("symbol", "").split(":")[-1].replace("-EQ", "")
                if symbol is None or sym == symbol.upper():
                    out.append({"id": o.get("id"), "symbol": sym,
                                "side": "BUY" if o.get("side") == 1 else "SELL",
                                "trigger": o.get("stopPrice"), "qty": o.get("qty")})
        return out
