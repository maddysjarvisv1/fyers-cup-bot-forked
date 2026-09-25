#!/usr/bin/env python3
"""One-shot SEND-VERIFY-CANCEL probe against live Fyers.

Places ONE 1-share CNC LIMIT BUY 5% below market (== will REST, not fill),
reads the real returned order id + orderbook, then CANCELS it in the same run.
Purpose: prove the order SEND path works against the live account (place ->
id -> orderbook -> cancel) with nothing left resting.

Safety: 1 share, LIMIT 5% below LTP, cancelled in-script. Uses the bot's own
FyersClient so it exercises the exact code the bot will run.
"""
import json, time, sys
import force_ipv4  # noqa: must be first — force IPv4 egress for Fyers IP whitelist
from fyers_client import FyersClient, nse

SYMBOL = "IDEA"
QTY = 1


def show(label, resp):
    s = json.dumps(resp)[:300]
    print(f"[{label}] {s}")


def main():
    c = FyersClient.from_files()
    ltp = c.ltps([SYMBOL]).get(SYMBOL)
    if not ltp:
        print(f"ABORT: no live LTP for {SYMBOL}"); sys.exit(2)
    limit = round(ltp * 0.95, 1)
    print(f"LTP {SYMBOL} = {ltp}  ->  LIMIT BUY {QTY} @ {limit} (5% below, rests, won't fill)\n")

    # 1) PLACE — real send
    place = c.fy.place_order({
        "symbol": nse(SYMBOL), "qty": QTY, "type": 1, "side": 1,   # type1=LIMIT, side1=BUY
        "productType": "CNC", "limitPrice": limit, "stopPrice": 0,
        "validity": "DAY", "orderTag": "cuptest",
        "disclosedQty": 0, "offeredQty": 0, "triggerPrice": 0,
    })
    show("PLACE", place)
    oid = place.get("id")
    if not oid:
        print("NO ORDER ID in response — plck the real shape and stop."); sys.exit(1)
    print(f"  -> ORDER ID: {oid}")

    # 2) ORDERBOOK — confirm visible
    time.sleep(1)
    o = c.orderbook()
    print("  -> my order present in orderbook:", any(str(x.get("id")) == str(oid) for x in (o.get("orderBook") or []) if isinstance(x, dict)))

    # 3) CANCEL — close it out (ALWAYS runs; nothing left resting)
    show("CANCEL", c.cancel_order(oid))
    time.sleep(1)
    ob = c.orderbook()
    still = any(str(x.get("id")) == str(oid) for x in (ob.get("orderBook") or []) if isinstance(x, dict))
    print("  -> order still in orderbook after cancel:", still)
    print("\nDONE. Real order was placed and cancelled; nothing left resting." if not still else "\nWARN: order still present — review.")


if __name__ == "__main__":
    main()