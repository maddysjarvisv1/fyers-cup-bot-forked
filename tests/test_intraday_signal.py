"""Tests for intraday_paper.signal — pure trigger + exit-mode math + cost netting
+ no-lookahead. PURE: no client/no network/no io."""
import pytest

from intraday_paper.signal import (
    detect_signal,
    initial_stop,
    compute_net_pnl,
    sample_prior_resistance,
    simulate_exits,
)

EPS = 0.0


def _bar(o, h, l, c, slot=0):
    return {"sym": "SYM", "open": o, "high": h, "low": l, "close": c,
            "slot": slot, "finalized": True}


# ================= detect_signal =================
def test_green_open_equals_low_is_signal():
    res = detect_signal(_bar(o=100.0, h=106.0, l=100.0, c=105.0), epsilon=EPS)
    assert res["triggered"] is True
    assert res["signal_bar_close"] == 105.0
    assert res["signal_bar_low"] == 100.0


def test_non_green_close_le_open():
    assert detect_signal(_bar(o=100.0, h=103.0, l=100.0, c=100.0), epsilon=EPS)["triggered"] is False
    assert detect_signal(_bar(o=100.0, h=103.0, l=100.0, c=99.0), epsilon=EPS)["triggered"] is False


def test_open_not_equal_low_no_signal():
    # open above low disqualified
    assert detect_signal(_bar(o=101.0, h=106.0, l=100.0, c=105.0), epsilon=EPS)["triggered"] is False


def test_epsilon_allows_tiny_low_delta():
    # open==low within epsilon accepted
    bar = _bar(o=100.0, h=105.0, l=100.0 + 1e-7, c=104.0)
    assert detect_signal(bar, epsilon=1e-6)["triggered"] is True


def test_epsilon_rejects_beyond_epsilon():
    bar = _bar(o=100.0, h=105.0, l=100.0 + 0.05, c=104.0)
    assert detect_signal(bar, epsilon=1e-3)["triggered"] is False


# ================= initial stop =================
def test_initial_stop_low_minus_points():
    assert initial_stop(low=100.0, sl_points=2.0) == 98.0


# ================= net pnl =================
def test_net_pnl_gross_minus_cost():
    assert compute_net_pnl(entry=100.0, exit=106.0, cost_in_points=2.0) == 4.0
    assert compute_net_pnl(entry=100.0, exit=97.0, cost_in_points=2.0) == -5.0
    assert compute_net_pnl(entry=100.0, exit=103.0, cost_in_points=0.0) == 3.0


# ================= prior resistance =================
def test_prior_resistance_highest_low_among_prior():
    prior = [_bar(90, 99, 90, 95, 0), _bar(95, 112, 94, 110, 1), _bar(110, 111, 105, 108, 2)]
    assert sample_prior_resistance(prior) == 112.0


def test_prior_resistance_none_empty():
    assert sample_prior_resistance([]) is None


# ================= simulate_exits =================
def test_exit_mode_prior_resistance_target():
    """Exit on prior-resistance: SL stop never hit first, price rises to
    prior swing high (resistance) — mode (a)."""
    signal = _bar(o=100.0, h=118.0, l=100.0, c=117.0, slot=5)
    prior = [_bar(90, 124.0, 90, 100, 4)]   # prior swing high = 124
    later = [
        _bar(117, 120, 116, 119, 6),
        _bar(119, 126.0, 118, 125, 7),       # hits resistance 124 -> target
    ]
    modes = simulate_exits(signal, prior, later,
                           cost_in_points=2.0, sl_points=2.0,
                           prior_lookback=10, three_bar_count=3)
    m = modes["prior_resistance"]
    assert m["exit_price"] == 124.0
    assert m["net_pnl"] == pytest.approx(compute_net_pnl(117.0, 124.0, 2.0))  # 5.0


def test_exit_mode_trail_ratchets_to_new_low():
    """Mode (b): trail SL ratchets to each new bar low; exit when price falls to trail."""
    signal = _bar(o=100.0, h=110.0, l=100.0, c=108.0, slot=0)
    prior = []
    later = [
        _bar(108, 111, 106, 110, 1),   # trail SL -> 106
        _bar(110, 112, 108, 111, 2),   # trail SL -> 108 (188 above prior 106)
        _bar(111, 111, 105, 106, 3),   # low 105 <= trail SL 108 -> exit at 108
    ]
    modes = simulate_exits(signal, prior, later,
                           cost_in_points=2.0, sl_points=2.0,
                           prior_lookback=10, three_bar_count=3)
    m = modes["trail"]
    assert m["exit_price"] == 108.0
    # net = (108 - 108) - 2 = -2


def test_exit_mode_three_bar_profit():
    """Mode (c): 3-bar profit exit — close 3 bars after signal, if in profit,
    exit at that bar's close."""
    signal = _bar(o=100.0, h=110.0, l=100.0, c=108.0, slot=0)
    prior = []
    later = [
        _bar(108, 109, 107, 108.5, 1),
        _bar(108.5, 109.5, 108, 109, 2),
        _bar(109, 110.5, 108.5, 110.0, 3),  # 3rd bar, in profit -> exit 110
    ]
    modes = simulate_exits(signal, prior, later,
                           cost_in_points=2.0, sl_points=2.0,
                           prior_lookback=10, three_bar_count=3)
    m = modes["three_bar"]
    assert m["exit_price"] == 110.0
    assert m["net_pnl"] == pytest.approx(compute_net_pnl(108.0, 110.0, 2.0))


def test_no_lookahead_only_finalized_upto_now():
    """Signal/exit logic must never peek forward: exit at bar n uses only bars
    0..n (all finalized) — a later bar's data must not influence an earlier exit."""
    signal = _bar(o=100.0, h=110.0, l=100.0, c=108.0, slot=0)
    prior = [_bar(90, 124.0, 90, 100, -1)]   # resistance 124
    later = [
        _bar(108, 110, 99.0, 100, 1),   # low 99 < SL(98)? no, SL=98 — still ok
        _bar(100, 101, 95.0, 96, 2),    # low 95 <= trail(99) -> exit trail at 99
    ]
    modes = simulate_exits(signal, prior, later,
                           cost_in_points=2.0, sl_points=2.0,
                           prior_lookback=10, three_bar_count=3)
    # The trail exit at slot=2 must only use bars up to slot 2 (prior + later[0:3]);
    # it must not "see" a hypothetical futuristic resistance.
    assert modes["trail"]["exit_price"] == 99.0
    # prior_resistance uses prior swing high (124) — never later bars' highs
    assert modes["prior_resistance"]["target"] == 124.0