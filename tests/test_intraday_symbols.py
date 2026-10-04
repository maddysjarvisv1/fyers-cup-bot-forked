"""Tests for intraday_paper.symbols — pure ATM/expiry helpers (no network).

The live resolver (resolve_atm_symbols) is exercised separately in the live
smoke; here we lock the symbol-format math that underpins it.
"""
from datetime import datetime

from intraday_paper.symbols import (
    atm_strike, monthly_symbol, weekly_symbol, month_code_3, month_letter,
    next_week_days, candidate_symbols,
)


def test_atm_strike_rounds_to_100():
    assert atm_strike(22446.5, 100) == 22400
    assert atm_strike(54565.1, 100) == 54600


def test_monthly_symbol_yy_mmm():
    dt = datetime(2026, 10, 30)
    assert monthly_symbol("NIFTY", dt, 22400, "CE") == "NSE:NIFTY26OCT22400CE"
    assert monthly_symbol("BANKNIFTY", dt, 54600, "PE") == "NSE:BANKNIFTY26OCT54600PE"


def test_weekly_symbol_yy_monthletter_dd():
    dt = datetime(2026, 10, 6)
    assert weekly_symbol("NIFTY", dt, 22400, "CE") == "NSE:NIFTY26O0622400CE"


def test_month_code_and_letter():
    dt = datetime(2026, 10, 6)
    assert month_code_3(dt) == "OCT"
    assert month_letter(dt) == "O"
    assert month_letter(datetime(2026, 3, 1)) == "M"


def test_next_week_days_tuesdays():
    from2026 = datetime(2026, 10, 1)   # Thursday
    tues = next_week_days(from2026, 1, 3)
    assert [d.day for d in tues] == [6, 13, 20]


def test_candidate_symbols_nifty_has_weekly_and_monthly():
    t = datetime(2026, 10, 1)
    cands = candidate_symbols("NIFTY", 22446.0, t, side="CE")
    assert "NSE:NIFTY26O0622400CE" in cands      # next Tuesday weekly
    assert any(c.startswith("NSE:NIFTY26OCT22400CE") for c in cands)  # monthly


def test_candidate_symbols_banknifty_monthly_only():
    t = datetime(2026, 10, 1)
    cands = candidate_symbols("BANKNIFTY", 54565.1, t, side="PE")
    assert any(c == "NSE:BANKNIFTY26OCT54600PE" for c in cands)
    # no weekly Mdd codes for BANKNIFTY
    assert not any(c.startswith("NSE:BANKNIFTY26O") and "OCT" not in c for c in cands)