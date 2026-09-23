"""Behavior 4: re-arm / duplicate-entry race guard.

_entry_pass must NOT place a buy-stop for a symbol that already has an open
order, a position, is in pos_state, or was requested for entry this session and
its fill has not yet produced a position (in-flight).
"""
from bot import CupBot
from conftest import WL


def _state(entry=100.0, qty=100):
    return {"entry": entry, "qty_total": qty, "qty_remaining": qty,
            "phase": "ACTIVE", "sl": entry * 0.94, "highest": entry}


def _bot(double, state_path):
    return CupBot(double, lambda syms: {"WINNER": 99.0}, WL,
                  state_path=str(state_path / "st.json"),
                  mode="LIVE", verbose=False)


def test_entry_pass_skips_open_order(double, tmp_path):
    double.pending.append({"id": "X", "symbol": "WINNER", "qty": 100,
                           "trigger": 104, "side": "BUY", "status": "pending"})
    bot = _bot(double, tmp_path)
    bot._entry_pass({"WINNER": 99.0})
    assert all(c[0] != "BUY" for c in double.placed)


def test_entry_pass_skips_existing_position(double, tmp_path):
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(double, tmp_path)
    bot._entry_pass({"WINNER": 99.0})
    assert all(c[0] != "BUY" for c in double.placed)


def test_entry_pass_skips_symbol_in_pos_state(double, tmp_path):
    bot = _bot(double, tmp_path)
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}
    bot._entry_pass({"WINNER": 99.0})
    assert all(c[0] != "BUY" for c in double.placed)


def test_entry_pass_rearms_after_in_flight_order_cancelled(double, tmp_path):
    bot = _bot(double, tmp_path)
    prices = {"WINNER": 99.0}

    bot._entry_pass(prices)  # arms -> marks in-flight
    assert any(c[0] == "BUY" for c in double.placed)

    # finding 8: the buy-stop is cancelled/rejected/expired at the broker (no
    # open order, no position) -> _in_flight is cleared and the symbol re-arms.
    double.pending.clear()
    double.positions_map.clear()
    before = len([c for c in double.placed if c[0] == "BUY"])

    bot._entry_pass(prices)

    after = len([c for c in double.placed if c[0] == "BUY"])
    assert after == before + 1, "must re-arm once the stale buy-stop is gone"
