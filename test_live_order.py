"""
SAFE live-order verification for the DIY Fyers path.

Places a LIMIT BUY of 1 share ~5% BELOW market — which RESTS and will NOT fill —
then verifies it in the orderbook, modifies it, and CANCELS it.
Zero fills, ~zero money at risk. Purpose: prove place_order / orderbook / modify /
cancel work against real Fyers, and print the REAL response shapes so we can
finalize LiveBroker's parsing.

YOU run this (it places a real, non-filling order on your account and cancels it).
It asks for a typed 'YES' before placing anything.

    python test_live_order.py
"""
import json
import time
from fyers_client import FyersClient, nse

SYMBOL = "IDEA"   # cheap + very liquid; edit if you prefer another
QTY = 1


def show(label, resp):
    print(f"\n===== {label} =====")
    try:
        print(json.dumps(resp, indent=2)[:1800])
    except Exception:
        print(resp)


def main():
    c = FyersClient.from_files()

    ltp = c.ltps([SYMBOL]).get(SYMBOL)
    print(f"LTP {SYMBOL} = {ltp}")
    if not ltp:
        print("No LTP returned — abort."); return

    limit = round(ltp * 0.95, 1)   # 5% below market -> rests, won't fill (within price band)
    print(f"\nPlan: LIMIT BUY {QTY} x {SYMBOL} @ {limit}  (5% below LTP {ltp}) — will REST, not fill.")
    print("This is a REAL order on your account. It will be cancelled at the end.")
    if input("Type YES to proceed: ").strip() != "YES":
        print("Aborted — nothing placed."); return

    # 1) PLACE
    place = c.fy.place_order({
        "symbol": nse(SYMBOL), "qty": QTY, "type": 1, "side": 1,   # type 1 = LIMIT, side 1 = BUY
        "productType": "CNC", "limitPrice": limit, "stopPrice": 0,
        "validity": "DAY", "offlineOrder": False,
    })
    show("PLACE (limit buy, 5% below)", place)
    oid = place.get("id")
    if not oid:
        print("\nNo order id in response — cannot continue. Inspect PLACE resp above."); return
    print(f"\norder id = {oid}")

    time.sleep(1.5)

    # 2) ORDERBOOK — reveals real field names for LiveBroker parsing
    show("ORDERBOOK (find our order; note the field names)", c.orderbook())

    time.sleep(0.5)

    # 3) MODIFY — move the limit a bit lower (still won't fill)
    show("MODIFY (limit -> 6% below)", c.modify_order(oid, limitPrice=round(ltp * 0.94, 1)))

    time.sleep(1.5)

    # 4) CANCEL — clean up, nothing left pending
    show("CANCEL", c.cancel_order(oid))

    time.sleep(0.5)
    show("ORDERBOOK after cancel (our order should be gone/cancelled)", c.orderbook())

    print("\nDone. If PLACE/MODIFY/CANCEL all returned ok and the order is cancelled,")
    print("the real Fyers order path works. Paste this output back so I can finalize LiveBroker parsing.")


if __name__ == "__main__":
    main()
