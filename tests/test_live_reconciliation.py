"""Behaviors 1-3: live fill/exit reconciliation and no-duplicate-sell-stop.

All LIVE mode. All mock-only (LiveBrokerDouble from conftest).
"""
import pytest

from bot import CupBot


def _state(entry=100.0, qty=100, qty_remaining=100, phase="ACTIVE",
           sl=94.0, highest=100.0):
    return {"entry": entry, "qty_total": qty, "qty_remaining": qty_remaining,
            "phase": phase, "sl": sl, "highest": highest}


def _bot(double, state_path, price_map, watchlist=None, mode="LIVE", alerts=True):
    return CupBot(double, lambda syms: {s: price_map[s] for s in syms if s in price_map},
                  watchlist if watchlist is not None else [],
                  state_path=str(state_path / "st.json"),
                  mode=mode, alerts=alerts, verbose=False)


# ---------------------------------------------------------------------------
# Behavior 1: NEW POSITION -> ENTRY reconciliation
# ---------------------------------------------------------------------------
def test_live_reconcile_new_position_creates_pos_state_and_sell_stop(double, alert_log, tmp_path):
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    # finding 5: reconcile only adopts strategy symbols (here WINNER is watchlisted)
    bot = _bot(double, tmp_path, {"WINNER": 100.0},
               watchlist=[{"symbol": "WINNER", "rim": 100, "trigger": 104, "qty": 100}])

    bot.cycle()

    assert "WINNER" in bot.pos_state
    ps = bot.pos_state["WINNER"]
    assert ps["sell_stop_id"]
    assert ps["state"]["entry"] == 100.0
    assert ps["state"]["qty_total"] == 100
    assert ps["state"]["qty_remaining"] == 100

    sells = [c for c in double.placed if c[0] == "SELL"]
    assert len(sells) == 1
    assert sells[0] == ("SELL", "WINNER", 100, 94.0)

    assert alert_log["entry"], "expected ENTRY alert on reconcile"
    assert alert_log["entry"][0][1] == "WINNER"


# ---------------------------------------------------------------------------
# Behavior 2: POSITION GONE -> STOP_EXIT cleanup
# ---------------------------------------------------------------------------
def test_live_reconcile_gone_position_cleans_up_no_phantom_sell_stop(double, alert_log, tmp_path):
    bot = _bot(double, tmp_path, {})
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}

    bot.cycle()  # 1st absent cycle -> not yet confirmed (transient-empty guard)
    assert "WINNER" in bot.pos_state
    assert alert_log["exit_"] == []

    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 94.0, "side": "SELL", "status": "pending"})
    bot.cycle()  # 2nd absent cycle -> confirmed gone

    assert "WINNER" not in bot.pos_state
    assert alert_log["exit_"], "expected EXIT alert on reconcile"
    assert "L00001" in double.cancelled, "must cancel the orphaned sell-stop"
    # no phantom sell-stop must be placed for the gone position
    assert all(c[0] != "SELL" for c in double.placed)


# ---------------------------------------------------------------------------
# Behavior 3: no duplicate SELL-stop when pos_state already has sell_stop_id
# ---------------------------------------------------------------------------
def test_live_reconcile_does_not_duplicate_sell_stop(double, alert_log, tmp_path):
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0})
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}
    # the stop must actually be resting at the broker, or the exit-pass
    # stale-stop check (bot.py) will treat it as vanished and re-arm a new
    # one — which is exactly the "duplicate stop" this test guards against.
    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 94.0, "side": "SELL", "status": "pending"})

    bot.cycle()

    assert all(c[0] != "SELL" for c in double.placed), "must not place a 2nd sell-stop"
    assert bot.pos_state["WINNER"]["sell_stop_id"] == "L00001"
    assert alert_log["entry"] == [], "must not re-alert ENTRY for existing position"
