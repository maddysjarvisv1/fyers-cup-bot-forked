"""Behaviors 5-6: order/SL modification failure handling and partial-book
cancel-then-replace. Failure must surface (raise), not silently leave a stale SL
or double-place a sell-stop.
"""
import pytest

from bot import CupBot


def _state(entry=100.0, qty=100, phase="BE", sl=100.0, highest=110.0):
    return {"entry": entry, "qty_total": qty, "qty_remaining": qty,
            "phase": phase, "sl": sl, "highest": highest}


def _bot(double, state_path, ltp):
    bot = CupBot(double, lambda syms: {s: ltp for s in syms}, [],
                 state_path=str(state_path / "st.json"),
                 mode="LIVE", verbose=False)
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    return bot


# ---------------------------------------------------------------------------
# Behavior 5: modify_stop failure must surface (breakeven/trail path)
# ---------------------------------------------------------------------------
def test_exit_pass_raises_on_modify_stop_failure(double, tmp_path):
    double.fail["modify_stop"] = True
    bot = _bot(double, tmp_path, ltp=111.0)
    # ACTIVE -> above +10% triggers breakeven -> modify_stop
    bot.pos_state["WINNER"] = {"state": _state(phase="ACTIVE", sl=94.0, highest=100.0),
                               "sell_stop_id": "L00001"}

    with pytest.raises(Exception, match="modify_stop"):
        bot.cycle()


# ---------------------------------------------------------------------------
# Behavior 5: market_sell failure must surface (partial-book path)
# ---------------------------------------------------------------------------
def test_exit_pass_raises_on_market_sell_failure(double, tmp_path):
    double.fail["market_sell"] = True
    bot = _bot(double, tmp_path, ltp=120.0)
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}

    with pytest.raises(Exception, match="market_sell"):
        bot.cycle()


# ---------------------------------------------------------------------------
# Behavior 6: partial-book cancel-then-replace — cancel failure must NOT place
# a second sell-stop
# ---------------------------------------------------------------------------
def test_partial_cancel_failure_does_not_double_place(double, tmp_path):
    double.fail["cancel"] = True
    bot = _bot(double, tmp_path, ltp=120.0)
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}

    with pytest.raises(Exception, match="cancel"):
        bot.cycle()

    assert all(c[0] != "SELL" for c in double.placed), \
        "must not place a new (trail) sell-stop before the old one is cancelled"
