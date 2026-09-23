"""Round-2 live-risk fixes: CRITICAL 1-4 and MAJOR 5-9 from the reviewer report.

Each test maps to a specific finding. Mocks only (LiveBrokerDouble from conftest).
"""
import json

import pytest

from bot import CupBot, LiveOrderError, _extract_order_id, _request_ok
from conftest import WL


def _state(entry=100.0, qty_total=100, qty_remaining=None, phase="ACTIVE",
           sl=None, highest=None):
    if qty_remaining is None:
        qty_remaining = qty_total
    if sl is None:
        sl = round(entry * 0.94, 2)
    if highest is None:
        highest = entry
    return {"entry": entry, "qty_total": qty_total, "qty_remaining": qty_remaining,
            "phase": phase, "sl": sl, "highest": highest}


def _bot(double, state_path, price_map, watchlist=None, mode="LIVE", alerts=True):
    return CupBot(double, lambda syms: {s: price_map[s] for s in syms if s in price_map},
                  watchlist if watchlist is not None else WL,
                  state_path=str(state_path / "st.json"),
                  mode=mode, alerts=alerts, verbose=False)


# ---------------------------------------------------------------------------
# MAJOR 7 — _request_ok strictness + order-id normalization (unit)
# ---------------------------------------------------------------------------
def test_request_ok_strict_for_mutators():
    # None -> success (PaperBroker mutators return None)
    assert _request_ok(None) is True
    # dict: only s == "ok" (case-insensitive) counts; missing s is NOT success
    assert _request_ok({"s": "ok"}) is True
    assert _request_ok({"s": "OK"}) is True
    assert _request_ok({"s": "ERROR"}) is False
    assert _request_ok({"s": "error"}) is False
    assert _request_ok({}) is False
    # strings: non-empty truthy -> success, empty -> failure
    assert _request_ok("") is False
    assert _request_ok("L001") is True


def test_extract_order_id_normalizes_place_results():
    assert _extract_order_id(None) is None
    assert _extract_order_id("") is None
    assert _extract_order_id("L001") == "L001"
    assert _extract_order_id({"s": "ok"}) is None          # no id field
    assert _extract_order_id({"s": "ok", "id": "X"}) == "X"
    assert _extract_order_id({"s": "error"}) is None


def test_live_place_sell_stop_dict_result_normalizes_stored_id(double, tmp_path):
    # finding 7: broker returns {"s":"ok","id":"X"} -> stored sell_stop_id is "X"
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.dict_order_ids = True
    bot = _bot(double, tmp_path, {"WINNER": 100.0})
    bot.cycle()
    assert bot.pos_state["WINNER"]["sell_stop_id"] == "L00001"
    # and the id is a bare string, not a dict
    assert isinstance(bot.pos_state["WINNER"]["sell_stop_id"], str)


def test_live_place_sell_stop_none_is_failure(double, tmp_path):
    # finding 7: a swallowed exception (None) from a LIVE place_* is a failure
    class NoneSellDouble(double.__class__):
        def place_sell_stop(self, symbol, qty, trigger, order_tag="cupbot"):
            self.calls.append("place_sell_stop")
            return None

    double = NoneSellDouble()
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0})
    with pytest.raises(LiveOrderError):
        bot.cycle()


# ---------------------------------------------------------------------------
# CRITICAL 1 — persist state even when a live op raises mid-cycle
# ---------------------------------------------------------------------------
def test_two_symbol_reconcile_second_failure_persists_first(double, tmp_path):
    wl2 = [{"symbol": "A", "rim": 100, "trigger": 104, "qty": 100},
           {"symbol": "B", "rim": 100, "trigger": 104, "qty": 100}]
    double.positions_map["A"] = {"qty": 100, "avg": 100.0}
    double.positions_map["B"] = {"qty": 100, "avg": 100.0}
    double.fail_symbols["place_sell_stop"] = {"B"}
    bot = _bot(double, tmp_path, {"A": 100.0, "B": 100.0}, watchlist=wl2)

    with pytest.raises(LiveOrderError):
        bot.cycle()

    # A's sell-stop succeeded and must already be persisted on disk
    saved = json.load(open(tmp_path / "st.json"))
    assert "A" in saved, "A's state must be persisted even though B raised"

    # restart: A must NOT receive a second SELL-stop
    double.fail_symbols.clear()
    double.placed.clear()
    bot2 = CupBot(double, lambda syms: {s: {"A": 100.0, "B": 100.0}[s] for s in syms},
                  wl2, state_path=str(tmp_path / "st.json"),
                  mode="LIVE", verbose=False)
    bot2.cycle()
    assert all(c[1] != "A" for c in double.placed), "must not duplicate A's SELL after restart"


# ---------------------------------------------------------------------------
# CRITICAL 2 — no naked position when the replace-stop fails after cancel
# ---------------------------------------------------------------------------
def test_partial_replace_failure_marks_sl_missing_then_self_heals(double, alert_log, tmp_path):
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 120.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="BE", sl=100.0, highest=110.0),
                               "sell_stop_id": "L00001"}
    double.fail_symbols["place_sell_stop"] = {"WINNER"}

    with pytest.raises(LiveOrderError):
        bot.cycle()

    # state persisted with SL marked missing (sell_stop_id None)
    saved = json.load(open(tmp_path / "st.json"))
    assert saved["WINNER"]["sell_stop_id"] is None
    # the partial was booked (qty_remaining reduced) and persisted
    assert saved["WINNER"]["state"]["qty_remaining"] == 50

    # the broker reflects the 50-lot sale; next cycle self-heals the SL
    double.fail_symbols.clear()
    double.positions_map["WINNER"] = {"qty": 50, "avg": 100.0}
    double.placed.clear()

    bot.cycle()

    sells = [c for c in double.placed if c[0] == "SELL"]
    assert len(sells) == 1, "self-heal must re-place exactly one protective sell-stop"
    assert sells[0][2] == 50
    assert bot.pos_state["WINNER"]["sell_stop_id"]


# ---------------------------------------------------------------------------
# CRITICAL 3 — partial-book order: cancel FIRST, then market-sell, then re-place
# ---------------------------------------------------------------------------
def test_partial_book_cancels_before_selling_before_replacing(double, tmp_path):
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 120.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="BE", sl=100.0, highest=110.0),
                               "sell_stop_id": "L00001"}

    bot.cycle()

    assert double.calls == ["cancel", "market_sell", "place_sell_stop"], \
        "must cancel the old stop BEFORE selling the partial, and place the new stop last"


def test_market_sell_failure_still_cancelled_first_and_persists_partial(double, tmp_path):
    # finding 3/#1: on market_sell failure, cancel happened first, no SELL placed,
    # and the reduced qty_remaining is persisted.
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.fail["market_sell"] = True
    bot = _bot(double, tmp_path, {"WINNER": 120.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="BE", sl=100.0, highest=110.0),
                               "sell_stop_id": "L00001"}

    with pytest.raises(LiveOrderError, match="market_sell"):
        bot.cycle()

    assert double.calls[0] == "cancel"
    assert all(c[0] != "SELL" for c in double.placed)
    saved = json.load(open(tmp_path / "st.json"))
    assert saved["WINNER"]["state"]["qty_remaining"] == 50


# ---------------------------------------------------------------------------
# CRITICAL 4 — gone-position cleanup: transient empty + cancel the old stop
# ---------------------------------------------------------------------------
def test_gone_position_requires_confirmation_then_cancels_stop(double, alert_log, tmp_path):
    bot = _bot(double, tmp_path, {})
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}

    bot.cycle()  # 1st absent cycle -> not yet confirmed
    assert "WINNER" in bot.pos_state
    assert alert_log["exit_"] == []

    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 94.0, "side": "SELL", "status": "pending"})
    bot.cycle()  # 2nd absent cycle -> confirmed gone
    assert "WINNER" not in bot.pos_state
    assert "L00001" in double.cancelled, "must cancel the orphaned sell-stop"
    assert alert_log["exit_"]


def test_transient_empty_positions_no_duplicate_sell_stop(double, alert_log, tmp_path):
    bot = _bot(double, tmp_path, {})
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}

    bot.cycle()  # ordinal absence (API hiccup)
    assert "WINNER" in bot.pos_state

    # position reappears with its stop still resting
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 94.0, "side": "SELL", "status": "pending"})
    double.placed.clear()

    bot.cycle()

    assert "WINNER" in bot.pos_state
    assert all(c[0] != "SELL" for c in double.placed), "no duplicate SELL-stop for reappeared position"
    assert alert_log["exit_"] == []


def test_positions_exception_skips_cleanup_and_alerts(double, alert_log, tmp_path):
    bot = _bot(double, tmp_path, {})
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}
    double.positions_error = True

    bot._reconcile_live()

    assert "WINNER" in bot.pos_state, "must not wipe state on a failed positions() call"
    assert alert_log["error"], "must alert on a failed positions() call"


# ---------------------------------------------------------------------------
# MAJOR 5 — reconcile only strategy positions
# ---------------------------------------------------------------------------
def test_foreign_position_gets_no_sell_stop(double, alert_log, tmp_path):
    double.positions_map["TATA"] = {"qty": 100, "avg": 100.0}  # not in watchlist
    bot = _bot(double, tmp_path, {"TATA": 100.0}, watchlist=WL)  # WL has WINNER only

    bot.cycle()

    assert all(c[1] != "TATA" for c in double.placed), "must not place an order for a foreign position"
    assert "TATA" not in bot.pos_state
    assert alert_log["entry"] == []


# ---------------------------------------------------------------------------
# MAJOR 6 — partial fills / qty drift -> re-place stop to full qty
# ---------------------------------------------------------------------------
def test_partial_fill_qty_drift_replaces_stop_to_full_qty(double, tmp_path):
    double.positions_map["WINNER"] = {"qty": 40, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0}, watchlist=WL)

    bot.cycle()  # adopt at 40
    assert bot.pos_state["WINNER"]["state"]["qty_total"] == 40
    assert double.placed[-1] == ("SELL", "WINNER", 40, 94.0)

    # later fills bring broker qty to 100
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.placed.clear()

    bot.cycle()

    sells = [c for c in double.placed if c[0] == "SELL"]
    assert len(sells) == 1
    assert sells[0][2] == 100, "stop qty must be updated to the full position"
    assert bot.pos_state["WINNER"]["state"]["qty_total"] == 100
    assert bot.pos_state["WINNER"]["state"]["qty_remaining"] == 100


# ---------------------------------------------------------------------------
# MAJOR 8 — _in_flight lifecycle (check buy result + clear on cancel/expiry)
# ---------------------------------------------------------------------------
def test_buy_stop_failure_does_not_block_or_alert(double, alert_log, tmp_path):
    double.fail["place_buy_stop"] = True
    bot = _bot(double, tmp_path, {"WINNER": 99.0}, watchlist=WL)

    with pytest.raises(LiveOrderError):
        bot._entry_pass({"WINNER": 99.0})

    assert "WINNER" not in bot._in_flight, "a failed arm must not block the symbol"
    assert alert_log["armed"] == [], "a failed arm must not fire a false ARMED alert"


def test_pending_buy_stop_prevents_duplicate_arm(double, tmp_path):
    bot = _bot(double, tmp_path, {"WINNER": 99.0}, watchlist=WL)
    bot._entry_pass({"WINNER": 99.0})  # arm
    bot._entry_pass({"WINNER": 99.0})  # pending buy present -> skip
    buys = [c for c in double.placed if c[0] == "BUY"]
    assert len(buys) == 1


def test_in_flight_rearms_after_order_cancelled(double, tmp_path):
    bot = _bot(double, tmp_path, {"WINNER": 99.0}, watchlist=WL)
    bot._entry_pass({"WINNER": 99.0})  # arm -> in-flight
    assert any(c[0] == "BUY" for c in double.placed)

    # buy-stop cancelled/rejected/expired at broker; no position, no order
    double.pending.clear()
    double.positions_map.clear()
    assert "WINNER" in bot._in_flight

    bot._entry_pass({"WINNER": 99.0})  # clear stale in-flight -> re-arm

    buys = [c for c in double.placed if c[0] == "BUY"]
    assert len(buys) == 2, "must re-arm once the stale buy-stop is gone"


# ---------------------------------------------------------------------------
# MAJOR 9 — exit P&L uses qty_remaining (post-partial), vs stop price
# ---------------------------------------------------------------------------
def test_exit_pnl_uses_qty_remaining_after_partial(double, alert_log, tmp_path):
    bot = _bot(double, tmp_path, {})
    bot.pos_state["WINNER"] = {"state": _state(qty_total=100, qty_remaining=50, sl=110.0),
                               "sell_stop_id": "L00001"}

    bot.cycle()
    bot.cycle()  # confirm gone across 2 cycles

    _mode, sym, price, qty, pnl = alert_log["exit_"][0]
    assert qty == 50, "exit P&L must use qty_remaining (50), not qty_total (100)"
    assert pnl == (110.0 - 100.0) * 50
