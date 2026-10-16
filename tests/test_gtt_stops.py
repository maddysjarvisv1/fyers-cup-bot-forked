"""
Offline tests for GTT (Good-Till-Triggered) protective stops and entry stops.

Covers all required invariants:
  1. Protective SELL stop and ENTRY BUY stop are placed with validity='GTT'
     while keeping SL-M semantics (type=3, limitPrice=0, stopPrice=align_tick(trigger)).
  2. The modify path and every cancel+replace path leave ZERO window without a
     protective stop (replacement is placed and resting BEFORE the old stop is
     cancelled; active protective stop count is >= 1 at every step).
  3. align_tick() is applied on every GTT price path (placement, in-place modify,
     and replace-before-cancel).
  4. Any failing GTT path or attempt to use DAY validity on a stop RAISES
     instead of silently downgrading to DAY.
"""
from __future__ import annotations

from decimal import Decimal
import os
import sys
from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bot import CupBot, LiveOrderError
from broker import LiveBroker, PaperBroker, align_tick
from fyers_client import FyersClient, FyersError


def _is_tick_multiple(price: float, tick: float = 0.05) -> bool:
    return (Decimal(str(price)) % Decimal(str(tick))) == 0


def _mock_fyers_client() -> FyersClient:
    c = FyersClient.__new__(FyersClient)
    c.app_id = "TEST-APP"
    c.warnings = []
    c.fy = MagicMock()
    c.fy.place_order.return_value = {"s": "ok", "id": "GTT-ORD-1"}
    c.fy.place_gtt_order.return_value = {"s": "ok", "id": "GTT-LEG-1"}
    c.fy.modify_order.return_value = {"s": "ok", "id": "GTT-ORD-1"}
    c.fy.modify_gtt_order.return_value = {"s": "ok", "id": "GTT-LEG-1"}
    c.fy.cancel_order.return_value = {"s": "ok", "id": "GTT-ORD-1"}
    c.fy.cancel_gtt_order.return_value = {"s": "ok", "id": "GTT-LEG-1"}
    return c


class StatefulGttFyersDouble:
    """Stateful FyersClient+SDK stand-in that tracks active protective stops at
    every single operation boundary so tests can prove there is NEVER a window
    where active_sell_stops drops to 0 during modify or cancel+replace."""

    def __init__(self, *, reject_modify: bool = False, fail_replace_place: bool = False, fail_cancel_old_id: str | None = None):
        self.reject_modify = reject_modify
        self.fail_replace_place = fail_replace_place
        self.fail_cancel_old_id = fail_cancel_old_id

        self.c = FyersClient.__new__(FyersClient)
        self.c.app_id = "TEST"
        self.c.warnings = []
        self.c.fy = MagicMock()

        self._seq = 0
        self.orders: dict[str, dict] = {}
        self.payloads: list[tuple[str, dict]] = []
        # Snapshot of (op_name, active_sell_stop_ids, active_sell_triggers) AFTER every SDK call
        self.protection_timeline: list[tuple[str, list[str], list[float]]] = []
        self._place_sell_calls = 0

        self.c.fy.place_order.side_effect = self._fy_place_order
        self.c.fy.modify_order.side_effect = self._fy_modify_order
        self.c.fy.cancel_order.side_effect = self._fy_cancel_order
        self.c.fy.orderbook.side_effect = self._fy_orderbook
        self.c.fy.positions.side_effect = lambda: {
            "s": "ok",
            "netPositions": [{"symbol": "NSE:WINNER-EQ", "netQty": 100, "avgPrice": 100.0}],
        }

    def _record_snapshot(self, op: str):
        active_sells = [
            (oid, o["stopPrice"])
            for oid, o in self.orders.items()
            if o["status"] == 6 and o["side"] == -1
        ]
        self.protection_timeline.append(
            (op, [oid for oid, _ in active_sells], [px for _, px in active_sells])
        )

    def _fy_place_order(self, data: dict):
        self.payloads.append(("place_order", dict(data)))
        if data.get("side") == -1:
            self._place_sell_calls += 1
            if self.fail_replace_place and self._place_sell_calls >= 2:
                self._record_snapshot("place_order_failed")
                return {"s": "error", "code": -99, "message": "replacement GTT stop rejected"}
        self._seq += 1
        oid = f"GTT{self._seq:04d}"
        self.orders[oid] = {
            "id": oid,
            "symbol": data["symbol"],
            "qty": data["qty"],
            "type": data["type"],
            "side": data["side"],
            "productType": data["productType"],
            "limitPrice": data["limitPrice"],
            "stopPrice": data["stopPrice"],
            "validity": data["validity"],
            "orderTag": data.get("orderTag", ""),
            "status": 6,  # Pending / resting
        }
        self._record_snapshot(f"place_order:{oid}")
        return {"s": "ok", "id": oid}

    def _fy_modify_order(self, data: dict):
        self.payloads.append(("modify_order", dict(data)))
        oid = str(data["id"])
        if self.reject_modify:
            self._record_snapshot(f"modify_order_rejected:{oid}")
            return {"s": "error", "code": -50, "message": "modify not supported on GTT order"}
        if oid not in self.orders or self.orders[oid]["status"] != 6:
            self._record_snapshot(f"modify_order_missing:{oid}")
            return {"s": "error", "code": -404, "message": "order not found"}
        if "stopPrice" in data:
            self.orders[oid]["stopPrice"] = data["stopPrice"]
        if "qty" in data:
            self.orders[oid]["qty"] = data["qty"]
        self._record_snapshot(f"modify_order:{oid}")
        return {"s": "ok", "id": oid}

    def _fy_cancel_order(self, data: dict):
        self.payloads.append(("cancel_order", dict(data)))
        oid = str(data["id"])
        if self.fail_cancel_old_id and oid == self.fail_cancel_old_id:
            self._record_snapshot(f"cancel_order_failed:{oid}")
            return {"s": "error", "code": -500, "message": "exchange cancel timeout"}
        if oid in self.orders:
            self.orders[oid]["status"] = 1  # Cancelled
        self._record_snapshot(f"cancel_order:{oid}")
        return {"s": "ok", "id": oid}

    def _fy_orderbook(self, data=None):
        return {"s": "ok", "orderBook": list(self.orders.values())}


# ===========================================================================
# 1. Protective SELL stop & ENTRY BUY stop placed with GTT validity + SL-M
# ===========================================================================
def test_entry_buy_stop_and_protective_sell_stop_payloads_have_gtt_validity():
    c = _mock_fyers_client()

    buy_id = c.place_buy_stop("NAZARA", 28, 436.30, order_tag="cup")
    assert buy_id == "GTT-ORD-1"
    buy_payload = c.fy.place_order.call_args_list[0][0][0]
    assert buy_payload == {
        "symbol": "NSE:NAZARA-EQ",
        "qty": 28,
        "type": 3,               # SL-M semantics preserved
        "side": 1,               # BUY
        "productType": "CNC",
        "limitPrice": 0,
        "stopPrice": 436.30,
        "validity": "GTT",       # GTT validity so breakout entry survives close
        "offlineOrder": False,
        "orderTag": "cup",
    }

    sell_id = c.place_sell_stop("NAZARA", 28, 410.10, order_tag="cup")
    assert sell_id == "GTT-ORD-1"
    sell_payload = c.fy.place_order.call_args_list[1][0][0]
    assert sell_payload == {
        "symbol": "NSE:NAZARA-EQ",
        "qty": 28,
        "type": 3,               # SL-M semantics preserved
        "side": -1,              # SELL protective stop
        "productType": "CNC",
        "limitPrice": 0,
        "stopPrice": 410.10,
        "validity": "GTT",       # GTT validity so open position is protected overnight
        "offlineOrder": False,
        "orderTag": "cup",
    }


def test_place_buy_gtt_and_place_sell_gtt_default_to_gtt_validity():
    c = _mock_fyers_client()

    res_buy = c.place_buy_gtt("NAZARA", 28, 436.30)
    assert res_buy["s"] == "ok"
    buy_gtt_payload = c.fy.place_gtt_order.call_args_list[0][0][0]
    assert buy_gtt_payload["validity"] == "GTT"
    assert buy_gtt_payload["side"] == 1
    assert buy_gtt_payload["productType"] == "CNC"
    assert buy_gtt_payload["orderInfo"]["leg1"] == {"qty": 28, "price": 0, "triggerPrice": 436.30}

    res_sell = c.place_sell_gtt("NAZARA", 28, 410.10)
    assert res_sell["s"] == "ok"
    sell_gtt_payload = c.fy.place_gtt_order.call_args_list[1][0][0]
    assert sell_gtt_payload["validity"] == "GTT"
    assert sell_gtt_payload["side"] == -1
    assert sell_gtt_payload["productType"] == "CNC"
    assert sell_gtt_payload["orderInfo"]["leg1"] == {"qty": 28, "price": 0, "triggerPrice": 410.10}


def test_cupbot_end_to_end_places_entry_and_protective_stops_with_gtt_validity(tmp_path):
    env = StatefulGttFyersDouble(reject_modify=False)
    lb = LiveBroker(env.c, tag="cup", state_path=str(tmp_path / "live.json"))
    wl = [{"symbol": "WINNER", "rim": 100.0, "trigger": 104.0, "qty": 100}]
    prices = {"WINNER": 99.5}
    bot = CupBot(
        lb,
        lambda syms: {s: prices[s] for s in syms if s in prices},
        wl,
        state_path=str(tmp_path / "bot.json"),
        mode="LIVE",
        verbose=False,
    )

    # 1. Near-rim cycle arms entry buy-stop as GTT (while position already exists on reconcile)
    env.c.fy.positions.side_effect = lambda: {"s": "ok", "netPositions": []}
    bot.cycle()
    assert len(env.payloads) == 1
    op, buy_payload = env.payloads[0]
    assert op == "place_order"
    assert buy_payload["side"] == 1
    assert buy_payload["type"] == 3
    assert buy_payload["validity"] == "GTT"
    assert buy_payload["stopPrice"] == 104.0

    # 2. Simulate fill -> position appears -> protective sell-stop placed as GTT
    for o in env.orders.values():
        o["status"] = 2
        o["filledQty"] = 100
        o["tradedPrice"] = 104.0
    env.c.fy.positions.side_effect = lambda: {
        "s": "ok",
        "netPositions": [{"symbol": "NSE:WINNER-EQ", "netQty": 100, "avgPrice": 104.0}],
    }
    prices["WINNER"] = 104.0
    bot.cycle()

    op2, sell_payload = env.payloads[-1]
    assert op2 == "place_order"
    assert sell_payload["side"] == -1
    assert sell_payload["type"] == 3
    assert sell_payload["validity"] == "GTT"
    assert sell_payload["stopPrice"] == 97.75  # 104 * 0.94 = 97.76 -> 97.75 aligned


def test_paper_broker_places_gtt_stops_that_survive_day_roll(tmp_path):
    sp = tmp_path / "paper.json"
    pb = PaperBroker(state_path=str(sp), reset=True)
    bid = pb.place_buy_stop("WINNER", 10, 104.0)
    sid = pb.place_sell_stop("WINNER", 10, 97.75)

    orders = {o["id"]: o for o in pb.open_orders()}
    assert orders[bid]["validity"] == "GTT"
    assert orders[sid]["validity"] == "GTT"

    # Simulate day roll: GTT stops must NOT expire at session close
    pb.s["day"] = "2000-01-01"
    pb._save()
    pb_next_day = PaperBroker(state_path=str(sp))
    open_after_roll = {o["id"]: o for o in pb_next_day.open_orders()}
    assert bid in open_after_roll
    assert sid in open_after_roll


# ===========================================================================
# 2. Modify path & Cancel+Replace path leave ZERO window without a stop
# ===========================================================================
def test_modify_path_leaves_no_window_without_protective_stop(tmp_path):
    """When in-place modify_order succeeds, the resting stop is updated in-place
    and active_sell_stops is [sid] at every step (never empty)."""
    env = StatefulGttFyersDouble(reject_modify=False)
    lb = LiveBroker(env.c, tag="cup", state_path=str(tmp_path / "live.json"))

    sid = lb.place_sell_stop("WINNER", 100, 94.0, order_tag="cup")
    assert sid == "GTT0001"
    env.protection_timeline.clear()

    # Trail stop via modify_stop
    res = lb.modify_stop(sid, 100.0)
    assert res["s"] == "ok"
    assert len(env.protection_timeline) == 1
    for op, active_ids, triggers in env.protection_timeline:
        assert len(active_ids) >= 1, f"Window without protective stop after {op}!"
    assert env.protection_timeline[-1] == ("modify_order:GTT0001", ["GTT0001"], [100.0])

    # Partial-book qty+trigger modify via modify_stop_qty
    env.protection_timeline.clear()
    res_qty = lb.modify_stop_qty(sid, 50, 108.0, symbol="WINNER", order_tag="cup")
    assert res_qty["s"] == "ok"
    for op, active_ids, _ in env.protection_timeline:
        assert len(active_ids) >= 1, f"Window without protective stop after {op}!"
    assert env.orders[sid]["qty"] == 50
    assert env.orders[sid]["stopPrice"] == 108.0


def test_cancel_replace_path_places_replacement_before_cancelling_old_stop(tmp_path):
    """When Fyers rejects modify_order on a GTT stop, LiveBroker must place the
    replacement GTT stop FIRST and cancel the old stop SECOND — so at every SDK
    step there is >= 1 active protective stop (1 -> 2 -> 1, never 0)."""
    env = StatefulGttFyersDouble(reject_modify=True)
    lb = LiveBroker(env.c, tag="cup", state_path=str(tmp_path / "live.json"))

    old_id = lb.place_sell_stop("WINNER", 100, 94.0, order_tag="cup")
    assert old_id == "GTT0001"
    env.protection_timeline.clear()

    # 1. modify_stop fallback -> replace-then-cancel
    res = lb.modify_stop(old_id, 100.0)
    new_id = res["id"]
    assert res["s"] == "ok"
    assert new_id == "GTT0002"
    assert new_id != old_id

    # Verify the exact call sequence and that protection count NEVER hit 0:
    # Step 1: modify_order_rejected:GTT0001 -> active: [GTT0001] (1 stop)
    # Step 2: place_order:GTT0002           -> active: [GTT0001, GTT0002] (2 stops: replacement in place BEFORE cancel!)
    # Step 3: cancel_order:GTT0001          -> active: [GTT0002] (1 stop)
    ops = [step[0] for step in env.protection_timeline]
    assert ops == [
        "modify_order_rejected:GTT0001",
        "place_order:GTT0002",
        "cancel_order:GTT0001",
    ]
    for op, active_ids, _ in env.protection_timeline:
        assert len(active_ids) >= 1, f"Position was unprotected at step {op}!"
    assert env.protection_timeline[1][1] == ["GTT0001", "GTT0002"]
    assert env.protection_timeline[2][1] == ["GTT0002"]
    assert env.orders[new_id]["validity"] == "GTT"
    assert env.orders[new_id]["stopPrice"] == 100.0

    # 2. modify_stop_qty fallback -> also replace-then-cancel with 0 unprotected window
    env.protection_timeline.clear()
    res2 = lb.modify_stop_qty(new_id, 50, 108.0, symbol="WINNER", order_tag="cup")
    third_id = res2["id"]
    assert res2["s"] == "ok"
    assert third_id == "GTT0003"

    ops2 = [step[0] for step in env.protection_timeline]
    assert ops2 == [
        "modify_order_rejected:GTT0002",
        "place_order:GTT0003",
        "cancel_order:GTT0002",
    ]
    for op, active_ids, _ in env.protection_timeline:
        assert len(active_ids) >= 1, f"Position was unprotected at step {op}!"
    assert env.protection_timeline[1][1] == ["GTT0002", "GTT0003"]
    assert env.protection_timeline[2][1] == ["GTT0003"]
    assert env.orders[third_id]["qty"] == 50
    assert env.orders[third_id]["stopPrice"] == 108.0
    assert env.orders[third_id]["validity"] == "GTT"


def test_cupbot_trail_updates_stop_id_on_gtt_cancel_replace_without_naked_window(tmp_path):
    """End-to-end CupBot trailing stop when Fyers rejects GTT modify_order:
    CupBot trails smoothly via replace-before-cancel and updates sell_stop_id."""
    env = StatefulGttFyersDouble(reject_modify=True)
    lb = LiveBroker(env.c, tag="cup", state_path=str(tmp_path / "live.json"))
    wl = [{"symbol": "WINNER", "rim": 100.0, "trigger": 104.0, "qty": 100}]
    prices = {"WINNER": 100.0}
    bot = CupBot(
        lb,
        lambda syms: {s: prices[s] for s in syms if s in prices},
        wl,
        state_path=str(tmp_path / "bot.json"),
        mode="LIVE",
        verbose=False,
    )

    # Cycle 1: Reconcile adopts WINNER @ 100 x 100 -> places initial GTT stop GTT0001 @ 94.0
    bot.cycle()
    first_id = bot.pos_state["WINNER"]["sell_stop_id"]
    assert first_id == "GTT0001"

    # Cycle 2: Price moves to +11% (111.0) -> breakeven SL 100.0 -> modify rejected -> replace-then-cancel
    env.protection_timeline.clear()
    prices["WINNER"] = 111.0
    bot.cycle()

    second_id = bot.pos_state["WINNER"]["sell_stop_id"]
    assert second_id == "GTT0002"
    for op, active_ids, _ in env.protection_timeline:
        assert len(active_ids) >= 1, f"Unprotected window during CupBot breakeven at {op}!"
    assert env.protection_timeline[-1][1] == ["GTT0002"]
    assert env.orders["GTT0002"]["stopPrice"] == 100.0
    assert env.orders["GTT0002"]["validity"] == "GTT"


def test_replace_failure_leaves_original_stop_intact_and_raises(tmp_path):
    """If placing the replacement GTT stop fails, the old stop is NEVER cancelled
    (no unprotected window), and the call raises."""
    env = StatefulGttFyersDouble(reject_modify=True, fail_replace_place=True)
    lb = LiveBroker(env.c, tag="cup", state_path=str(tmp_path / "live.json"))

    old_id = lb.place_sell_stop("WINNER", 100, 94.0, order_tag="cup")
    env.protection_timeline.clear()

    with pytest.raises(FyersError, match="replacement GTT stop rejected"):
        lb.modify_stop(old_id, 100.0)

    # Old stop was never cancelled and is still resting
    for op, active_ids, _ in env.protection_timeline:
        assert active_ids == [old_id], f"Old stop lost at {op}!"
    assert env.orders[old_id]["status"] == 6


def test_replace_cancel_failure_rolls_back_new_stop_keeps_old_stop_and_raises(tmp_path):
    """If cancelling the old stop fails after the replacement is placed, the
    replacement is rolled back so we don't stack two stops, the old stop remains
    resting (0 unprotected window), and the call raises."""
    env = StatefulGttFyersDouble(reject_modify=True, fail_cancel_old_id="GTT0001")
    lb = LiveBroker(env.c, tag="cup", state_path=str(tmp_path / "live.json"))

    old_id = lb.place_sell_stop("WINNER", 100, 94.0, order_tag="cup")
    env.protection_timeline.clear()

    with pytest.raises(RuntimeError, match="cancel of GTT0001 unconfirmed"):
        lb.modify_stop(old_id, 100.0)

    for op, active_ids, _ in env.protection_timeline:
        assert len(active_ids) >= 1, f"Unprotected window at {op}!"
    # At the end, GTT0002 was rolled back and GTT0001 is the sole active stop
    assert env.protection_timeline[-1][1] == ["GTT0001"]


# ===========================================================================
# 3. align_tick() applied on every GTT price path
# ===========================================================================
def test_align_tick_applied_on_every_gtt_price_path(tmp_path):
    # 1. LiveBroker aligns every GTT path (buy-stop, sell-stop, modify, cancel+replace, qty-modify)
    env = StatefulGttFyersDouble(reject_modify=True)
    lb = LiveBroker(env.c, tag="cup", state_path=str(tmp_path / "live_align.json"))

    buy_oid = lb.place_buy_stop("NAZARA", 28, 436.28)
    assert env.orders[buy_oid]["stopPrice"] == 436.30
    assert env.orders[buy_oid]["validity"] == "GTT"
    assert _is_tick_multiple(env.orders[buy_oid]["stopPrice"])

    sell_oid = lb.place_sell_stop("NAZARA", 28, 410.122)
    assert env.orders[sell_oid]["stopPrice"] == 410.10
    assert env.orders[sell_oid]["validity"] == "GTT"
    assert _is_tick_multiple(env.orders[sell_oid]["stopPrice"])

    # LiveBroker cancel+replace path aligns unaligned trigger (471.1824 -> 471.20)
    res = lb.modify_stop(sell_oid, 471.1824)
    new_oid = res["id"]
    assert env.orders[new_oid]["stopPrice"] == 471.20
    assert env.orders[new_oid]["validity"] == "GTT"
    assert _is_tick_multiple(env.orders[new_oid]["stopPrice"])

    # LiveBroker modify_stop_qty cancel+replace path aligns unaligned trigger (485.026 -> 485.05)
    res_qty = lb.modify_stop_qty(new_oid, 14, 485.026, symbol="NAZARA")
    new_oid_2 = res_qty["id"]
    assert env.orders[new_oid_2]["stopPrice"] == 485.05
    assert env.orders[new_oid_2]["validity"] == "GTT"
    assert _is_tick_multiple(env.orders[new_oid_2]["stopPrice"])

    # 2. PaperBroker aligns every GTT path
    pb = PaperBroker(state_path=str(tmp_path / "paper_align.json"))
    pb_buy = pb.place_buy_stop("NAZARA", 28, 436.28)
    pb_sell = pb.place_sell_stop("NAZARA", 28, 410.122)
    by_id = {o["id"]: o for o in pb.s["orders"]}
    assert by_id[pb_buy]["trigger"] == 436.30
    assert by_id[pb_buy]["validity"] == "GTT"
    assert by_id[pb_sell]["trigger"] == 410.10
    assert by_id[pb_sell]["validity"] == "GTT"
    pb.modify_stop(pb_sell, 471.1824)
    by_id = {o["id"]: o for o in pb.s["orders"]}
    assert by_id[pb_sell]["trigger"] == 471.20

    # 3. FyersClient.replace_sell_stop aligns unaligned trigger before placing replacement GTT
    rep_oid = env.c.replace_sell_stop(new_oid_2, "NAZARA", 14, 490.122)
    assert env.orders[rep_oid]["stopPrice"] == 490.10
    assert env.orders[rep_oid]["validity"] == "GTT"

    # 4. Direct FyersClient GTT methods invoke align_tick() on every GTT price
    import broker as broker_mod
    aligned_inputs = []
    real_align = broker_mod.align_tick

    def spy_align(price, tick=None):
        res = real_align(price, tick=tick)
        aligned_inputs.append((price, res))
        return res

    monkeypatch_orig = broker_mod.align_tick
    try:
        broker_mod.align_tick = spy_align
        c = _mock_fyers_client()
        c.place_buy_stop("NAZARA", 28, 436.30)
        assert c.fy.place_order.call_args[0][0]["stopPrice"] == 436.30
        assert c.fy.place_order.call_args[0][0]["validity"] == "GTT"

        c.place_sell_stop("NAZARA", 28, 410.10)
        assert c.fy.place_order.call_args[0][0]["stopPrice"] == 410.10
        assert c.fy.place_order.call_args[0][0]["validity"] == "GTT"

        c.place_buy_gtt("NAZARA", 28, 436.30)
        assert c.fy.place_gtt_order.call_args[0][0]["orderInfo"]["leg1"]["triggerPrice"] == 436.30

        c.place_sell_gtt("NAZARA", 28, 410.10)
        assert c.fy.place_gtt_order.call_args[0][0]["orderInfo"]["leg1"]["triggerPrice"] == 410.10

        c.modify_order("ORD-1", stop_price=420.05)
        assert c.fy.modify_order.call_args[0][0]["stopPrice"] == 420.05

        c.modify_gtt_order("GTT-LEG-1", 471.20, 14)
        assert c.fy.modify_gtt_order.call_args[0][0]["orderInfo"]["leg1"]["triggerPrice"] == 471.20
    finally:
        broker_mod.align_tick = monkeypatch_orig

    assert [r for _, r in aligned_inputs] == [436.30, 410.10, 436.30, 410.10, 420.05, 471.20]


# ===========================================================================
# 4. Failing GTT path RAISES instead of silently downgrading to DAY
# ===========================================================================
def test_non_gtt_validity_on_stops_raises_and_never_downgrades_to_day():
    c = _mock_fyers_client()

    # Explicitly passing validity="DAY" or "IOC" to any stop/GTT method must raise FyersError
    with pytest.raises(FyersError, match=r"validity"):
        c.place_buy_stop("NAZARA", 28, 436.30, validity="DAY")
    with pytest.raises(FyersError, match=r"validity"):
        c.place_sell_stop("NAZARA", 28, 410.10, validity="DAY")
    with pytest.raises(FyersError, match=r"validity"):
        c.place_buy_gtt("NAZARA", 28, 436.30, validity="DAY")
    with pytest.raises(FyersError, match=r"validity"):
        c.place_sell_gtt("NAZARA", 28, 410.10, validity="DAY")
    with pytest.raises(FyersError, match=r"validity"):
        c.modify_order("ORD-1", stop_price=420.00, validity="DAY")
    with pytest.raises(FyersError, match=r"validity"):
        c.modify_gtt_order("GTT-1", 420.00, 28, validity="DAY")

    # Zero broker calls were made
    assert c.fy.place_order.call_count == 0
    assert c.fy.place_gtt_order.call_count == 0
    assert c.fy.modify_order.call_count == 0
    assert c.fy.modify_gtt_order.call_count == 0


def test_failing_gtt_broker_call_raises_and_never_falls_back_to_day(tmp_path):
    c = _mock_fyers_client()
    c.fy.place_order.return_value = {"s": "error", "code": -50, "message": "GTT rejected by broker"}
    c.fy.place_gtt_order.return_value = {"s": "error", "code": -50, "message": "GTT rejected by broker"}

    with pytest.raises(FyersError, match="GTT rejected by broker"):
        c.place_buy_stop("NAZARA", 28, 436.30)
    with pytest.raises(FyersError, match="GTT rejected by broker"):
        c.place_sell_stop("NAZARA", 28, 410.10)
    with pytest.raises(FyersError, match="GTT rejected by broker"):
        c.place_buy_gtt("NAZARA", 28, 436.30)
    with pytest.raises(FyersError, match="GTT rejected by broker"):
        c.place_sell_gtt("NAZARA", 28, 410.10)

    # Verify none of the calls ever retried with validity="DAY"
    for call in c.fy.place_order.call_args_list:
        assert call[0][0]["validity"] == "GTT"
    for call in c.fy.place_gtt_order.call_args_list:
        assert call[0][0]["validity"] == "GTT"

    # PaperBroker also refuses non-GTT stop validity
    pb = PaperBroker(state_path=str(tmp_path / "p.json"), reset=True)
    with pytest.raises(ValueError, match="GTT"):
        pb._place_stop("NAZARA", 28, 436.30, "BUY", validity="DAY")
