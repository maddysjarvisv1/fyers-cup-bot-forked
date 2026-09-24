"""Round-2 live-risk fixes: CRITICAL 1-4 and MAJOR 5-9 from the reviewer report.

Mocks only (LiveBrokerDouble from conftest).

=== MERGE NOTE ===
Several tests here were written against the UNFIXED broker/fyers_client layer,
where place_*/modify_* returned a Fyers-style dict ({"s": "ok"/"error", ...})
or the whole raw response, and bot.py had to sniff it with _request_ok()/
_extract_order_id(). The merged fyers_client.py validates and raises
FyersError on a rejected place_*/modify_* instead, so those two helpers (and
the tests that unit-tested them directly) no longer apply — bot.py's
LiveOrderError wrapping + exception handling is the new equivalent, and is
exercised through CupBot's behavior below rather than as bare functions.

The partial-book ordering tests (CRITICAL 3) also changed: the original branch
did cancel -> market_sell -> place (three broker calls, a naked window for the
WHOLE position between cancel and place). The merged bot.py instead calls
broker.modify_stop_qty(), which tries a single atomic modify first (no window
at all) and only falls back to cancel-then-replace if Fyers rejects the
combined qty+trigger modify — unverified either way; test_live_order.py is
what proves it against real Fyers. Both orderings are tested below.
"""
import json

import pytest

from bot import CupBot, LiveOrderError
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
# MAJOR 7 (adapted) — a LIVE place_* that yields no usable id is a failure,
# even if the broker call didn't raise (defense in depth: the real
# fyers_client.py never does this — it raises instead — but bot.py must not
# silently accept a falsy id as success either).
# ---------------------------------------------------------------------------
def test_live_place_sell_stop_none_is_failure(double, tmp_path):
    class NoneSellDouble(double.__class__):
        def place_sell_stop(self, symbol, qty, trigger, order_tag="cupbot"):
            self.calls.append("place_sell_stop")
            return None

    double = NoneSellDouble()
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0})
    bot.cycle()   # _reconcile_live isolates the failure per-symbol — must NOT raise
    assert "WINNER" not in bot.pos_state, \
        "a None (falsy) order id must be treated as a failed placement, not adopted"


# ---------------------------------------------------------------------------
# CRITICAL 1 — persist state even when a live op fails mid-cycle for one symbol
# (adapted: reconcile isolates per-symbol failures rather than raising, so the
# assertion is "A succeeded and persisted, B failed and was skipped, cycle did
# not raise" rather than "cycle raised but A was persisted anyway").
# ---------------------------------------------------------------------------
def test_two_symbol_reconcile_second_failure_persists_first(double, tmp_path):
    wl2 = [{"symbol": "A", "rim": 100, "trigger": 104, "qty": 100},
           {"symbol": "B", "rim": 100, "trigger": 104, "qty": 100}]
    double.positions_map["A"] = {"qty": 100, "avg": 100.0}
    double.positions_map["B"] = {"qty": 100, "avg": 100.0}
    double.fail_symbols["place_sell_stop"] = {"B"}
    bot = _bot(double, tmp_path, {"A": 100.0, "B": 100.0}, watchlist=wl2)

    bot.cycle()   # must not raise — B's failure must not block A

    assert "A" in bot.pos_state, "A's adoption must succeed despite B failing"
    assert "B" not in bot.pos_state, "B's failed adoption must not be recorded as if it worked"
    saved = json.load(open(tmp_path / "st.json"))
    assert "A" in saved, "A's state must be persisted even though B failed"

    # restart: A must NOT receive a second SELL-stop; B gets retried
    double.fail_symbols.clear()
    double.placed.clear()
    bot2 = CupBot(double, lambda syms: {s: {"A": 100.0, "B": 100.0}[s] for s in syms},
                  wl2, state_path=str(tmp_path / "st.json"),
                  mode="LIVE", verbose=False)
    bot2.cycle()
    assert all(c[1] != "A" for c in double.placed), "must not duplicate A's SELL after restart"
    assert any(c[1] == "B" for c in double.placed), "B must be retried once the failure clears"
    assert "B" in bot2.pos_state


# ---------------------------------------------------------------------------
# CRITICAL 3 — partial-book: the atomic modify_stop_qty path (no cancel/place
# at all — this is the no-naked-window case).
# ---------------------------------------------------------------------------
def test_partial_book_uses_atomic_resize_when_available(double, tmp_path):
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 120.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="BE", sl=100.0, highest=110.0),
                               "sell_stop_id": "L00001"}
    # the stop must actually be resting, or the exit-pass stale-stop check
    # (bot.py) treats it as vanished and self-heals a new one before the
    # book-partial logic below ever runs.
    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 100.0, "side": "SELL", "status": "pending"})

    bot.cycle()

    assert double.calls == ["modify_stop_qty", "market_sell"], \
        "the atomic path must resize the stop in one call, then sell — no cancel/place needed"
    # highest=110 -> max(110, ltp=120)=120; trail SL = 120 * (1 - TRAIL_PCT 0.10) = 108.0
    assert double.modified == [("L00001", 108.0)]
    assert double.sold == [("WINNER", 50, 120.0)]
    assert bot.pos_state["WINNER"]["sell_stop_id"] == "L00001", \
        "the atomic path keeps the same order id (Fyers modify doesn't change it)"


# ---------------------------------------------------------------------------
# CRITICAL 3 (fallback) — when Fyers rejects the combined qty+trigger modify,
# the fallback must still cancel BEFORE placing the replacement, and only
# after the cancel is confirmed.
# ---------------------------------------------------------------------------
def test_partial_book_fallback_cancels_before_replacing(double, tmp_path):
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.fail["modify_stop_qty"] = True   # force the cancel-then-replace fallback
    double._next_id = 100                   # avoid an id collision with the hand-set "L00001"
    bot = _bot(double, tmp_path, {"WINNER": 120.0})
    bot.pos_state["WINNER"] = {"state": _state(phase="BE", sl=100.0, highest=110.0),
                               "sell_stop_id": "L00001"}
    # must actually be resting, or the stale-stop self-heal fires first (see
    # test_partial_book_uses_atomic_resize_when_available for why)
    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 100.0, "side": "SELL", "status": "pending"})

    bot.cycle()

    assert double.calls == ["modify_stop_qty", "cancel", "place_sell_stop", "market_sell"], \
        "fallback must cancel the old stop BEFORE placing the new one, and sell last"
    assert "L00001" in double.cancelled
    new_id = bot.pos_state["WINNER"]["sell_stop_id"]
    assert new_id != "L00001", "fallback path must yield the freshly placed stop's id"
    assert double.sold == [("WINNER", 50, 120.0)]


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
# MAJOR 6 — partial fills / qty drift -> resize the stop to the full qty
# (adapted: the resize goes through modify_stop_qty, not a fresh SELL place,
# unless the atomic path is rejected)
# ---------------------------------------------------------------------------
def test_partial_fill_qty_drift_resizes_stop_to_full_qty(double, tmp_path):
    double.positions_map["WINNER"] = {"qty": 40, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0}, watchlist=WL)

    bot.cycle()  # adopt at 40 — this IS a fresh placement (no prior stop to resize)
    assert bot.pos_state["WINNER"]["state"]["qty_total"] == 40
    assert double.placed[-1] == ("SELL", "WINNER", 40, 94.0)
    stop_id = bot.pos_state["WINNER"]["sell_stop_id"]

    # later fills bring broker qty to 100
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.placed.clear()
    double.modified.clear()

    bot.cycle()

    assert double.modified == [(stop_id, 94.0)], "existing stop must be resized, not replaced"
    assert all(c[0] != "SELL" for c in double.placed), "must not place a second stop for the same fill"
    assert bot.pos_state["WINNER"]["sell_stop_id"] == stop_id
    assert bot.pos_state["WINNER"]["state"]["qty_total"] == 100
    assert bot.pos_state["WINNER"]["state"]["qty_remaining"] == 100


def test_partial_fill_qty_drift_fallback_path_still_ends_at_correct_qty(double, tmp_path):
    """Same drift, but the atomic resize is rejected -> fallback cancel+place
    must still land on the correct final qty (this is what protects against
    the position ending up under-protected after the fallback)."""
    double.positions_map["WINNER"] = {"qty": 40, "avg": 100.0}
    bot = _bot(double, tmp_path, {"WINNER": 100.0}, watchlist=WL)
    bot.cycle()

    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    double.fail["modify_stop_qty"] = True
    double.placed.clear()

    bot.cycle()

    resting = [o for o in double.pending if o["symbol"] == "WINNER" and o["side"] == "SELL"]
    assert len(resting) == 1, "exactly one resting stop after the fallback — never two"
    assert resting[0]["qty"] == 100


# ---------------------------------------------------------------------------
# MAJOR 8 — _in_flight lifecycle (check buy result + clear on cancel/expiry)
# (adapted: entry is isolated — a failed arm alerts and returns, it does not
# raise out of _entry_pass_isolated; see bot._entry_one)
# ---------------------------------------------------------------------------
def test_buy_stop_failure_does_not_block_or_alert_armed(double, alert_log, tmp_path):
    double.fail["place_buy_stop"] = True
    bot = _bot(double, tmp_path, {"WINNER": 99.0}, watchlist=WL)

    bot._entry_pass_isolated({"WINNER": 99.0})   # must not raise

    assert "WINNER" not in bot._in_flight, "a failed arm must not block the symbol"
    assert alert_log["armed"] == [], "a failed arm must not fire a false ARMED alert"
    assert alert_log["error"], "a failed arm must still surface as an error alert"


def test_pending_buy_stop_prevents_duplicate_arm(double, tmp_path):
    bot = _bot(double, tmp_path, {"WINNER": 99.0}, watchlist=WL)
    bot._entry_pass_isolated({"WINNER": 99.0})  # arm
    bot._entry_pass_isolated({"WINNER": 99.0})  # pending buy present -> skip
    buys = [c for c in double.placed if c[0] == "BUY"]
    assert len(buys) == 1


def test_in_flight_rearms_after_order_cancelled(double, tmp_path):
    bot = _bot(double, tmp_path, {"WINNER": 99.0}, watchlist=WL)
    bot._entry_pass_isolated({"WINNER": 99.0})  # arm -> in-flight
    assert any(c[0] == "BUY" for c in double.placed)

    # buy-stop cancelled/rejected/expired at broker; no position, no order
    double.pending.clear()
    double.positions_map.clear()
    assert "WINNER" in bot._in_flight

    bot._entry_pass_isolated({"WINNER": 99.0})  # clear stale in-flight -> re-arm

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
