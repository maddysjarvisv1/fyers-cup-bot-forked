"""momentum_screener.py: one positive fixture, one negative control per clause,
owner-confirmed row semantics (previous ATH, entry = prev_ath*1.04, 6% stop,
Rs 750-900 defined-loss band, SONACOMS acceptance fixture + boundary controls),
the counter wrapper, and run verdicts (clean-zero vs all-fail).
Mock-only: a fake Fyers history client; no network, no credentials."""
import datetime as dt
import json
import os
import sys
import types
from collections import Counter
from decimal import Decimal

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[:1] != [ROOT]:
    if ROOT in sys.path:
        sys.path.remove(ROOT)
    sys.path.insert(0, ROOT)

import cup_sl_engine as sl
import momentum_screener as ms
from fyers_client import FyersError
from scripts import sonacoms_order as so

IST = ms.IST
ASOF = dt.date(2026, 10, 21)   # a Wednesday, safely in the past for main()'s session-closed guard
FLAT = dict(o=700.0, h=707.0, l=693.0, c=700.0, v=200_000)
SPIKE = dict(o=790.0, h=860.0, l=785.0, c=851.85, v=1_000_000)   # SONACOMS-shaped breakout, 5x volume


def _ts(d):
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())


def make_series(asof=ASOF, days=400, flat=None, today=None, edits=None):
    """Weekday FLAT bars from asof-days to asof-1, then the `today` bar on asof
    (today=False omits it). edits: {days_before_asof: {field: value}}."""
    f = dict(FLAT, **(flat or {}))
    rows = {}
    d = asof - dt.timedelta(days=days)
    while d < asof:
        if d.weekday() < 5:
            rows[d] = dict(f)
        d += dt.timedelta(days=1)
    for back, fields in (edits or {}).items():
        day = asof - dt.timedelta(days=back)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        rows[day] = dict(rows.get(day, f), **fields)
    if today is not False:
        rows[asof] = dict(SPIKE, **(today or {}))
    return [[_ts(d), r["o"], r["h"], r["l"], r["c"], r["v"]] for d, r in sorted(rows.items())]


class FakeFyers:
    def __init__(self, series=None, errors=None, raw=None):
        self.series, self.errors, self.raw, self.calls = series or {}, errors or {}, raw or {}, []

    def history(self, symbol, range_from, range_to, resolution="D"):
        self.calls.append((symbol, range_from, range_to, resolution))
        if symbol in self.errors:
            raise self.errors[symbol]
        if symbol in self.raw:
            return self.raw[symbol]
        lo, hi = dt.date.fromisoformat(range_from), dt.date.fromisoformat(range_to)
        rows = [r for r in self.series.get(symbol, [])
                if lo <= dt.datetime.fromtimestamp(r[0], IST).date() <= hi]
        return {"s": "ok", "code": 200, "candles": rows} if rows else {"s": "no_data", "candles": []}


def screen(rows, sym="SONACOMS", asof=ASOF):
    stats = ms.ScanStats()
    sig = ms.screen_one(FakeFyers({sym: rows}), sym, asof, stats, ms.Throttle(0))
    return sig, stats


# --- positive fixture -------------------------------------------------------------
def test_positive_fixture_is_a_hit_labelled_ath():
    sig, stats = screen(make_series(edits={120: {"h": 839.0, "c": 825.0}}))
    assert sig is not None, stats.outcomes
    assert stats.outcomes == Counter({"hit": 1}) and not stats.errors
    assert (sig.high_kind, sig.ath_check) == ("ATH", "ok")
    assert sig.prior_52w_high == pytest.approx(839.0)
    assert sig.prev_ath == pytest.approx(839.0)
    assert sig.vol_mult == pytest.approx(5.0)
    assert sig.range_pos == pytest.approx((851.85 - 785.0) / 75.0)


def test_52w_high_that_is_not_an_ath_is_labelled_52w():
    # a 900 high ~380 days back: outside the 52-week window, inside the ATH walk
    sig, _ = screen(make_series(edits={380: {"h": 900.0}}))
    assert sig is not None and sig.high_kind == "52W"


# --- previous ATH vs latest high (owner override) ---------------------------------
def test_new_high_today_measured_against_previous_ath_not_latest_high():
    """SONACOMS-shaped fixture: prior peak was 839.0, today spiked to a NEW high of 860.0
    (and closed at 851.85). Also test a multi-day breakout leg where yesterday already
    poked to 845.0 before today's 860.0. In both cases, previous ATH must remain 839.0,
    NEVER today's 860.0 or yesterday's 845.0."""
    # Case A: single-bar breakout on asof (today.h = 860.0 > prev_ath = 839.0)
    rows_single = make_series(edits={120: {"h": 839.0, "c": 820.0}})
    sig_a, _ = screen(rows_single, sym="SONACOMS")
    assert sig_a is not None
    assert sig_a.high == pytest.approx(860.0)       # today made a new all-time high
    assert sig_a.prev_ath == pytest.approx(839.0)   # measured against previous ATH 839, never 860
    row_a = ms.build_row(sig_a, capital_budget=50_000.0, asof=ASOF)
    assert row_a["rim"] == pytest.approx(839.0)
    assert row_a["trigger"] == pytest.approx(872.55)
    assert row_a["stop"] == pytest.approx(820.20)
    assert row_a["risk_per_share"] == pytest.approx(52.35)
    assert row_a["qty"] == 16
    assert row_a["risk"] == pytest.approx(837.60)

    # Case B: multi-day breakout leg (day -2 rose to 780, day -1 poked to 845, today hit 860)
    rows_multi = make_series(
        edits={
            120: {"h": 839.0, "c": 820.0},
            2: {"o": 710.0, "h": 780.0, "l": 705.0, "c": 775.0},
            1: {"o": 778.0, "h": 845.0, "l": 775.0, "c": 840.0},
        }
    )
    sig_b, _ = screen(rows_multi, sym="SONACOMS")
    assert sig_b is not None
    assert sig_b.prev_ath == pytest.approx(839.0)


# --- SONACOMS acceptance fixture & loss-band boundary controls --------------------
def test_sonacoms_acceptance_fixture():
    """Acceptance fixture: previous high 839 -> entry 872.55 -> stop 820.20 ->
    risk/share 52.35 -> qty 16 -> risk 837.60."""
    levels = ms.compute_cup_levels(839.0, capital_budget=50_000.0)
    assert levels["prev_ath"] == pytest.approx(839.0)
    assert levels["entry"] == pytest.approx(872.55)
    assert levels["trigger"] == pytest.approx(872.55)
    assert levels["stop"] == pytest.approx(820.20)
    assert levels["risk_per_share"] == pytest.approx(52.35)
    assert levels["qty"] == 16
    assert levels["risk"] == pytest.approx(837.60)
    assert levels["unsizable_reason"] is None

    # Verify sonacoms_order.compute_plan produces the exact same numbers
    plan = so.compute_plan(839.0, atr14=24.43, capital_budget=50_000.0,
                           budget_left=50_000.0, funds_available=50_000.0)
    assert plan["H"] == pytest.approx(839.0)
    assert plan["trigger"] == pytest.approx(872.55)
    assert plan["limit"] == pytest.approx(872.55)
    assert plan["stop"] == pytest.approx(820.20)
    assert plan["risk_per_share"] == pytest.approx(52.35)
    assert plan["qty"] == 16
    assert plan["risk_cash"] == pytest.approx(837.60)
    assert plan["band_reason"] is None


def test_loss_band_boundary_controls_qty_15_16_17_and_budget_rejection(monkeypatch, tmp_path):
    """Boundary controls:
    1) qty 15 and qty 17 are reachable for other entry prices in the [750, 900] band
       (and qty 15 is also reachable when budget caps SONACOMS to 15 shares).
    2) When the band cannot be satisfied with the available budget, it is reported as
       unsizable ('insufficient_budget_for_loss_band') rather than silently violating the band.
    """
    # Reachable qty = 15 via entry price in band (prev_ath = 880 -> entry = 915.20, stop = 860.30, risk/sh = 54.90)
    lvl_15 = ms.compute_cup_levels(880.0, capital_budget=50_000.0)
    assert lvl_15["entry"] == pytest.approx(915.20)
    assert lvl_15["stop"] == pytest.approx(860.30)
    assert lvl_15["risk_per_share"] == pytest.approx(54.90)
    assert lvl_15["qty"] == 15
    assert 750.0 <= lvl_15["risk"] <= 900.0
    assert lvl_15["risk"] == pytest.approx(823.50)

    # Reachable qty = 15 for SONACOMS (839) when budget allows 15 shares (13,100) but not 16 (13,960.80)
    lvl_15_b = ms.compute_cup_levels(839.0, capital_budget=13_100.0)
    assert lvl_15_b["qty"] == 15
    assert lvl_15_b["risk"] == pytest.approx(785.25)
    assert 750.0 <= lvl_15_b["risk"] <= 900.0

    # Reachable qty = 17 via entry price in band (prev_ath = 780 -> entry = 811.20, stop = 762.55, risk/sh = 48.65)
    lvl_17 = ms.compute_cup_levels(780.0, capital_budget=50_000.0)
    assert lvl_17["entry"] == pytest.approx(811.20)
    assert lvl_17["stop"] == pytest.approx(762.55)
    assert lvl_17["risk_per_share"] == pytest.approx(48.65)
    assert lvl_17["qty"] == 17
    assert 750.0 <= lvl_17["risk"] <= 900.0
    assert lvl_17["risk"] == pytest.approx(827.05)

    # Budget cannot satisfy the [750, 900] band (budget Rs 10,000 -> max 11 shares -> risk Rs 575.85 < 750)
    lvl_bad = ms.compute_cup_levels(839.0, capital_budget=10_000.0)
    assert lvl_bad["qty"] is None
    assert lvl_bad["risk"] is None
    assert "insufficient_budget_for_loss_band" in lvl_bad["unsizable_reason"]

    # End-to-end scan reports the budget-unsizable symbol in status["unsizable"] and status["unsizable_reasons"]
    rows = make_series(edits={120: {"h": 839.0, "c": 820.0}})
    status, cand_rows = ms.run_scan(
        FakeFyers(series={"SONACOMS": rows}),
        ["SONACOMS"],
        ASOF,
        capital_budget=10_000.0,
        min_ok=1,
        interval=0,
        cand_path=str(tmp_path / "cand.csv"),
        status_path=str(tmp_path / "status.json"),
    )
    assert cand_rows == []
    assert status["candidates"] == 0
    assert status["unsizable"] == ["SONACOMS"]
    assert "insufficient_budget_for_loss_band" in status["unsizable_reasons"]["SONACOMS"]


# --- one negative control per clause ----------------------------------------------
def test_negative_close_below_prior_52w_high():
    sig, stats = screen(make_series(edits={100: {"h": 870.0}}))
    assert sig is None and stats.outcomes == Counter({"below_52w_high": 1})


def test_negative_volume_below_2x_average():
    sig, stats = screen(make_series(today={"v": 399_999}))
    assert sig is None and stats.outcomes == Counter({"no_volume_spike": 1})


def test_volume_exactly_2x_average_passes():
    sig, _ = screen(make_series(today={"v": 400_000}))
    assert sig is not None


def test_negative_close_outside_top_third():
    sig, stats = screen(make_series(today={"c": 805.0}))
    assert sig is None and stats.outcomes == Counter({"weak_close": 1})


def test_close_exactly_at_two_thirds_passes():
    sig, _ = screen(make_series(today={"c": 835.0}))     # (835-785)/75 == 2/3
    assert sig is not None


def test_negative_illiquid_average_volume():
    sig, stats = screen(make_series(flat={"v": 99_000}, today={"v": 5_000_000}))
    assert sig is None and stats.outcomes == Counter({"illiquid": 1})


def test_stale_feed_is_not_a_hit():
    sig, stats = screen(make_series(today=False))
    assert sig is None and stats.outcomes == Counter({"stale_data": 1})


# --- row semantics ----------------------------------------------------------------------
def test_row_semantics_use_owner_parameters():
    sig, _ = screen(make_series(edits={120: {"h": 839.0, "c": 820.0}}))
    row = ms.build_row(sig, capital_budget=50_000.0, asof=ASOF)
    assert sig.atr14 == pytest.approx((13 * 14.0 + 160.0) / 14)
    assert row["kind"] == "momentum"
    assert row["rim"] == pytest.approx(839.0)
    assert row["trigger"] == pytest.approx(872.55)      # align_tick(839 * 1.04)
    assert row["stop"] == pytest.approx(820.20)         # align_tick(872.55 * 0.94)
    assert row["risk_per_share"] == pytest.approx(52.35)
    assert row["qty"] == 16
    assert row["risk"] == pytest.approx(837.60)
    assert 750.0 <= row["risk"] <= 900.0
    assert row["time_stop_days"] == 5
    assert row["valid_until"] == "2026-10-22"
    for k in ("rim", "trigger", "stop"):
        assert Decimal(str(row[k])) % Decimal("0.05") == 0


def test_take_profit_ladder_unchanged():
    """Take-profit ladder is unchanged: +10% breakeven, +20% book half, then 10% trailing stop."""
    assert sl.SL_PCT == pytest.approx(0.06)
    assert sl.BE_TRIGGER == pytest.approx(0.10)
    assert sl.PARTIAL_TRIGGER == pytest.approx(0.20)
    assert sl.TRAIL_PCT == pytest.approx(0.10)

    st = sl.PosState(entry=872.55, qty_total=16, qty_remaining=16, sl=820.20)
    # +10% -> breakeven
    st, acts = sl.step(st, round(872.55 * 1.10, 2))
    assert st.phase == "BE" and st.sl == pytest.approx(872.55)
    assert ("modify_sl", 872.55) in acts
    # +20% -> book half (8 shares) + start trailing 10% below high
    px_20 = round(872.55 * 1.20, 2)
    st, acts = sl.step(st, px_20)
    assert st.phase == "TRAILING" and st.qty_remaining == 8
    assert ("book_partial", 8, px_20) in acts
    assert st.sl == pytest.approx(round(px_20 * 0.90, 2))
    # New high -> trail moves up to 10% below new high
    st, acts = sl.step(st, 1100.0)
    assert st.sl == pytest.approx(990.0)
    assert ("modify_sl", 990.0) in acts


def test_unverified_fyers_fields_fail_closed():
    """Two Fyers field pairs (lower_ckt/upper_ckt in depth, quantity/costPrice in holdings)
    are unverified against repo fixtures; missing fields must fail closed with FyersError."""
    class BadDepthHoldingsClient:
        def depth(self, sym):
            return {"s": "ok", "d": {"NSE:SONACOMS-EQ": {"lp": 850.0}}}

        def positions(self):
            return {"s": "ok", "netPositions": []}

        def holdings(self):
            return {"s": "ok", "holdings": [{"symbol": "NSE:SONACOMS-EQ", "holdingQty": 10}]}

    c = BadDepthHoldingsClient()
    with pytest.raises(FyersError, match="lower_ckt/upper_ckt"):
        so.circuit_band(c, "SONACOMS")
    with pytest.raises(FyersError, match="quantity.*costPrice"):
        so.held_qty(c, "SONACOMS")


# --- counter wrapper ---------------------------------------------------------------------
def test_counter_wrapper_counts_every_exception_by_type():
    client = FakeFyers(
        series={"GOOD": make_series(), "FLAT": make_series(today=dict(FLAT))},
        errors={"THROTTLED": FyersError("history failed: code=429"), "BOOM": ConnectionError("reset")},
        raw={"MALFORMED": {"s": "ok", "candles": [[1, 2, 3]]}, "NOKEY": {"s": "ok"}},
    )
    stats = ms.ScanStats()
    syms = ("GOOD", "FLAT", "THROTTLED", "BOOM", "MALFORMED", "NOKEY", "BAFNAPH")
    hits = [ms.screen_one(client, s, ASOF, stats, ms.Throttle(0)) for s in syms]
    assert [h.symbol for h in hits if h] == ["GOOD"]
    assert stats.outcomes == Counter({"hit": 1, "no_volume_spike": 1, "t2t_excluded": 1})
    assert stats.errors == Counter({"FyersError": 1, "ConnectionError": 1, "BadCandles": 2})
    assert (stats.symbols_ok, stats.n_errors) == (3, 4)
    assert stats.error_samples["FyersError"][0].startswith("THROTTLED:")


# --- run verdicts -------------------------------------------------------------------------
def _run_main(monkeypatch, out_dir, client, symbols, min_ok):
    out_dir.mkdir()
    monkeypatch.setattr(ms, "FyersClient", types.SimpleNamespace(from_files=lambda: client))
    monkeypatch.setattr(ms, "fetch_universe", lambda max_n=None: list(symbols))
    monkeypatch.setattr(ms, "resolve_capital_budget", lambda *a, **k: 50_000.0)
    rc = ms.main(["scan", "--asof", ASOF.isoformat(), "--min-ok", str(min_ok),
                  "--interval", "0", "--out-dir", str(out_dir)])
    return rc, json.loads((out_dir / "momentum_status.json").read_text())


def test_clean_zero_candidate_run_is_distinguishable_from_all_fail_run(monkeypatch, tmp_path):
    syms = [f"S{i:02d}" for i in range(30)]
    flat = make_series(today=dict(FLAT))
    rc_ok, clean = _run_main(monkeypatch, tmp_path / "clean",
                             FakeFyers(series={s: flat for s in syms}), syms, 30)
    rc_bad, fail = _run_main(monkeypatch, tmp_path / "fail",
                             FakeFyers(errors={s: FyersError("token expired") for s in syms}), syms, 30)

    assert clean["candidates"] == fail["candidates"] == 0   # identical under `except: return None`

    assert (rc_ok, clean["verdict"]) == (0, "OK")
    assert clean["symbols_ok"] == 30 and clean["errors_by_type"] == {}
    assert clean["outcomes"] == {"no_volume_spike": 30}
    assert (tmp_path / "clean" / "momentum_candidates.csv").read_text() == ",".join(ms.ROW_FIELDS) + "\n"

    assert (rc_bad, fail["verdict"]) == (2, "FAILED")
    assert fail["symbols_ok"] == 0
    assert fail["errors_by_type"] == {"FyersError": ms.EARLY_ABORT_ERRORS}
    assert fail["aborted"] is True
    assert not (tmp_path / "fail" / "momentum_candidates.csv").exists()


@pytest.mark.parametrize("n_err,verdict", [(0, "OK"), (2, "OK"), (3, "DEGRADED"),
                                           (5, "DEGRADED"), (6, "FAILED")])
def test_verdict_error_rate_bands(n_err, verdict):
    stats = ms.ScanStats(outcomes=Counter({"below_52w_high": 100 - n_err}),
                         errors=Counter({"FyersError": n_err}))
    assert ms.judge(stats, min_ok=0)[0] == verdict


def test_default_floor_requires_2000_symbols_ok():
    stats = ms.ScanStats(outcomes=Counter({"no_volume_spike": 1999}))
    v, reasons = ms.judge(stats)
    assert v == "FAILED" and any("1999 < 2000" in r for r in reasons)
    stats.outcomes["no_volume_spike"] += 1
    assert ms.judge(stats)[0] == "OK"


def test_stale_majority_fails_the_run():
    stats = ms.ScanStats(outcomes=Counter({"stale_data": 1500, "no_volume_spike": 600}))
    v, reasons = ms.judge(stats)
    assert v == "FAILED" and any("stale_data" in r for r in reasons)


def test_offline_cli_fixture_scan_and_list(tmp_path, capsys):
    """Run momentum_screener.py CLI offline against a fixture file (SONACOMS + band
    boundary controls + reject controls) with zero network or live API calls."""
    fixture = {
        "capital_budget": 50_000.0,
        "symbols": ["SONACOMS", "BAND15", "BAND17", "FLATCO", "BAFNAPH"],
        "series": {
            "SONACOMS": make_series(edits={120: {"h": 839.0, "c": 820.0}}),
            "BAND15": make_series(
                edits={120: {"h": 880.0, "c": 860.0}},
                today={"o": 850.0, "h": 905.0, "l": 845.0, "c": 895.0, "v": 1_000_000},
            ),
            "BAND17": make_series(
                edits={120: {"h": 780.0, "c": 760.0}},
                today={"o": 750.0, "h": 805.0, "l": 745.0, "c": 795.0, "v": 1_000_000},
            ),
            "FLATCO": make_series(today=dict(FLAT)),
        },
    }
    fx_path = tmp_path / "fixture.json"
    fx_path.write_text(json.dumps(fixture), encoding="utf-8")

    rc = ms.main([
        "scan", "--asof", ASOF.isoformat(), "--min-ok", "5",
        "--interval", "0", "--out-dir", str(tmp_path), "--fixture", str(fx_path),
    ])
    assert rc == 0
    status = json.loads((tmp_path / "momentum_status.json").read_text(encoding="utf-8"))
    assert status["verdict"] == "OK"
    assert status["symbols_ok"] == 5
    assert status["candidates"] == 3
    assert status["outcomes"] == {"hit": 3, "no_volume_spike": 1, "t2t_excluded": 1}

    ms.list_cands(str(tmp_path / "momentum_candidates.csv"))
    out = capsys.readouterr().out
    assert "3 momentum candidates" in out
    assert "SONACOMS" in out


def test_scan_creates_nonexistent_nested_out_dir_and_writes_status_and_candidates(
    monkeypatch, tmp_path, capsys
):
    """Regression for task-185: scan with a non-existent nested --out-dir creates parent
    directories and writes both momentum_status.json and momentum_candidates.csv."""
    out_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not out_dir.exists()

    series = {
        "SONACOMS": make_series(edits={120: {"h": 839.0, "c": 820.0}}),
        "FLATCO": make_series(today=dict(FLAT)),
    }
    monkeypatch.setattr(
        ms, "FyersClient", types.SimpleNamespace(from_files=lambda: FakeFyers(series=series))
    )
    monkeypatch.setattr(ms, "fetch_universe", lambda max_n=None: ["SONACOMS", "FLATCO"])
    monkeypatch.setattr(ms, "resolve_capital_budget", lambda *a, **k: 50_000.0)

    rc = ms.main([
        "scan", "--asof", ASOF.isoformat(), "--min-ok", "2",
        "--interval", "0", "--out-dir", str(out_dir),
    ])
    assert rc == 0

    status_file = out_dir / "momentum_status.json"
    cand_file = out_dir / "momentum_candidates.csv"
    assert status_file.is_file()
    assert cand_file.is_file()
    assert list(out_dir.glob(".momentum_*.tmp")) == []

    status = json.loads(status_file.read_text(encoding="utf-8"))
    assert status["tool"] == "momentum_screener"
    assert status["asof"] == ASOF.isoformat()
    assert status["verdict"] == "OK"
    assert status["verdict_reasons"] == []
    assert status["exit_code"] == 0
    assert status["universe"] == 2
    assert status["attempted"] == 2
    assert status["symbols_ok"] == 2
    assert status["errors"] == 0
    assert status["error_rate"] == 0.0
    assert status["candidates"] == 1
    assert status["outcomes"] == {"hit": 1, "no_volume_spike": 1}
    assert status["candidates_path"] == str(cand_file)

    cand_lines = cand_file.read_text(encoding="utf-8").splitlines()
    assert len(cand_lines) == 2
    assert cand_lines[0] == ",".join(ms.ROW_FIELDS)
    assert cand_lines[1].startswith("SONACOMS,839.0,872.55,16,momentum,820.2,52.35,837.6,")

    capsys.readouterr()
    assert ms.main(["list", "--out-dir", str(out_dir)]) == 0
    assert "1 momentum candidates" in capsys.readouterr().out

    # Also verify _fail_setup writes status artifact when --out-dir does not exist yet
    setup_fail_dir = tmp_path / "missing_parent" / "setup_fail"
    assert not setup_fail_dir.exists()
    rc_fail = ms.main([
        "scan", "--asof", ASOF.isoformat(), "--out-dir", str(setup_fail_dir),
        "--fixture", str(tmp_path / "no_such_fixture.json"),
    ])
    assert rc_fail == 2
    fail_status = json.loads((setup_fail_dir / "momentum_status.json").read_text(encoding="utf-8"))
    assert fail_status["verdict"] == "FAILED"
    assert fail_status["exit_code"] == 2
    assert fail_status["aborted"] is True


def test_unwritable_out_dir_raises_clear_error(monkeypatch, tmp_path):
    """Negative control for task-185: a read-only or unwritable target directory fails
    loudly with a clear error message and is never swallowed."""
    ro_dir = tmp_path / "readonly_dir"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        with pytest.raises(PermissionError, match="not writable") as exc_info:
            ms._atomic_write(str(ro_dir / "momentum_status.json"), "{}\n")
        assert str(ro_dir) in str(exc_info.value)

        nested_under_ro = ro_dir / "nested" / "sub"
        series = {"SONACOMS": make_series(edits={120: {"h": 839.0, "c": 820.0}})}
        with pytest.raises(PermissionError, match="not writable") as scan_exc:
            ms.run_scan(
                FakeFyers(series=series),
                ["SONACOMS"],
                ASOF,
                capital_budget=50_000.0,
                min_ok=1,
                interval=0,
                cand_path=str(nested_under_ro / "momentum_candidates.csv"),
                status_path=str(nested_under_ro / "momentum_status.json"),
            )
        assert str(nested_under_ro) in str(scan_exc.value)
        assert not nested_under_ro.exists()
    finally:
        ro_dir.chmod(0o755)

    blocker_file = tmp_path / "file_not_a_dir"
    blocker_file.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(
        ms,
        "FyersClient",
        types.SimpleNamespace(
            from_files=lambda: FakeFyers(series={"FLATCO": make_series(today=dict(FLAT))})
        ),
    )
    monkeypatch.setattr(ms, "fetch_universe", lambda max_n=None: ["FLATCO"])
    monkeypatch.setattr(ms, "resolve_capital_budget", lambda *a, **k: 50_000.0)
    with pytest.raises(NotADirectoryError, match="not a directory"):
        ms.main([
            "scan", "--asof", ASOF.isoformat(), "--min-ok", "1",
            "--interval", "0", "--out-dir", str(blocker_file / "nested"),
        ])


def test_live_fyers_history_skipped_when_token_stale():
    """Any test requiring live Fyers market data must be explicitly skipped when token.json is stale."""
    if not so.token_is_today("token.json"):
        pytest.skip("Live Fyers market token.json is missing or stale today; skipping live API check")

