"""Unit tests for intraday_paper.bars — 5-minute bar building from ltp polls.
PURE: no network, no client, no import of fyers anything."""
import pytest

from intraday_paper.bars import BarBuilder, new_bar, update_bar, current_bar, finalize_bar

BAR = 300  # 5 minutes in seconds


def _b(sym, o, h, l, c, tslot):
    return {"sym": sym, "open": o, "high": h, "low": l, "close": c, "slot": tslot,
            "finalized": False}


# ---------- new_bar ----------
def test_new_bar_opens_at_first_price():
    bar = new_bar("SYM", 100.0, slot=0)
    assert bar["sym"] == "SYM"
    assert bar["open"] == 100.0
    assert bar["high"] == 100.0
    assert bar["low"] == 100.0
    assert bar["close"] == 100.0
    assert bar["finalized"] is False


# ---------- update_bar ----------
def test_update_bar_extends_high_low_close():
    bar = new_bar("SYM", 100.0, slot=0)
    update_bar(bar, 105.0)
    assert bar["high"] == 105.0
    assert bar["close"] == 105.0
    assert bar["low"] == 100.0
    # dips below open
    update_bar(bar, 98.0)
    assert bar["low"] == 98.0
    assert bar["high"] == 105.0
    assert bar["open"] == 100.0  # open never moves


def test_update_bar_never_reduces_high_nor_raises_low():
    bar = new_bar("SYM", 100.0, slot=0)
    update_bar(bar, 104.0)
    update_bar(bar, 99.0)
    update_bar(bar, 102.0)
    assert bar["high"] == 104.0
    assert bar["low"] == 99.0
    assert bar["close"] == 102.0


# ---------- current_bar / finalize_bar ----------
def test_finalize_bar_marks_finalized():
    bar = new_bar("SYM", 100.0, slot=0)
    update_bar(bar, 103.0)
    out = finalize_bar(bar)
    assert out["finalized"] is True
    # original is finalized in place
    assert bar["finalized"] is True


# ---------- BarBuilder rolling state machine ----------
def test_builds_rolling_5min_bar_and_finalizes_on_slot_roll():
    bb = BarBuilder(bar_seconds=BAR)
    # first poll at ts=0: opens bar slot 0
    assert bb.add("SYM", 100.0, ts=0) == []
    cb = bb.current_bar("SYM")
    assert cb["slot"] == 0 and cb["open"] == 100.0

    # same slot, higher price
    assert bb.add("SYM", 104.0, ts=30) == []
    assert bb.current_bar("SYM")["high"] == 104.0

    # next slot at ts=300: previous bar finalizes, new opens
    finalized = bb.add("SYM", 106.0, ts=300)
    assert len(finalized) == 1
    f = finalized[0]
    assert f["sym"] == "SYM" and f["finalized"] is True
    assert f["open"] == 100.0 and f["high"] == 104.0 and f["low"] == 100.0 and f["close"] == 104.0
    # new current bar opened at 106
    assert bb.current_bar("SYM")["slot"] == 1
    assert bb.current_bar("SYM")["open"] == 106.0


def test_current_bar_survives_after_roll():
    bb = BarBuilder(bar_seconds=BAR)
    bb.add("SYM", 100.0, ts=0)
    bb.add("SYM", 101.0, ts=30)
    # roll
    bb.add("SYM", 102.0, ts=300)
    current = current_bar(bb.bar_map, "SYM")
    assert current["slot"] == 1 and current["open"] == 102.0


def test_two_symbols_independently():
    bb = BarBuilder(bar_seconds=BAR)
    bb.add("A", 100.0, ts=0)
    bb.add("B", 200.0, ts=0)
    bb.add("A", 150.0, ts=300)  # only A rolls
    assert bb.current_bar("A")["open"] == 150.0
    assert bb.current_bar("B")["slot"] == 0   # B still on first bar
    assert bb.current_bar("B")["open"] == 200.0