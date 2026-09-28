"""Sqlite ledger + cup-lifecycle tracker tests. Mock-only: no fyers_apiv3,
no network, no credentials, no real orders. Ledger db written to tmp_path."""
import pytest

from ledger import Ledger
from tracking import Tracker
from broker import PaperBroker
from bot import CupBot, ScriptedFeed


WATCH = [{"symbol": "WINNER", "rim": 100.0, "trigger": 104.0, "qty": 100}]


def test_ledger_upsert_and_report(tmp_path):
    db = Ledger(str(tmp_path / "l.db"))
    db.set_status("WINNER", "order", trigger=104.0, qty=100)
    db.set_status("WINNER", "in_trade", entry_price=104.0, last_price=104.0)
    db.set_status("WINNER", "stop_loss", last_price=96.0, pnl=-800.0)
    c = db.cup("WINNER")
    assert c["status"] == "stop_loss"
    assert c["pnl"] == -800.0
    assert db.status_counts() == {"stop_loss": 1}
    r = db.report(watchlist=WATCH)
    assert r["counts"]["stop_loss"] == 1
    assert r["cups"][0]["status"] == "stop_loss"
    db.close() if hasattr(db, "close") else None


def test_tracker_maps_alerts_to_ledger(tmp_path):
    tk = Tracker(path=str(tmp_path / "t.db"))
    tk.on_alert("armed", "LALPATH", 201.0, 208.0, 50)
    c = tk.ledger.cup("LALPATH")
    assert c["status"] == "order"
    assert c["trigger"] == 208.0
    tk.on_alert("entry", "LALPATH", 208.0, 50, 190.0)
    c = tk.ledger.cup("LALPATH")
    assert c["status"] == "in_trade"
    assert c["entry_price"] == 208.0
    tk.on_alert("exit_", "LALPATH", 190.0, 50, -900.0)
    c = tk.ledger.cup("LALPATH")
    assert c["status"] == "stop_loss"
    assert c["pnl"] == -900.0
    tk.on_alert("error", "LALPATH", "order rejected -50")
    c = tk.ledger.cup("LALPATH")
    assert c["status"] == "error"
    assert "rejected" in c["error"]
    trades = tk.ledger.trades_for("LALPATH")
    assert [t["reason"] for t in trades][:2] == ["STOP_EXIT", "ENTRY"]
    assert tk.ledger.audit_for("LALPATH")


def test_bot_lifecycle_records_to_ledger(tmp_path):
    broker = PaperBroker(state_path=str(tmp_path / "paper.json"), reset=True)
    feed = ScriptedFeed({"WINNER": [99, 104, 93]})   # near-rim -> breakout -> stop-out
    tk = Tracker(path=str(tmp_path / "l.db"))
    bot = CupBot(broker, feed, WATCH, state_path=str(tmp_path / "st.json"),
                 mode="PAPER", verbose=False, tracker=tk)
    bot.cycle()   # arm buy-stop -> order
    assert tk.ledger.cup("WINNER")["status"] == "order"
    bot.cycle()   # breakout fill -> in_trade + ENTRY
    c = tk.ledger.cup("WINNER")
    assert c["status"] == "in_trade"
    assert any(t["reason"] == "ENTRY" for t in tk.ledger.trades_for("WINNER"))
    bot.cycle()   # drop below SL -> stop_loss + STOP_EXIT pnl
    c = tk.ledger.cup("WINNER")
    assert c["status"] == "stop_loss"
    assert c["pnl"] is not None and c["pnl"] < 0
    assert any(t["reason"] == "STOP_EXIT" and t["pnl"] is not None
               for t in tk.ledger.trades_for("WINNER"))


def test_tracker_error_neither_crashes_nor_touches_good_state(tmp_path):
    tk = Tracker(path=str(tmp_path / "e.db"))
    tk.on_alert("entry", "X", 100.0, 10, 90.0)
    tk.on_alert("error", "X", "reject")
    assert tk.ledger.cup("X")["status"] == "error"
    # a stray ALL/system error never flips a cup
    tk.on_alert("error", "ALL", "positions() failed")
    assert tk.ledger.cup("X")["status"] == "error"
    assert tk.ledger.audit_for(None)


def test_report_pre_trade_split_by_price(tmp_path):
    db = Ledger(str(tmp_path / "p.db"))
    db.set_status("LALPATH", "in_trade", entry_price=208.0)
    wl = [{"symbol": "READY", "trigger": 104.0, "qty": 50, "rim": 100.0},
          {"symbol": "LALPATH", "trigger": 208.0, "qty": 50, "rim": 200.0}]
    prices = {"READY": 90.0, "LALPATH": 208.0}
    r = db.report(watchlist=wl, prices=prices)
    got = {c["symbol"]: c["status"] for c in r["cups"]}
    assert got["READY"] == "trade_ready"          # 90 < 104*0.98, not within near
    assert got["LALPATH"] == "in_trade"           # ledger overlay wins
    assert r["counts"]["in_trade"] == 1
    assert r["counts"]["trade_ready"] == 1


def test_duplicate_status_is_idempotent(tmp_path):
    db = Ledger(str(tmp_path / "i.db"))
    db.set_status("S", "in_trade", entry_price=10.0)
    db.set_status("S", "in_trade", last_price=11.0)   # replay
    c = db.cup("S")
    assert c["status"] == "in_trade"
    assert c["entry_price"] == 10.0
    assert c["last_price"] == 11.0
    assert db.status_counts() == {"in_trade": 1}