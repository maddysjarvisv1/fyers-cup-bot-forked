"""Tests for intraday_paper.tracker — state machine: signal -> entry -> exit
modes -> JSONL ledger. Pure (uses tmp_path writes, no network)."""
import json

from intraday_paper.tracker import PaperTracker, write_ledger_line


def _fbar(sym, o, h, l, c, slot):
    return {"sym": sym, "open": o, "high": h, "low": l, "close": c,
            "slot": slot, "finalized": True}


def test_signal_opens_trade_and_logs_signal(tmp_path):
    led = str(tmp_path / "ledger.jsonl")
    tr = PaperTracker(ledger_path=led, sl_points=2.0, cost_in_points=2.0)
    sig = _fbar("NIFTY", 100.0, 106.0, 100.0, 105.0, 0)   # green open==low
    evs = tr.on_finalized_bar(sig, now_ts=1000.0)
    assert len(evs) == 1 and evs[0]["event"] == "signal"
    assert tr.open_count() == 1
    assert evs[0]["entry"] == 105.0
    assert evs[0]["sl"] == 100.0 - 2.0    # signal_low - sl_points


def test_signal_persisted_to_ledger(tmp_path):
    led = str(tmp_path / "ledger.jsonl")
    tr = PaperTracker(ledger_path=led, sl_points=2.0, cost_in_points=2.0)
    tr.on_finalized_bar(_fbar("NIFTY", 100.0, 106.0, 100.0, 105.0, 0), now_ts=1.0)
    lines = open(led).read().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "signal" and rec["sym"] == "NIFTY"
    assert rec["entry"] == 105.0 and rec["sl"] == 98.0


def test_trade_exit_trail_and_logged(tmp_path):
    led = str(tmp_path / "ledger.jsonl")
    tr = PaperTracker(ledger_path=led, sl_points=2.0, cost_in_points=2.0)
    tr.on_finalized_bar(_fbar("NIFTY", 100.0, 110.0, 100.0, 108.0, 0), now_ts=1.0)
    # priors: none. Later bars raise then drop to hit the trail SL.
    evs = tr.on_finalized_bar(_fbar("NIFTY", 108, 111, 106, 110, 1), now_ts=2.0)
    # trail ratchets to 106; no exit yet (low 106 >= trail 98->106 not <=)
    evs = tr.on_finalized_bar(_fbar("NIFTY", 110, 112, 108, 111, 2), now_ts=3.0)
    evs = tr.on_finalized_bar(_fbar("NIFTY", 111, 111, 105, 106, 3), now_ts=4.0)
    exits = [e for e in evs if e and e["event"] == "exit"]
    assert len(exits) == 1
    assert exits[0]["mode"] == "trail"
    assert exits[0]["exit_price"] == 108.0
    assert tr.open_count() == 0
    # ledger: signal + exit persisted
    lines = open(led).read().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["event"] == "exit"
    assert json.loads(lines[1])["mode"] == "trail"


def test_non_green_bar_no_trade(tmp_path):
    tr = PaperTracker(ledger_path=str(tmp_path / "ledger.jsonl"),
                      sl_points=2.0, cost_in_points=2.0)
    evs = tr.on_finalized_bar(_fbar("NIFTY", 101.0, 106.0, 100.0, 105.0, 0))  # open != low
    assert evs == []
    assert tr.open_count() == 0


def test_write_ledger_line_appends_jsonl(tmp_path):
    led = str(tmp_path / "ledger.jsonl")
    write_ledger_line(led, {"a": 1})
    write_ledger_line(led, {"b": 2})
    lines = open(led).read().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["b"] == 2