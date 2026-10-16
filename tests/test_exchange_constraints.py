"""
tests/test_exchange_constraints.py — Exhaustive offline unit & integration tests
for scripts/exchange_constraints.py.

Every exchange rule has explicit PASS and FAIL test cases:
  1. TICK SIZE          — check_tick_size + check_outgoing_price_paths (AST + runtime)
  2. LOT SIZE           — check_lot_size + get_lot_size (equity=1 + F&O hook)
  3. FREEZE QUANTITY    — check_freeze_quantity + split_freeze_quantity
  4. PRICE BANDS        — check_price_band (2/5/10/20% bands, explicit circuits, missing ref refusal)
  5. ORDER VALUE CAP    — check_order_value_cap (within cap vs over cap / invalid)
  6. T2T / -BE SERIES   — check_t2t_series + FyersClient & watchlist_sync hard refusal
  7. VALIDITY           — check_validity + will_stop_survive_overnight (DAY overnight risk, IOC refusal)
  8. PRODUCT TYPE       — check_product_type + describe_product_type (CNC vs INTRADAY/MARGIN)
  9. MARKET/AMO HOURS   — check_market_hours (09:15-15:30 IST, pre-open, weekend, holiday, AMO)
 10. DUPLICATE GUARD    — check_duplicate_guard + CupBot invariant verification
 11. ORDER TAG & SL DIR — check_order_tag, check_sl_trigger_direction, check_symbol_format
 12. MULTI-FAULT        — validate_order(symbol, side, qty, price, product, validity)
                          returns EVERY violation on a multi-fault order, not just the first.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.exchange_constraints import (
    IST,
    RULE_ENFORCEABILITY,
    check_amo_hours,
    check_duplicate_guard,
    check_duplicate_stop,
    check_freeze_qty,
    check_freeze_quantity,
    check_lot,
    check_lot_size,
    check_market_hours,
    check_order_tag,
    check_order_value,
    check_order_value_cap,
    check_outgoing_price_paths,
    check_price_band,
    check_price_bands,
    check_product,
    check_product_type,
    check_sl_trigger_direction,
    check_symbol_format,
    check_t2t,
    check_t2t_series,
    check_tick,
    check_tick_size,
    check_validity,
    compute_price_band,
    describe_product_type,
    get_freeze_quantity,
    get_lot_size,
    split_freeze_quantity,
    validate_order,
    will_stop_survive_overnight,
)
import fyers_client
from fyers_client import FyersClient, FyersError, is_t2t_symbol
import watchlist_sync


# ===========================================================================
# 1. TICK SIZE (PASS + FAIL + Outgoing Price Path AST Audit)
# ===========================================================================
def test_rule1_tick_size_pass():
    for valid_px in (0.05, 1.0, 99.95, 419.50, 436.30, 2126.80):
        ok, reason = check_tick_size(valid_px, tick=0.05)
        assert ok is True, f"Expected PASS for {valid_px}, got {reason}"
        assert "exact multiple" in reason

    # Market order with price=0 allowed when allow_zero_market=True
    ok_mkt, _ = check_tick_size(0.0, tick=0.05, allow_zero_market=True)
    assert ok_mkt is True

    # Custom tick size (e.g. 0.25)
    ok_custom, _ = check_tick(100.75, tick=0.25)
    assert ok_custom is True


def test_rule1_tick_size_fail():
    # The exact NAZARA bug that blocked LIVE entry: 419.5 * 1.04 = 436.28
    ok, reason = check_tick_size(436.28, tick=0.05)
    assert ok is False
    assert "436.28" in reason
    assert "0.0500" in reason
    assert "436.30" in reason

    # 3-decimal unaligned price from trailing stop calculation (e.g. 436.30 * 0.94 = 410.122)
    ok2, reason2 = check_tick_size(410.122, tick=0.05)
    assert ok2 is False
    assert "410.10" in reason2

    # Non-positive, non-finite, or bad tick
    assert check_tick_size(0.0)[0] is False
    assert check_tick_size(-10.0)[0] is False
    assert check_tick_size(float("nan"))[0] is False
    assert check_tick_size(100.0, tick=0)[0] is False
    assert check_tick_size(True)[0] is False


def test_rule1_every_outgoing_price_path_uses_align_tick(tmp_path):
    # PASS case against real repository
    ok, reason = check_outgoing_price_paths()
    assert ok is True, reason
    assert "all 19 outgoing price paths" in reason

    # FAIL case: synthetic repo where a price path forgets align_tick
    broken_fc = tmp_path / "fyers_client.py"
    broken_fc.write_text(
        "class FyersClient:\n"
        "    def place_buy_stop(self, symbol, qty, trigger):\n"
        "        return {'stopPrice': float(trigger)}\n",
        encoding="utf-8",
    )
    ok_bad, reason_bad = check_outgoing_price_paths(repo_root=str(tmp_path))
    assert ok_bad is False
    assert "fyers_client.py:place_buy_stop (does not call align_tick)" in reason_bad


def test_rule1_fyers_client_modify_order_aligns_limit_and_stop(monkeypatch):
    c = FyersClient.__new__(FyersClient)
    c.fy = MagicMock()
    c.fy.modify_order.return_value = {"s": "ok", "id": "ORD1"}

    c.modify_order("ORD1", stop_price=436.28, limit_price=436.27, order_type=1)
    sent = c.fy.modify_order.call_args[0][0]
    assert sent["stopPrice"] == 436.30
    assert sent["limitPrice"] == 436.25


# ===========================================================================
# 2. LOT SIZE (PASS + FAIL + F&O Hook)
# ===========================================================================
def test_rule2_lot_size_pass():
    # NSE cash equity lot size = 1
    assert get_lot_size("NAZARA") == 1
    assert get_lot_size("NSE:SOTL-EQ") == 1
    ok, reason = check_lot_size(28, symbol="NAZARA")
    assert ok is True
    assert "lot size 1" in reason

    # F&O index option via built-in hook (NIFTY = 75, BANKNIFTY = 30)
    assert get_lot_size("NSE:NIFTY26OCT22400CE") == 75
    assert get_lot_size("NSE:NIFTY26O0622400PE") == 75
    assert get_lot_size("NSE:BANKNIFTY26OCT54600CE") == 30
    ok_fo, reason_fo = check_lot_size(150, symbol="NSE:NIFTY26OCT22400CE")
    assert ok_fo is True
    assert "2 lot(s)" in reason_fo

    # Custom lot_sizes hook for stock F&O
    ok_stock_fo, _ = check_lot(550, symbol="NSE:RELIANCE26OCTFUT", lot_sizes={"RELIANCE": 275})
    assert ok_stock_fo is True


def test_rule2_lot_size_fail():
    # Odd quantity on NIFTY option (lot size = 75)
    ok, reason = check_lot_size(50, symbol="NSE:NIFTY26OCT22400CE")
    assert ok is False
    assert "not a multiple of lot size 75" in reason

    # Explicit lot_size override mismatch
    ok2, reason2 = check_lot_size(10, symbol="BANKNIFTY", lot_size=30)
    assert ok2 is False
    assert "remainder=10" in reason2

    # Unconfigured stock F&O without lot_sizes hook refuses rather than assuming 1
    ok_unconf, reason_unconf = check_lot_size(10, symbol="NSE:RELIANCE26OCTFUT")
    assert ok_unconf is False
    assert "no configured lot size" in reason_unconf

    # Zero, negative, fractional, boolean quantities
    assert check_lot_size(0, symbol="SOTL")[0] is False
    assert check_lot_size(-5, symbol="SOTL")[0] is False
    assert check_lot_size(2.5, symbol="SOTL")[0] is False
    assert check_lot_size(True, symbol="SOTL")[0] is False


# ===========================================================================
# 3. FREEZE QUANTITY (PASS + FAIL + Order Splitter)
# ===========================================================================
def test_rule3_freeze_quantity_pass_and_split():
    # Equity order within freeze limit
    ok, reason = check_freeze_quantity(500, symbol="SOTL")
    assert ok is True
    assert "within freeze limit" in reason

    # NIFTY option within 1800 freeze limit
    ok_fo, _ = check_freeze_qty(1800, symbol="NSE:NIFTY26OCT22400CE")
    assert ok_fo is True

    # Splitting a 4050-qty NIFTY order (lot=75, freeze=1800) -> [1800, 1800, 450]
    slices = split_freeze_quantity(4050, symbol="NSE:NIFTY26OCT22400CE")
    assert slices == [1800, 1800, 450]
    assert sum(slices) == 4050
    assert all(s <= 1800 and s % 75 == 0 for s in slices)


def test_rule3_freeze_quantity_fail():
    # NIFTY option order above 1800 freeze limit
    ok, reason = check_freeze_quantity(1875, symbol="NSE:NIFTY26OCT22400CE")
    assert ok is False
    assert "exceeds NSE freeze limit 1800" in reason
    assert "[1800, 75]" in reason

    # Equity order above freeze limit (100,000 default or explicit override)
    ok_eq, reason_eq = check_freeze_quantity(150_000, symbol="IDEA")
    assert ok_eq is False
    assert "exceeds NSE freeze limit 100000" in reason_eq
    assert "[100000, 50000]" in reason_eq

    # Invalid inputs to split_freeze_quantity
    with pytest.raises(ValueError):
        split_freeze_quantity(0, symbol="SOTL")
    with pytest.raises(ValueError):
        split_freeze_quantity(100, symbol="NSE:NIFTY26OCT22400CE")  # not multiple of lot 75


# ===========================================================================
# 4. PRICE BANDS / CIRCUIT LIMITS (PASS + FAIL + Explicit Offline Disclosure)
# ===========================================================================
def test_rule4_price_band_pass():
    # 20% standard equity band around ref_price=100 -> [80.00, 120.00]
    low, high = compute_price_band(100.0, band_pct=20.0)
    assert (low, high) == (80.0, 120.0)
    ok, reason = check_price_band(104.0, ref_price=100.0, band_pct=20.0, symbol="SOTL")
    assert ok is True
    assert "inside ±20% circuit band [80.00, 120.00]" in reason

    # Explicit lower_circuit / upper_circuit from live quote feed
    ok_explicit, _ = check_price_bands(436.30, lower_circuit=400.0, upper_circuit=450.0, symbol="NAZARA")
    assert ok_explicit is True

    # T2T symbol defaults to 5% band -> 103.0 within [95.0, 105.0]
    ok_t2t_band, _ = check_price_band(103.0, ref_price=100.0, symbol="BAFNAPH")
    assert ok_t2t_band is True


def test_rule4_price_band_fail():
    # Trigger outside 5% circuit band on a 5%-band stock
    ok, reason = check_price_band(106.0, ref_price=100.0, band_pct=5.0, symbol="SMALLCAP")
    assert ok is False
    assert "outside ±5% circuit band [95.00, 105.00]" in reason

    # T2T symbol defaults to 5% band automatically -> +8% trigger rejected
    ok_t2t, reason_t2t = check_price_band(108.0, ref_price=100.0, symbol="BAFNAPH")
    assert ok_t2t is False
    assert "±5% circuit band" in reason_t2t

    # Trigger below lower circuit (e.g. -6% SL on a 5%-circuit stock!)
    ok_sl, reason_sl = check_price_band(94.0, ref_price=100.0, band_pct=5.0, symbol="LOCKED5")
    assert ok_sl is False
    assert "outside ±5% circuit band [95.00, 105.00]" in reason_sl

    # Explicit lower/upper circuit breach
    ok_exp, reason_exp = check_price_band(460.0, lower_circuit=400.0, upper_circuit=450.0, symbol="NAZARA")
    assert ok_exp is False
    assert "outside daily circuit band [400.00, 450.00]" in reason_exp

    # Non-standard band_pct (e.g. 15% is not an NSE circuit band)
    ok_bad_band, reason_bad_band = check_price_band(105.0, ref_price=100.0, band_pct=15.0)
    assert ok_bad_band is False
    assert "invalid band_pct" in reason_bad_band

    # Missing ref_price and circuit bounds: MUST explicitly report it cannot be enforced offline
    ok_no_ref, reason_no_ref = check_price_band(104.0, symbol="SOTL")
    assert ok_no_ref is False
    assert "CANNOT ENFORCE offline without ref_price" in reason_no_ref


# ===========================================================================
# 5. ORDER VALUE CAP (PASS + FAIL)
# ===========================================================================
def test_rule5_order_value_cap_pass():
    # 28 * 436.30 = Rs 12,216.40 <= Rs 50,000 cap
    ok, reason = check_order_value_cap(28, 436.30, max_order_value=50_000.0)
    assert ok is True
    assert "12,216.40" in reason

    # Single-arg notional form
    ok_direct, _ = check_order_value(49_999.95, max_order_value=50_000.0)
    assert ok_direct is True


def test_rule5_order_value_cap_fail():
    # Exceeds Rs 50,000 strategy budget cap
    ok, reason = check_order_value_cap(200, 436.30, max_order_value=50_000.0)
    assert ok is False
    assert "exceeds per-order cap Rs 50,000.00" in reason

    # Exceeds Rs 1 Crore retail exchange sanity ceiling
    ok_huge, reason_huge = check_order_value_cap(10_000, 2_500.0, max_order_value=50_000.0)
    assert ok_huge is False
    assert "exceeds retail sanity cap" in reason_huge

    # Zero or negative order value
    assert check_order_value_cap(0, 100.0)[0] is False
    assert check_order_value_cap(-10, 100.0)[0] is False


# ===========================================================================
# 6. T2T / -BE SERIES (PASS + FAIL + Hard Enforcement in FyersClient & Sync)
# ===========================================================================
def test_rule6_t2t_series_pass():
    ok, reason = check_t2t_series("SOTL")
    assert ok is True
    assert "NSE:SOTL-EQ" in reason

    ok_eq, _ = check_t2t("NSE:LALPATHLAB-EQ")
    assert ok_eq is True

    # Opt-in override when explicitly permitted
    ok_opt, reason_opt = check_t2t_series("BAFNAPH", allow_t2t=True)
    assert ok_opt is True
    assert "WARNING" in reason_opt


def test_rule6_t2t_series_fail():
    # Known SYMBOL_OVERRIDES in fyers_client.py
    for sym in ("BAFNAPH", "SANGINITA", "SWANDEF", "UNIVASTU", "VIJIFIN", "WELINV"):
        assert is_t2t_symbol(sym) is True
        ok, reason = check_t2t_series(sym)
        assert ok is False, f"{sym} must be rejected by check_t2t_series"
        assert "Trade-to-Trade (BE)" in reason
        assert "same-day stop-loss CANNOT execute" in reason

    # Explicit -BE / -BZ / -SZ suffix even if not in SYMBOL_OVERRIDES
    ok_be, reason_be = check_t2t_series("NSE:NEWSTOCK-BE")
    assert ok_be is False
    assert "NSE:NEWSTOCK-BE" in reason_be

    ok_bz, _ = check_t2t_series("NSE:SURVEIL-BZ")
    assert ok_bz is False


def test_rule6_fyers_client_hard_refuses_t2t_stops():
    c = FyersClient.__new__(FyersClient)
    c.fy = MagicMock()

    with pytest.raises(FyersError, match="T2T/BE-series delivery-only"):
        c.place_buy_stop("BAFNAPH", 10, 100.0)
    with pytest.raises(FyersError, match="T2T/BE-series delivery-only"):
        c.place_sell_stop("NSE:CUSTOM-BE", 10, 94.0)
    with pytest.raises(FyersError, match="T2T/BE-series delivery-only"):
        c.place_buy_gtt("SWANDEF", 10, 100.0)

    # Zero broker calls were made
    assert c.fy.place_order.call_count == 0
    assert c.fy.place_gtt_order.call_count == 0


def test_rule6_watchlist_sync_excludes_t2t_candidates(tmp_path):
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"
    wl_path.write_text("symbol,rim,trigger,qty\nLALPATHLAB,2045.0,2126.8,5\n", encoding="utf-8")
    cand_path.write_text(
        "symbol,ath,ath_date,buy_price,sl_price,qty,capital,current,pct_from_ath\n"
        "BAFNAPH,100.0,2021-01-01,104.0,97.75,10,1040.0,99.0,-1.0\n"
        "SOTL,200.0,2021-01-01,208.0,195.50,10,2080.0,198.0,-1.0\n",
        encoding="utf-8",
    )
    res = watchlist_sync.sync_watchlist(
        watchlist_path=str(wl_path),
        candidates_path=str(cand_path),
        capital_budget=50_000,
    )
    added_syms = [r["symbol"] for r in res["added"]]
    skipped_by_sym = {s["symbol"]: s["reason"] for s in res["skipped"]}
    assert "SOTL" in added_syms
    assert "BAFNAPH" not in added_syms
    assert "BAFNAPH" in skipped_by_sym
    assert "T2T/BE-series" in skipped_by_sym["BAFNAPH"]


# ===========================================================================
# 7. VALIDITY (PASS + FAIL + Overnight Survival Exposure)
# ===========================================================================
def test_rule7_validity_pass():
    assert will_stop_survive_overnight("DAY") is False
    assert will_stop_survive_overnight("IOC") is False
    assert will_stop_survive_overnight("GTT") is True

    # DAY stop when bot has next-day self-heal/re-arm enabled
    ok, reason = check_validity("DAY", is_stop=True, overnight_position=True, has_next_day_rearm=True)
    assert ok is True
    assert "survives_overnight=False" in reason
    assert "expires at 15:30 IST close" in reason

    # GTT survives overnight natively
    ok_gtt, reason_gtt = check_validity("GTT", is_stop=True, overnight_position=True, has_next_day_rearm=False)
    assert ok_gtt is True
    assert "survives_overnight=True" in reason_gtt


def test_rule7_validity_fail():
    # DAY stop on an overnight position WITHOUT next-day re-arm leaves position unprotected
    ok, reason = check_validity("DAY", is_stop=True, overnight_position=True, has_next_day_rearm=False)
    assert ok is False
    assert "survives_overnight=False" in reason
    assert "refusing to leave overnight position unprotected" in reason

    # IOC validity on a resting stop order cancels immediately
    ok_ioc, reason_ioc = check_validity("IOC", is_stop=True)
    assert ok_ioc is False
    assert "IOC (Immediate-or-Cancel) is invalid for a resting stop order" in reason_ioc

    # Unknown validity (e.g. GTC is not supported on NSE/Fyers standard orders)
    ok_gtc, reason_gtc = check_validity("GTC")
    assert ok_gtc is False
    assert "invalid" in reason_gtc


# ===========================================================================
# 8. PRODUCT TYPE (PASS + FAIL + Explicit Consequences)
# ===========================================================================
def test_rule8_product_type_pass():
    ok, reason = check_product_type("CNC", symbol="NAZARA", hold_overnight=True)
    assert ok is True
    assert "can be held overnight" in reason
    assert "no intraday broker auto-square-off" in reason

    # INTRADAY allowed when strategy does NOT hold overnight (and not T2T)
    ok_intra, reason_intra = check_product("INTRADAY", symbol="NAZARA", hold_overnight=False)
    assert ok_intra is True
    assert "auto-squares off" in reason_intra

    # MARGIN allowed on F&O symbol
    ok_margin, _ = check_product_type("MARGIN", symbol="NSE:NIFTY26OCT22400CE", hold_overnight=True)
    assert ok_margin is True


def test_rule8_product_type_fail():
    # INTRADAY rejected when holding overnight (auto square-off at 15:15-15:20 IST)
    ok, reason = check_product_type("INTRADAY", symbol="NAZARA", hold_overnight=True)
    assert ok is False
    assert "auto-squares off" in reason
    assert "CANNOT be held overnight" in reason

    # MARGIN rejected on cash equity (-EQ)
    ok_margin_eq, reason_margin_eq = check_product_type("MARGIN", symbol="NAZARA")
    assert ok_margin_eq is False
    assert "rejected on NSE cash equity" in reason_margin_eq

    # CNC rejected on F&O derivative
    ok_cnc_fo, reason_cnc_fo = check_product_type("CNC", symbol="NSE:NIFTY26OCT22400CE")
    assert ok_cnc_fo is False
    assert "rejected on F&O symbol" in reason_cnc_fo

    # INTRADAY rejected on T2T (-BE) stock even if hold_overnight=False
    ok_t2t_intra, reason_t2t_intra = check_product_type("INTRADAY", symbol="BAFNAPH", hold_overnight=False)
    assert ok_t2t_intra is False
    assert "requires CNC delivery" in reason_t2t_intra

    # Unknown product type
    assert check_product_type("CO")[0] is False
    assert "Unknown" in describe_product_type("BOGUS")


# ===========================================================================
# 9. MARKET / AMO HOURS (PASS + FAIL)
# ===========================================================================
def test_rule9_market_hours_pass():
    # Friday 2026-10-16 at 10:30 IST (regular market hours)
    regular_ts = datetime(2026, 10, 16, 10, 30, 0, tzinfo=IST)
    ok, reason = check_market_hours(regular_ts, offline_order=False)
    assert ok is True
    assert "within regular NSE hours" in reason

    # Boundary timestamps: 09:15:00 and 15:30:00 IST
    assert check_market_hours(datetime(2026, 10, 16, 9, 15, 0, tzinfo=IST))[0] is True
    assert check_market_hours(datetime(2026, 10, 16, 15, 30, 0, tzinfo=IST))[0] is True

    # After-hours with explicit AMO (offline_order=True, allow_amo=True)
    after_close = datetime(2026, 10, 16, 18, 0, 0, tzinfo=IST)
    ok_amo, reason_amo = check_amo_hours(after_close, offline_order=True, allow_amo=True)
    assert ok_amo is True
    assert "AMO (offlineOrder=True) explicitly enabled" in reason_amo


def test_rule9_market_hours_fail():
    # Regular order after 15:30 IST close
    after_close = datetime(2026, 10, 16, 15, 31, 0, tzinfo=IST)
    ok, reason = check_market_hours(after_close, offline_order=False)
    assert ok is False
    assert "outside 09:15-15:30 IST" in reason
    assert "offlineOrder=False" in reason

    # Regular order on Saturday (weekend)
    saturday = datetime(2026, 10, 17, 11, 0, 0, tzinfo=IST)
    ok_wknd, reason_wknd = check_market_hours(saturday, offline_order=False)
    assert ok_wknd is False
    assert "weekend (Saturday)" in reason_wknd

    # Regular order on NSE holiday
    holiday_ts = datetime(2026, 10, 2, 11, 0, 0, tzinfo=IST)
    ok_hol, reason_hol = check_market_hours(holiday_ts, holidays={"2026-10-02"})
    assert ok_hol is False
    assert "NSE holiday (2026-10-02)" in reason_hol

    # Pre-open session (09:00-09:15 IST): SL-M stop orders rejected even with AMO
    pre_open = datetime(2026, 10, 16, 9, 5, 0, tzinfo=IST)
    ok_pre, reason_pre = check_market_hours(pre_open, offline_order=True, allow_amo=True, is_stop=True)
    assert ok_pre is False
    assert "pre-open session" in reason_pre

    # Sending AMO (offlineOrder=True) DURING live market hours is rejected by Fyers
    live_ts = datetime(2026, 10, 16, 11, 0, 0, tzinfo=IST)
    ok_amo_live, reason_amo_live = check_market_hours(live_ts, offline_order=True, allow_amo=True)
    assert ok_amo_live is False
    assert "cannot be sent during live NSE market hours" in reason_amo_live


# ===========================================================================
# 10. DUPLICATE GUARD (PASS + FAIL)
# ===========================================================================
def test_rule10_duplicate_guard_pass():
    open_orders = [
        {"id": "O1", "symbol": "SOTL", "side": "BUY", "qty": 10, "status": "pending"},
    ]
    # Different symbol -> PASS
    ok, reason = check_duplicate_guard("NAZARA", "BUY", 28, open_orders=open_orders)
    assert ok is True
    assert "no conflicting BUY stop" in reason

    # Same symbol, opposite side (e.g. checking SELL when only BUY is in open_orders, or vice versa)
    ok_opp, _ = check_duplicate_stop("SOTL", "SELL", 10, open_orders=open_orders, position_qty=10)
    assert ok_opp is True

    # Cancelled/filled orders in raw orderbook are ignored
    closed_orders = [
        {"id": "O2", "symbol": "NSE:NAZARA-EQ", "side": -1, "qty": 28, "status": 1},  # 1 = Cancelled
        {"id": "O3", "symbol": "NSE:NAZARA-EQ", "side": -1, "qty": 28, "status": 2},  # 2 = Filled
    ]
    ok_closed, _ = check_duplicate_guard("NAZARA", "SELL", 28, open_orders=closed_orders, position_qty=28)
    assert ok_closed is True


def test_rule10_duplicate_guard_fail():
    # Existing resting SELL stop for NAZARA -> second SELL stop must be refused (oversell/short risk)
    open_orders = [
        {"id": "STOP01", "symbol": "NSE:NAZARA-EQ", "side": "SELL", "qty": 28, "status": "pending"},
    ]
    ok, reason = check_duplicate_guard("NAZARA", "SELL", 28, open_orders=open_orders, position_qty=28)
    assert ok is False
    assert "STOP01" in reason
    assert "oversell/accidental short risk" in reason

    # Existing resting BUY stop for NAZARA -> second BUY stop refused (double-entry risk)
    open_buys = [
        {"id": "BUY01", "symbol": "NAZARA", "side": 1, "qty": 28, "status": 6},  # Fyers status 6 = Pending
    ]
    ok_buy, reason_buy = check_duplicate_guard("NSE:NAZARA-EQ", "BUY", 28, open_orders=open_buys)
    assert ok_buy is False
    assert "BUY01" in reason_buy
    assert "double-entry" in reason_buy

    # SELL qty exceeds held position_qty even with no resting orders -> oversell/short risk
    ok_over, reason_over = check_duplicate_guard("NAZARA", "SELL", 30, open_orders=[], position_qty=28)
    assert ok_over is False
    assert "exceeds held position_qty 28" in reason_over


# ===========================================================================
# 11. BONUS CLASS MEMBERS: ORDER TAG, SL TRIGGER DIRECTION, SYMBOL FORMAT
# ===========================================================================
def test_rule11_order_tag_pass_and_fail():
    assert check_order_tag("cupbot")[0] is True
    assert check_order_tag("cup2026")[0] is True
    assert check_order_tag("")[0] is True

    # Too long (>20 chars) or non-alphanumeric (rejected by Fyers v3)
    assert check_order_tag("a" * 21)[0] is False
    assert check_order_tag("cup-bot_1")[0] is False
    assert check_order_tag("cup bot")[0] is False


def test_rule12_sl_trigger_direction_pass_and_fail():
    # PASS: BUY stop above LTP; SELL stop below LTP
    assert check_sl_trigger_direction("BUY", 436.30, ltp=419.50)[0] is True
    assert check_sl_trigger_direction("SELL", 410.10, ltp=436.30)[0] is True

    # FAIL: BUY stop at/below LTP; SELL stop at/above LTP
    ok_b, reason_b = check_sl_trigger_direction("BUY", 415.00, ltp=419.50)
    assert ok_b is False
    assert "behind the market" in reason_b

    ok_s, reason_s = check_sl_trigger_direction("SELL", 440.00, ltp=436.30)
    assert ok_s is False
    assert "behind the market" in reason_s

    # Missing LTP -> explicitly states cannot enforce offline
    ok_noltp, reason_noltp = check_sl_trigger_direction("BUY", 436.30, ltp=None)
    assert ok_noltp is False
    assert "CANNOT ENFORCE offline without live ltp" in reason_noltp


def test_rule13_symbol_format_pass_and_fail():
    assert check_symbol_format("NAZARA")[0] is True
    assert check_symbol_format("NSE:M&M-EQ")[0] is True
    assert check_symbol_format("")[0] is False
    assert check_symbol_format("NYSE:AAPL")[0] is False
    assert check_symbol_format("BAD SYMBOL!")[0] is False


# ===========================================================================
# 12. UNIFIED ENTRY POINT: validate_order(...) — CLEAN + MULTI-FAULT
# ===========================================================================
def test_validate_order_clean_pass():
    # Standard 6-arg call for a clean cup breakout order
    violations = validate_order("NAZARA", "BUY", 28, 436.30, "CNC", "DAY")
    assert violations == []

    # Full context call during market hours with ref_price, ltp, and open_orders
    market_ts = datetime(2026, 10, 16, 10, 15, 0, tzinfo=IST)
    violations_full = validate_order(
        "NAZARA",
        "BUY",
        28,
        436.30,
        "CNC",
        "DAY",
        ref_price=415.00,
        band_pct=20.0,
        ltp=419.00,
        now=market_ts,
        open_orders=[],
        order_tag="cup",
    )
    assert violations_full == []


def test_validate_order_catches_all_faults_on_multi_fault_order():
    """A single multi-fault order must return EVERY violation found, never stopping at the first."""
    after_hours_saturday = datetime(2026, 10, 17, 19, 0, 0, tzinfo=IST)
    existing_orders = [
        {"id": "DUP99", "symbol": "NSE:BAFNAPH-BE", "side": "SELL", "qty": 10, "status": "pending"},
    ]

    # Construct an order that simultaneously violates ALL 10+ rules:
    #  1. tick_size:        price=436.283 (not a multiple of 0.05)
    #  2. lot_size:         qty=105 on lot_size=30 (not a multiple of 30)
    #  3. freeze_quantity:  qty=105 with freeze_qty=60 (exceeds freeze limit)
    #  4. price_band:       price=436.283 vs ref_price=300.0, band_pct=5.0 (outside [285, 315])
    #  5. order_value_cap:  105 * 436.283 = Rs 45,809.72 > max_order_value=25,000
    #  6. t2t_series:       symbol="BAFNAPH" (T2T -BE series)
    #  7. validity:         validity="IOC" on a resting stop (invalid for stop)
    #  8. product_type:     product="INTRADAY" on T2T / overnight strategy
    #  9. market_hours:     Saturday 19:00 IST with offline_order=False
    # 10. duplicate_guard:  existing SELL stop DUP99 already resting for BAFNAPH
    # 11. order_tag:        "bad_tag!_too_long_123456" (non-alphanumeric & >20 chars)
    # 12. sl_trigger_dir:   SELL stop @ 436.283 when ltp=400.0 (stop >= LTP)
    violations = validate_order(
        "BAFNAPH",
        "SELL",
        105,
        436.283,
        "INTRADAY",
        "IOC",
        lot_size=30,
        freeze_qty=60,
        ref_price=300.0,
        band_pct=5.0,
        max_order_value=25_000.0,
        now=after_hours_saturday,
        open_orders=existing_orders,
        position_qty=50,
        order_tag="bad_tag!_too_long_123456",
        ltp=400.0,
    )

    joined = "\n".join(violations)
    assert len(violations) >= 10, f"Expected at least 10 violations, got {len(violations)}:\n{joined}"

    expected_prefixes = (
        "tick_size:",
        "lot_size:",
        "freeze_quantity:",
        "price_band:",
        "order_value_cap:",
        "t2t_series:",
        "validity:",
        "product_type:",
        "market_hours:",
        "duplicate_guard:",
        "order_tag:",
        "sl_trigger_direction:",
    )
    for prefix in expected_prefixes:
        assert any(v.startswith(prefix) for v in violations), (
            f"Missing violation starting with {prefix!r} in:\n{joined}"
        )


def test_validate_order_6_positional_args_multi_fault():
    """Even with only the 6 required positional args (symbol, side, qty, price, product, validity),
    validate_order catches multiple simultaneous faults at once."""
    violations = validate_order(
        "BAFNAPH",       # Rule 6: T2T -BE symbol
        "BUY",
        150_000,         # Rule 3: > 100,000 freeze qty AND Rule 5: > Rs 50,000 value cap
        436.28,          # Rule 1: not a multiple of 0.05 tick
        "INTRADAY",      # Rule 8: INTRADAY rejected for T2T / overnight hold
        "IOC",           # Rule 7: IOC rejected for resting stop
    )
    joined = "\n".join(violations)
    assert len(violations) == 6, f"Expected 6 violations from 6-arg call, got:\n{joined}"
    for prefix in (
        "tick_size:",
        "freeze_quantity:",
        "order_value_cap:",
        "t2t_series:",
        "validity:",
        "product_type:",
    ):
        assert any(v.startswith(prefix) for v in violations), f"Missing {prefix} in:\n{joined}"


def test_strict_unverifiable_surfaces_missing_live_feeds():
    """When strict_unverifiable=True, validate_order explicitly reports rules that cannot
    be enforced without live quote/circuit feeds (price_band and sl_trigger_direction)."""
    violations = validate_order(
        "NAZARA",
        "BUY",
        28,
        436.30,
        "CNC",
        "DAY",
        strict_unverifiable=True,
    )
    assert len(violations) == 2
    assert any("price_band: CANNOT ENFORCE offline" in v for v in violations)
    assert any("sl_trigger_direction: CANNOT ENFORCE offline" in v for v in violations)
    assert RULE_ENFORCEABILITY["price_band"]["enforced_offline"] == "requires_ref_price_or_bounds"
