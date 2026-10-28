"""Momentum rows through the SAME CupBot path: arm-near-rim buy-stop, 6% row stop
on fill, time-stop exit (cancel-confirmed, then market), cup rows unchanged.
Uses conftest.LiveBrokerDouble; no network."""
import datetime as dt
import importlib
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[:1] != [ROOT]:
    if ROOT in sys.path:
        sys.path.remove(ROOT)
    sys.path.insert(0, ROOT)

import cup_sl_engine as sl
import run
if not os.path.abspath(getattr(run, "__file__", "")).startswith(ROOT):
    run = importlib.reload(run)
from bot import CupBot

MOMO = {"symbol": "SONACOMS", "rim": 839.0, "trigger": 872.55, "qty": 16, "kind": "momentum",
        "stop": 820.20, "time_stop_days": 5, "valid_until": "2099-01-01"}


def _bot(double, tmp_path, wl, px):
    return CupBot(double, lambda syms: dict(px), wl, state_path=str(tmp_path / "bot_state.json"),
                  mode="LIVE", verbose=False)


def _entry_day_n_sessions_ago(today, n):
    d = dt.date.fromisoformat(today)
    while sl.sessions_since(d.isoformat(), today) < n:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def test_momentum_row_arms_buy_stop_near_rim(double, tmp_path):
    _bot(double, tmp_path, [MOMO], {"SONACOMS": 835.0}).cycle()       # 835 >= 839*0.99, < 872.55
    assert double.placed == [("BUY", "SONACOMS", 16, 872.55)]


def test_expired_momentum_row_never_arms(double, tmp_path):
    _bot(double, tmp_path, [dict(MOMO, valid_until="2000-01-01")], {"SONACOMS": 835.0}).cycle()
    assert double.placed == []


def test_momentum_fill_uses_row_stop(double, tmp_path):
    double.positions_map["SONACOMS"] = {"qty": 16, "avg": 872.55}
    bot = _bot(double, tmp_path, [MOMO], {"SONACOMS": 875.0})
    bot.cycle()
    assert ("SELL", "SONACOMS", 16, 820.2) in double.placed
    st = bot.pos_state["SONACOMS"]["state"]
    assert (st["sl"], st["time_stop_days"], st["entry_day"]) == (820.2, 5, bot.day)


def test_momentum_stop_above_fill_falls_back_to_engine_default(double, tmp_path):
    double.positions_map["SONACOMS"] = {"qty": 16, "avg": 810.0}
    _bot(double, tmp_path, [MOMO], {"SONACOMS": 812.0}).cycle()
    assert ("SELL", "SONACOMS", 16, 761.4) in double.placed             # 810*0.94


def test_cup_row_keeps_engine_default_stop(double, tmp_path):
    cup = {"symbol": "WINNER", "rim": 100, "trigger": 104, "qty": 100}
    double.positions_map["WINNER"] = {"qty": 100, "avg": 104.0}
    bot = _bot(double, tmp_path, [cup], {"WINNER": 104.5})
    bot.cycle()
    assert ("SELL", "WINNER", 100, 97.75) in double.placed
    assert bot.pos_state["WINNER"]["state"]["time_stop_days"] == 0


def test_time_stop_cancels_stop_then_sells(double, tmp_path):
    double.positions_map["SONACOMS"] = {"qty": 16, "avg": 872.55}
    bot = _bot(double, tmp_path, [MOMO], {"SONACOMS": 875.0})
    bot.cycle()
    sid = bot.pos_state["SONACOMS"]["sell_stop_id"]
    bot.pos_state["SONACOMS"]["state"]["entry_day"] = _entry_day_n_sessions_ago(bot.day, 5)
    bot.cycle()
    assert sid in double.cancelled
    assert ("SONACOMS", 16, 875.0) in double.sold
    assert double.calls.index("cancel") < double.calls.index("market_sell")
    assert "SONACOMS" not in bot.pos_state


def test_time_stop_not_before_n_sessions(double, tmp_path):
    double.positions_map["SONACOMS"] = {"qty": 16, "avg": 872.55}
    bot = _bot(double, tmp_path, [MOMO], {"SONACOMS": 875.0})
    bot.cycle()
    bot.pos_state["SONACOMS"]["state"]["entry_day"] = _entry_day_n_sessions_ago(bot.day, 4)
    bot.cycle()
    assert double.sold == [] and double.cancelled == [] and "SONACOMS" in bot.pos_state


def test_time_stop_refuses_to_sell_when_cancel_unconfirmed(double, tmp_path):
    double.positions_map["SONACOMS"] = {"qty": 16, "avg": 872.55}
    bot = _bot(double, tmp_path, [MOMO], {"SONACOMS": 875.0})
    bot.cycle()
    sid = bot.pos_state["SONACOMS"]["sell_stop_id"]
    bot.pos_state["SONACOMS"]["state"]["entry_day"] = _entry_day_n_sessions_ago(bot.day, 5)
    double.fail["cancel"] = "always"
    bot.cycle()
    assert double.sold == []
    assert bot._orphan_stops.get("SONACOMS") == sid and "SONACOMS" in bot.pos_state


def test_engine_time_exit_counts_sessions():
    st = sl.PosState(entry=100, qty_total=10, qty_remaining=10, sl=94.0,
                     time_stop_days=5, entry_day="2026-10-21")
    st, acts = sl.step(st, 101, today="2026-10-27")                 # Thu Fri Mon Tue = 4
    assert acts == [] and st.phase == "ACTIVE"
    st, acts = sl.step(st, 101, today="2026-10-28")                 # 5th session
    assert acts == [("time_exit", 10, 101)] and st.phase == "CLOSED"


def test_engine_time_stop_ignored_after_breakeven():
    st = sl.PosState(entry=100, qty_total=10, qty_remaining=10, sl=94.0,
                     time_stop_days=5, entry_day="2026-10-21")
    st, _ = sl.step(st, 111, today="2026-10-22")
    assert st.phase == "BE"
    st, acts = sl.step(st, 105, today="2026-11-30")
    assert all(a[0] != "time_exit" for a in acts) and st.phase == "BE"


def test_load_watchlist_momentum_rows(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("symbol,rim,trigger,qty,kind,stop,time_stop_days,valid_until\n"
                 "SONACOMS,839.0,872.55,16,momentum,820.20,5,2026-10-29\n"
                 "OLD,100,104.0,10,momentum,97.75,5,2026-10-20\n")
    assert run.load_watchlist(str(p), today="2026-10-29") == [
        {"symbol": "SONACOMS", "rim": 839.0, "trigger": 872.55, "qty": 16, "kind": "momentum",
         "stop": 820.20, "time_stop_days": 5, "valid_until": "2026-10-29"}]


def test_load_watchlist_rejects_stop_not_below_trigger(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("symbol,rim,trigger,qty,kind,stop,time_stop_days,valid_until\n"
                 "SONACOMS,839.0,872.55,16,momentum,872.55,5,2099-01-01\n")
    with pytest.raises(ValueError):
        run.load_watchlist(str(p))


def test_load_watchlist_cup_rows_unchanged(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("symbol,rim,trigger,qty\nNAZARA,419.5,436.3,28\n")
    assert run.load_watchlist(str(p)) == [{"symbol": "NAZARA", "rim": 419.5, "trigger": 436.3, "qty": 28}]
