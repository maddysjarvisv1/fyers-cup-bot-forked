"""
Tests for Fix 2: new_high_detector.py — separate alert-only new-high + volume detector.

Covers:
  1. Positive cases:
     - All-Time High (ATH) breakout with volume >= 2.0x 50d avg, close in top third, avg_vol >= 1e5 -> fires with high_type='ATH'.
     - 52-Week High (52W) breakout (below older multi-year ATH) with all other clauses holding -> fires with high_type='52W'.
  2. Single-clause negative controls (each satisfies the other 3 clauses and fails ONLY on its target clause):
     - low_volume: vol_ratio = 1.25x (< 2.0x)
     - close_not_in_top_third: close in bottom third of day's range (range_pos = 0.1333 < 0.6667) while still >= prior 52w high
     - low_average_volume: 50d avg volume = 40,000 (< 1e5) despite 3.0x volume spike
     - not_new_high: close = 190.0 (< prior 52w high 200.0) despite 2.5x volume and top-third close
  3. Hard boundary verification:
     - AST import list contains NO order-path modules (bot, broker, cup_sl_engine, run, watchlist_sync, ledger) and NO yfinance.
     - ReadOnlyFyersDataAdapter raises PermissionError on any order method (place_buy_stop, modify_order, cancel_order, etc.).
     - Explicit error when client only exposes .quotes() without .history()/.fy.history() (never falls back to yfinance).
     - Output schema is its own schema (not cup watchlist symbol,rim,trigger,qty) and writes JSON status artifact.
"""
from __future__ import annotations

import ast
import csv
import datetime
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT in sys.path:
    sys.path.remove(ROOT)
sys.path.insert(0, ROOT)

import new_high_detector
from fyers_client import FyersError


def _build_bars(
    *,
    num_prior_bars: int = 300,
    old_peak_high: float = 180.0,
    prior_52w_high: float = 200.0,
    avg_vol_50d: float = 200_000.0,
    today_low: float = 200.0,
    today_high: float = 212.0,
    today_close: float = 210.0,
    today_vol: float = 500_000.0,
) -> list[dict]:
    """Build deterministic settled daily bars with independent control over each clause."""
    base_date = datetime.date(2025, 1, 1)
    bars: list[dict] = []
    for i in range(num_prior_bars):
        d = base_date + datetime.timedelta(days=i)
        if i == 0:
            # Old peak outside the 252-bar 52-week window when num_prior_bars > 252
            h = old_peak_high
            l = old_peak_high - 10.0
            c = old_peak_high - 2.0
        elif i == num_prior_bars - 20:
            # 52-week prior peak
            h = prior_52w_high
            l = prior_52w_high - 8.0
            c = prior_52w_high - 3.0
        else:
            h = 150.0
            l = 140.0
            c = 145.0
        bars.append({
            "date": d.isoformat(),
            "open": l + 2.0,
            "high": h,
            "low": l,
            "close": c,
            "volume": avg_vol_50d,
        })

    today_date = base_date + datetime.timedelta(days=num_prior_bars)
    bars.append({
        "date": today_date.isoformat(),
        "open": today_low + 1.0,
        "high": today_high,
        "low": today_low,
        "close": today_close,
        "volume": today_vol,
    })
    return bars


def test_positive_cases_ath_and_52w_both_fire():
    """Positive control:
    1) BREAK_ATH breaks above both 52-week high (200) and older multi-year high (180) -> high_type == 'ATH'.
    2) BREAK_52W breaks above 52-week high (200) while older multi-year high is 300 -> high_type == '52W'.
    """
    bars_ath = _build_bars(
        num_prior_bars=300,
        old_peak_high=180.0,
        prior_52w_high=200.0,
        avg_vol_50d=200_000.0,
        today_low=200.0,
        today_high=212.0,
        today_close=210.0,     # >= 200.0 (ATH) and range_pos = 10/12 = 0.8333 >= 0.6667
        today_vol=500_000.0,   # 2.5x >= 2.0x
    )
    res_ath = new_high_detector.evaluate_bars("BREAK_ATH", bars_ath)
    assert res_ath["outcome"] == "candidate"
    cand_ath = res_ath["candidate"]
    assert cand_ath["symbol"] == "BREAK_ATH"
    assert cand_ath["high_type"] == "ATH"
    assert cand_ath["vol_ratio"] == 2.5
    assert cand_ath["avg_vol_50d"] == 200_000.0
    assert cand_ath["range_pos"] >= 2.0 / 3.0

    bars_52w = _build_bars(
        num_prior_bars=300,
        old_peak_high=300.0,   # Older ATH > 1 yr ago is 300.0
        prior_52w_high=200.0,  # 52-week high is 200.0
        avg_vol_50d=200_000.0,
        today_low=200.0,
        today_high=212.0,
        today_close=210.0,     # >= 200.0 (52W) but < 300.0 (ATH)
        today_vol=500_000.0,
    )
    res_52w = new_high_detector.evaluate_bars("BREAK_52W", bars_52w)
    assert res_52w["outcome"] == "candidate"
    cand_52w = res_52w["candidate"]
    assert cand_52w["symbol"] == "BREAK_52W"
    assert cand_52w["high_type"] == "52W"
    assert cand_52w["prior_52w_high"] == 200.0
    assert cand_52w["prior_ath"] == 300.0


def test_negative_control_low_volume():
    """Negative control 1 (low volume):
    - Close IS at ATH/52w high (210 >= 200)
    - Close IS in top third (range_pos = 0.8333)
    - 50d avg vol IS >= 1e5 (200,000)
    - Volume is 250,000 (1.25x < 2.0x) -> MUST NOT FIRE (filter_reason='low_volume')."""
    bars = _build_bars(
        avg_vol_50d=200_000.0,
        today_low=200.0,
        today_high=212.0,
        today_close=210.0,
        today_vol=250_000.0,  # 1.25x < 2.0x
    )
    res = new_high_detector.evaluate_bars("NEG_LOW_VOL", bars)
    assert res["outcome"] == "filtered"
    assert res["candidate"] is None
    assert res["filter_reason"] == "low_volume"


def test_negative_control_close_in_bottom_third():
    """Negative control 2 (close in bottom third of day's range):
    - Close IS at new high (today_close=202.0 >= prior_52w_high=200.0)
    - Volume IS >= 2.0x 50d avg (500,000 / 200,000 = 2.5x)
    - 50d avg vol IS >= 1e5 (200,000)
    - Day range is [200.0, 215.0], close=202.0 -> range_pos = 2/15 = 0.1333 (bottom third)
    -> MUST NOT FIRE (filter_reason='close_not_in_top_third')."""
    bars = _build_bars(
        prior_52w_high=200.0,
        old_peak_high=180.0,
        avg_vol_50d=200_000.0,
        today_low=200.0,
        today_high=215.0,
        today_close=202.0,  # >= 200.0 new high, but in bottom third of [200, 215]
        today_vol=500_000.0,
    )
    res = new_high_detector.evaluate_bars("NEG_BOT_THIRD", bars)
    assert res["outcome"] == "filtered"
    assert res["candidate"] is None
    assert res["filter_reason"] == "close_not_in_top_third"


def test_negative_control_low_average_volume():
    """Negative control 3 (low average volume < 1e5):
    - Close IS at new high (210 >= 200)
    - Close IS in top third (range_pos = 0.8333)
    - Volume IS >= 2.0x 50d avg (120,000 / 40,000 = 3.0x)
    - 50d avg vol is 40,000 (< 100,000) -> MUST NOT FIRE (filter_reason='low_average_volume')."""
    bars = _build_bars(
        avg_vol_50d=40_000.0,
        today_low=200.0,
        today_high=212.0,
        today_close=210.0,
        today_vol=120_000.0,  # 3.0x spike, but base avg_vol < 1e5
    )
    res = new_high_detector.evaluate_bars("NEG_LOW_AVG_VOL", bars)
    assert res["outcome"] == "filtered"
    assert res["candidate"] is None
    assert res["filter_reason"] == "low_average_volume"


def test_negative_control_not_a_new_high():
    """Negative control 4 (not a new high):
    - 50d avg vol IS >= 1e5 (200,000)
    - Volume IS >= 2.0x 50d avg (500,000 / 200,000 = 2.5x)
    - Close IS in top third of day's range ([180, 192], close=190 -> range_pos = 10/12 = 0.8333)
    - Close (190.0) < prior_52w_high (200.0) -> MUST NOT FIRE (filter_reason='not_new_high')."""
    bars = _build_bars(
        prior_52w_high=200.0,
        old_peak_high=220.0,
        avg_vol_50d=200_000.0,
        today_low=180.0,
        today_high=192.0,
        today_close=190.0,
        today_vol=500_000.0,
    )
    res = new_high_detector.evaluate_bars("NEG_NOT_HIGH", bars)
    assert res["outcome"] == "filtered"
    assert res["candidate"] is None
    assert res["filter_reason"] == "not_new_high"


def test_end_to_end_detector_writes_own_schema_and_status_artifact(tmp_path):
    """Run detector over combined fixture (2 positive + 4 negative controls), verify CSV + JSON status."""
    fixtures = {
        "POS_ATH": _build_bars(
            old_peak_high=180.0, prior_52w_high=200.0, avg_vol_50d=200_000.0,
            today_low=200.0, today_high=212.0, today_close=210.0, today_vol=500_000.0,
        ),
        "POS_52W": _build_bars(
            old_peak_high=300.0, prior_52w_high=200.0, avg_vol_50d=200_000.0,
            today_low=200.0, today_high=212.0, today_close=210.0, today_vol=500_000.0,
        ),
        "NEG_LOW_VOL": _build_bars(
            avg_vol_50d=200_000.0, today_low=200.0, today_high=212.0,
            today_close=210.0, today_vol=250_000.0,
        ),
        "NEG_BOT_THIRD": _build_bars(
            prior_52w_high=200.0, old_peak_high=180.0, avg_vol_50d=200_000.0,
            today_low=200.0, today_high=215.0, today_close=202.0, today_vol=500_000.0,
        ),
        "NEG_LOW_AVG_VOL": _build_bars(
            avg_vol_50d=40_000.0, today_low=200.0, today_high=212.0,
            today_close=210.0, today_vol=120_000.0,
        ),
        "NEG_NOT_HIGH": _build_bars(
            prior_52w_high=200.0, old_peak_high=220.0, avg_vol_50d=200_000.0,
            today_low=180.0, today_high=192.0, today_close=190.0, today_vol=500_000.0,
        ),
    }

    alerts_csv = tmp_path / "new_high_alerts.csv"
    status_json = tmp_path / "new_high_alerts_status.json"
    status_jsonl = tmp_path / "new_high_alerts_status.jsonl"

    status = new_high_detector.run_detector(
        bars_by_symbol=fixtures,
        alerts_path=str(alerts_csv),
        status_path=str(status_json),
        status_jsonl_path=str(status_jsonl),
        min_symbols_ok=6,
    )

    assert status["verdict"] == "OK"
    assert status["symbols_total"] == 6
    assert status["ok"] == 6
    assert status["error"] == 0
    assert status["candidates"] == 2
    assert status["filtered_by_reason"] == {
        "insufficient_history": 0,
        "low_average_volume": 1,
        "not_new_high": 1,
        "low_volume": 1,
        "close_not_in_top_third": 1,
    }
    assert status["risk_sub_limit_rs"] == 15000.0
    assert status["max_positions_sub_limit"] == 1

    with open(alerts_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    # Must NOT use cup watchlist format (symbol,rim,trigger,qty)
    assert fieldnames != ["symbol", "rim", "trigger", "qty"]
    assert "rim" not in fieldnames
    assert "trigger" not in fieldnames
    assert fieldnames == new_high_detector.ALERT_FIELDS
    assert [r["symbol"] for r in rows] == ["POS_ATH", "POS_52W"]
    assert rows[0]["high_type"] == "ATH"
    assert rows[1]["high_type"] == "52W"

    # Verify JSON status artifact on disk + appended JSONL line
    disk_status = json.loads(status_json.read_text(encoding="utf-8"))
    assert disk_status == status
    jsonl_lines = status_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(jsonl_lines) == 1
    assert json.loads(jsonl_lines[0]) == status


def test_hard_boundaries_no_order_path_or_yfinance_imports():
    """Verify AST of new_high_detector.py has zero imports from cup order path or yfinance,
    and ReadOnlyFyersDataAdapter blocks all order methods."""
    src_path = os.path.join(ROOT, "new_high_detector.py")
    with open(src_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=src_path)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module.split(".")[0])

    forbidden = {
        "yfinance",
        "bot",
        "broker",
        "cup_sl_engine",
        "run",
        "watchlist_sync",
        "ledger",
    }
    assert imported_modules.isdisjoint(forbidden), (
        f"Forbidden imports found in new_high_detector.py: {imported_modules & forbidden}"
    )

    # Verify ReadOnlyFyersDataAdapter blocks every order method
    class _DummyClient:
        def quotes(self, symbols):
            return {"s": "ok", "d": []}

    adapter = new_high_detector.ReadOnlyFyersDataAdapter(_DummyClient())
    for method_name in (
        "place_buy_stop",
        "place_buy_gtt",
        "place_sell_stop",
        "place_market_sell",
        "modify_order",
        "cancel_order",
    ):
        with pytest.raises(PermissionError, match="alert-only"):
            getattr(adapter, method_name)("TEST", 1, 100.0)

    # Verify explicit error if client only has .quotes() and no .history() / .fy.history()
    with pytest.raises(FyersError, match="Explicit Fyers capability error"):
        adapter.fetch_settled_daily_bars("RELIANCE")
