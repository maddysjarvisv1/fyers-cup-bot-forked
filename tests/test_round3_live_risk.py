"""Round-3 live-risk fixes: R3-1..R3-4 from the round-2 reviewer report.

Each test maps to a specific remaining money-exposure finding:
  R3-1  _reconcile_qty must cancel the old stop before re-placing (no stacking).
  R3-2  an entry-side failure must not starve exit management / the self-heal.
  R3-3  a failed orphan-stop cancel during gone-cleanup must not be swallowed.
  R3-4  None from a LIVE mutator is a failure (not success).

Mocks only (LiveBrokerDouble from conftest). No network, no real orders.
"""
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
# R3-1 (CRITICAL, NEW-1): qty-drift reconcile must cancel the old stop first
# ---------------------------------------------------------------------------
def test_reconcile_qty_cancels_old_stop_before_replacing(double, tmp_path):
    double.positions_map["WINNER"] = {"qty": 40, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0}, alerts=False)

    bot.cycle()  # adopt the 40-share partial fill
    old_id = bot.pos_state["WINNER"]["sell_stop_id"]
    assert old_id
    assert double.placed[-1] == ("SELL", "WINNER", 40, 94.0)

    # later fills bring the broker position to 100 -> qty drift
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.placed.clear()

    bot.cycle()

    assert old_id in double.cancelled, "must cancel the old 40-lot stop before re-placing"
    resting = [o for o in double.open_orders() if o["side"] == "SELL"]
    assert len(resting) == 1, "exactly ONE SELL-stop must rest after qty drift"
    assert sum(o["qty"] for o in resting) <= 100, \
        "no oversell: resting SELL-stop qty must not exceed the held qty"
    assert bot.pos_state["WINNER"]["sell_stop_id"] == resting[0]["id"]


def test_reconcile_qty_cancel_failure_does_not_stack_and_alerts(double, alert_log, tmp_path):
    double.positions_map["WINNER"] = {"qty": 40, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0})

    bot.cycle()  # adopt at 40
    old_id = bot.pos_state["WINNER"]["sell_stop_id"]
    double.fail["cancel"] = "always"
    double.placed.clear()

    bot._reconcile_qty("WINNER", {"qty": 100, "avg": 100.0})

    assert old_id in double.cancel_attempts, "must attempt to cancel the old stop"
    assert all(c[0] != "SELL" for c in double.placed), \
        "must NOT stack a new stop when the old-stop cancel fails"
    assert bot.pos_state["WINNER"]["sell_stop_id"] is None, \
        "leave SL missing so the self-heal owns re-placement"
    assert bot._orphan_stops.get("WINNER") == old_id, "orphan stop id must be retained"
    assert alert_log["error"], "must alert CRITICAL on a failed drift cancel"


# ---------------------------------------------------------------------------
# R3-2 (CRITICAL, NEW-3): entry failure must not starve exit / self-heal
# ---------------------------------------------------------------------------
def test_entry_failure_does_not_starve_exit_or_heal(double, alert_log, tmp_path):
    wl = [{"symbol": "BAD", "rim": 100, "trigger": 104, "qty": 100}]
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.fail_symbols["place_buy_stop"] = {"BAD"}
    bot = _bot(double, tmp_path, {"BAD": 99.0, "WINNER": 118.0}, watchlist=wl)
    # WINNER needs a heal (no resting stop) AND a trail (new high -> SL up)
    bot.pos_state["WINNER"] = {
        "state": _state(entry=100.0, qty_total=100, qty_remaining=100,
                        phase="TRAILING", sl=100.0, highest=120.0),
        "sell_stop_id": None,
    }

    bot.cycle()  # must not raise: the BAD entry failure is isolated

    sells = [c for c in double.placed if c[0] == "SELL" and c[1] == "WINNER"]
    assert len(sells) == 1, "exit-pass self-heal must still run when an entry fails"
    assert bot.pos_state["WINNER"]["sell_stop_id"]
    assert double.modified and double.modified[-1][1] == 108.0, \
        "exit-pass trailing must still run when an entry fails"
    assert alert_log["error"], "the entry failure must be alerted, not silently aborted"


# ---------------------------------------------------------------------------
# R3-3 (finding #4): failed orphan-stop cancel during gone-cleanup is retained
# ---------------------------------------------------------------------------
def test_cancel_failure_during_gone_keeps_orphan_and_alerts(double, alert_log, tmp_path):
    bot = _bot(double, tmp_path, {})
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}
    double.fail["cancel"] = "always"

    bot.cycle()  # 1st absent cycle -> not yet confirmed
    bot.cycle()  # 2nd absent cycle -> confirmed gone, cancel fails

    assert "WINNER" in bot.pos_state, \
        "must retain the position until its orphan stop is confirmed cancelled"
    assert bot._orphan_stops.get("WINNER") == "L00001", "orphan stop id must be retained"
    assert alert_log["error"], "must alert CRITICAL when the orphan cancel fails"
    assert alert_log["exit_"] == [], "must not fake an EXIT while the stop is still resting"


def test_cancel_failure_orphan_retried_next_cycle(double, alert_log, tmp_path):
    bot = _bot(double, tmp_path, {})
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}
    double.fail["cancel"] = "always"

    bot.cycle()
    bot.cycle()
    assert "WINNER" in bot.pos_state

    # cancel recovers -> the retained orphan is retried and the position forgotten
    double.fail.clear()
    bot.cycle()

    assert "L00001" in double.cancelled, "the retained orphan stop must be retried"
    assert "WINNER" not in bot.pos_state
    assert alert_log["exit_"]


# ---------------------------------------------------------------------------
# R3-4 (finding #7): None from a LIVE mutator is a FAILURE
# ---------------------------------------------------------------------------
def test_live_mutator_none_is_failure(double, tmp_path):
    # mode-aware semantics: None is success only outside LIVE (paper regression)
    assert _request_ok(None) is True
    assert _request_ok(None, "PAPER") is True
    assert _request_ok(None, "LIVE") is False

    # a LIVE place_* error dict carrying an id must never yield a stored id
    assert _extract_order_id({"s": "error", "id": "X"}) is None
    assert _extract_order_id({"s": "ok", "id": "X"}) == "X"

    class NoneModifyDouble(double.__class__):
        def modify_stop(self, order_id, new_trigger):
            self.calls.append("modify_stop")
            return None

    class NoneCancelDouble(double.__class__):
        def cancel(self, order_id):
            self.calls.append("cancel")
            return None

    class NoneSellDouble(double.__class__):
        def market_sell(self, symbol, qty, price):
            self.calls.append("market_sell")
            return None

    # modify_stop -> None is a failure (breakeven/trail path)
    d = NoneModifyDouble()
    d.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(d, tmp_path, {"WINNER": 111.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="ACTIVE", sl=94.0, highest=100.0),
                               "sell_stop_id": "L00001"}
    with pytest.raises(LiveOrderError, match="modify_stop"):
        bot.cycle()

    # cancel -> None is a failure (partial-book path)
    d = NoneCancelDouble()
    d.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(d, tmp_path, {"WINNER": 120.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="BE", sl=100.0, highest=110.0),
                               "sell_stop_id": "L00001"}
    with pytest.raises(LiveOrderError, match="cancel"):
        bot.cycle()

    # market_sell -> None is a failure (partial-book path, after a good cancel)
    d = NoneSellDouble()
    d.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(d, tmp_path, {"WINNER": 120.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="BE", sl=100.0, highest=110.0),
                               "sell_stop_id": "L00001"}
    with pytest.raises(LiveOrderError, match="market_sell"):
        bot.cycle()
