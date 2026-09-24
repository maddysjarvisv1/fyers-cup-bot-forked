"""Behaviors 5-6: order/SL modification failure handling and partial-book
stop-resize failure.

=== MERGE NOTE ===
The original flash-test branch asserted these failures RAISE OUT of cycle(),
relying on run.py's bare `except Exception: print(...)` at the top of the poll
loop to catch them. That means a broken trailing stop failed with a console
print only — no Telegram alert, since the exception never reached bot._alert().
The merged bot.py fixes this: these failures are now caught INSIDE _exit_pass,
alerted (self._alert("error", ...)), and the position self-heals next cycle
instead of aborting the whole cycle (which would also have skipped exit
management / entries for every OTHER symbol this cycle). These tests assert
the new, intended behavior — not a reversion to letting exceptions escape.
"""
from bot import CupBot


def _state(entry=100.0, qty=100, phase="BE", sl=100.0, highest=110.0):
    return {"entry": entry, "qty_total": qty, "qty_remaining": qty,
            "phase": phase, "sl": sl, "highest": highest}


def _bot(double, state_path, ltp, alerts=True):
    bot = CupBot(double, lambda syms: {s: ltp for s in syms}, [],
                 state_path=str(state_path / "st.json"),
                 mode="LIVE", verbose=False, alerts=alerts)
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    return bot


# ---------------------------------------------------------------------------
# Behavior 5: modify_stop failure must NOT crash the cycle, must alert, and
# must mark the stop missing so the self-heal re-places it next cycle.
# ---------------------------------------------------------------------------
def test_exit_pass_modify_stop_failure_alerts_and_self_heals(double, alert_log, tmp_path):
    double.fail["modify_stop"] = True
    bot = _bot(double, tmp_path, ltp=111.0)
    # ACTIVE -> above +10% triggers breakeven -> modify_stop
    bot.pos_state["WINNER"] = {"state": _state(phase="ACTIVE", sl=94.0, highest=100.0),
                               "sell_stop_id": "L00001"}
    # must actually be resting, or the exit-pass stale-stop check (bot.py)
    # treats it as vanished and self-heals a new one before modify_stop runs
    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 94.0, "side": "SELL", "status": "pending"})

    bot.cycle()  # must not raise

    assert any("SL MODIFY FAILED" in a[2] for a in alert_log["error"]), \
        "a modify_stop failure must fire a CRITICAL alert, not fail silently"
    assert bot.pos_state["WINNER"]["sell_stop_id"] is None, \
        "stop must be marked missing so _exit_pass re-places it next cycle"

    double.fail.pop("modify_stop", None)
    bot.cycle()  # self-heal: re-places the missing stop
    assert bot.pos_state["WINNER"]["sell_stop_id"], "self-heal must re-arm the stop"
    heal_sells = [c for c in double.placed if c[0] == "SELL"]
    assert len(heal_sells) == 1


# ---------------------------------------------------------------------------
# Behavior 5: market_sell failure (partial-book) must NOT crash the cycle, and
# must NOT restore the old (larger) stop size — the stop is already correctly
# sized to `keep`; LIVE's qty-drift resync is what reconciles the rest.
# ---------------------------------------------------------------------------
def test_exit_pass_market_sell_failure_keeps_resized_stop_and_alerts(double, alert_log, tmp_path):
    double.fail["market_sell"] = True
    bot = _bot(double, tmp_path, ltp=120.0)
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}
    # must actually be resting, or the exit-pass stale-stop check (bot.py)
    # self-heals a new stop before the book-partial/modify_stop_qty path runs
    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 100.0, "side": "SELL", "status": "pending"})

    bot.cycle()  # must not raise

    assert any("PARTIAL BOOK SELL FAILED" in a[2] for a in alert_log["error"])
    assert all(c[0] != "SELL" or c[2] != 100 for c in double.placed), \
        "must not have re-placed a second full-size stop"
    # the resting stop was resized to `keep` (50) even though the sell failed
    assert bot.pos_state["WINNER"]["state"]["qty_remaining"] == 50


# ---------------------------------------------------------------------------
# Behavior 6: partial-book stop-resize failure — a cancel that can't be
# confirmed during the atomic-modify fallback must NOT place a second
# sell-stop (that would rest two stops on the same shares at once).
# ---------------------------------------------------------------------------
def test_partial_stop_resize_failure_does_not_double_place(double, alert_log, tmp_path):
    double.fail["modify_stop_qty"] = True   # force the cancel-then-replace fallback
    double.fail["cancel"] = True            # ...and make that cancel unconfirmed
    bot = _bot(double, tmp_path, ltp=120.0)
    bot.pos_state["WINNER"] = {"state": _state(), "sell_stop_id": "L00001"}
    # must actually be resting, or the exit-pass stale-stop check (bot.py)
    # self-heals a new stop before this path is ever exercised
    double.pending.append({"id": "L00001", "symbol": "WINNER", "qty": 100,
                           "trigger": 100.0, "side": "SELL", "status": "pending"})

    bot.cycle()  # must not raise

    assert all(c[0] != "SELL" for c in double.placed), \
        "must not place a new (trail) sell-stop before the old one is confirmed cancelled"
    assert any("PARTIAL BOOK STOP RESIZE FAILED" in a[2] for a in alert_log["error"])
    # nothing was sold either — selling before the stop is safely resized would
    # leave the remainder briefly over-protected by a now-oversized stale stop
    assert double.sold == []
