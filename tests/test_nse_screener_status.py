"""
Tests for Fix 1: nse_screener.py per-symbol outcome classification,
machine-readable JSON status artifact, verdict logic (OK / DEGRADED / FAILED),
and non-zero exit code on FAILED.

Specifically verifies:
  1. Clean zero-candidate run (2,364 symbols, 0 errors, 0 candidates) -> verdict=OK, exit=0,
     all 2,364 symbols accounted for in filtered_by_reason.
  2. All-fail zero-candidate run (2,364 symbols, 2,364 errors, 0 candidates) -> verdict=FAILED,
     exit=1, all 2,364 symbols accounted for in by_error_type (distinguishes clean 0-cand
     from all-fail 0-cand).
  3. Degraded run (failure rate between 2% and 5%, e.g. 3.17% with mixed auth/http/parse/empty
     errors and symbols_ok >= 2000) -> verdict=DEGRADED, exit=0.
  4. Rate-limit style error burst (> 5% HTTP 429 errors) -> verdict=FAILED, exit=1.
  5. Sub-2000 ok universe (e.g. 1,500 symbols, 0 errors) -> verdict=FAILED, exit=1.
  6. Candidates CSV format stays unchanged for existing consumers (watchlist_sync).
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
from pathlib import Path

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT in sys.path:
    sys.path.remove(ROOT)
sys.path.insert(0, ROOT)
if "nse_screener" in sys.modules and os.path.dirname(os.path.abspath(sys.modules["nse_screener"].__file__)) != ROOT:
    del sys.modules["nse_screener"]

import nse_screener
import watchlist_sync


DATES_100 = pd.date_range("2020-01-01", periods=100, freq="D")
DATES_RECENT = pd.date_range("2026-01-01", periods=60, freq="D")
DATES_SHORT = pd.date_range("2026-09-01", periods=5, freq="D")


def _df_candidate(ath: float = 419.5, cur: float = 415.0, vol: float = 250_000.0) -> pd.DataFrame:
    highs = [100.0] * len(DATES_100)
    highs[0] = ath  # 2020-01-01 (> 3 years old)
    closes = [cur] * len(DATES_100)
    vols = [vol] * len(DATES_100)
    return pd.DataFrame({"High": highs, "Close": closes, "Volume": vols}, index=DATES_100)


def _df_filtered_below_queue() -> pd.DataFrame:
    # Old ATH=400, current=250 (-37.5% < -5% queue threshold)
    return _df_candidate(ath=400.0, cur=250.0, vol=250_000.0)


def _df_filtered_low_vol() -> pd.DataFrame:
    return _df_candidate(ath=400.0, cur=395.0, vol=25_000.0)


def _df_filtered_recent_ath() -> pd.DataFrame:
    highs = [100.0] * len(DATES_RECENT)
    highs[-5] = 400.0  # ATH in 2026 (< 3 years old)
    closes = [395.0] * len(DATES_RECENT)
    vols = [250_000.0] * len(DATES_RECENT)
    return pd.DataFrame({"High": highs, "Close": closes, "Volume": vols}, index=DATES_RECENT)


def _df_filtered_short_history() -> pd.DataFrame:
    return pd.DataFrame(
        {"High": [100.0] * 5, "Close": [98.0] * 5, "Volume": [200_000.0] * 5},
        index=DATES_SHORT,
    )


def test_clean_zero_candidate_run_vs_all_fail_run_distinguished(tmp_path, monkeypatch, capsys):
    """Core regression test: a 2,364-symbol clean 0-candidate run MUST be OK (exit 0),
    whereas a 2,364-symbol all-fail 0-candidate run MUST be FAILED (exit 1).
    Both produce 0 candidates, proving the status artifact and exit code distinguish them."""
    universe = [f"SYM{i:04d}" for i in range(2364)]
    monkeypatch.setattr(nse_screener, "fetch_nse_symbols", lambda max_n=None: universe)
    monkeypatch.setattr(nse_screener.time, "sleep", lambda _s: None)

    # --- Part A: Clean 0-candidate run (all 2,364 symbols fetch fine, filtered out by strategy rules) ---
    cand_clean = tmp_path / "clean_candidates.csv"
    status_clean = tmp_path / "clean_status.json"
    monkeypatch.setattr(nse_screener, "CAND", str(cand_clean))
    monkeypatch.setattr(nse_screener, "STATUS", str(status_clean))

    df_below = _df_filtered_below_queue()
    df_low_v = _df_filtered_low_vol()
    df_rec = _df_filtered_recent_ath()
    df_short = _df_filtered_short_history()

    class _CleanTicker:
        def __init__(self, ticker_str: str):
            idx = int(ticker_str.split(".")[0].replace("SYM", ""))
            self.idx = idx

        def history(self, period="max", auto_adjust=True):
            mod = self.idx % 4
            if mod == 0:
                return df_below
            if mod == 1:
                return df_low_v
            if mod == 2:
                return df_rec
            return df_short

    monkeypatch.setattr(nse_screener.yf, "Ticker", _CleanTicker)

    rc_clean = nse_screener.main(["scan"])
    out_clean = capsys.readouterr().out
    art_clean = json.loads(status_clean.read_text(encoding="utf-8"))

    assert rc_clean == 0
    assert art_clean["verdict"] == "OK"
    assert art_clean["symbols_total"] == 2364
    assert art_clean["ok"] == 2364
    assert art_clean["error"] == 0
    assert art_clean["candidates"] == 0
    assert sum(art_clean["by_error_type"].values()) == 0
    assert art_clean["filtered_by_reason"] == {
        "insufficient_history": 591,
        "low_volume": 591,
        "ath_too_recent": 591,
        "below_queue_threshold": 591,
    }
    assert sum(art_clean["filtered_by_reason"].values()) == 2364
    assert "verdict=OK" in out_clean
    assert not any(line.strip().lower() == "ok" for line in out_clean.splitlines())

    # --- Part B: All-fail 0-candidate run (all 2,364 symbols fail / return empty) ---
    cand_fail = tmp_path / "fail_candidates.csv"
    status_fail = tmp_path / "fail_status.json"
    monkeypatch.setattr(nse_screener, "CAND", str(cand_fail))
    monkeypatch.setattr(nse_screener, "STATUS", str(status_fail))

    empty_df = pd.DataFrame()
    bad_col_df = pd.DataFrame({"Open": [1.0, 2.0]})

    class _AllFailTicker:
        def __init__(self, ticker_str: str):
            self.idx = int(ticker_str.split(".")[0].replace("SYM", ""))

        def history(self, period="max", auto_adjust=True):
            mod = self.idx % 4
            if mod == 0:
                raise urllib.error.HTTPError(
                    url="https://query2.finance.yahoo.com",
                    code=401,
                    msg="UnauthorizedInvalidCrumb",
                    hdrs=None,
                    fp=None,
                )
            if mod == 1:
                raise urllib.error.HTTPError(
                    url="https://query2.finance.yahoo.com",
                    code=429,
                    msg="Too Many Requests (rate limit)",
                    hdrs=None,
                    fp=None,
                )
            if mod == 2:
                return bad_col_df  # missing High/Close/Volume -> parse
            return empty_df        # empty DataFrame -> empty

    monkeypatch.setattr(nse_screener.yf, "Ticker", _AllFailTicker)

    rc_fail = nse_screener.main(["scan"])
    out_fail = capsys.readouterr().out
    art_fail = json.loads(status_fail.read_text(encoding="utf-8"))

    assert rc_fail == 1
    assert art_fail["verdict"] == "FAILED"
    assert art_fail["symbols_total"] == 2364
    assert art_fail["ok"] == 0
    assert art_fail["error"] == 2364
    assert art_fail["candidates"] == 0
    assert art_fail["by_error_type"] == {
        "auth": 591,
        "http": 591,
        "parse": 591,
        "empty": 591,
    }
    assert sum(art_fail["filtered_by_reason"].values()) == 0
    assert "verdict=FAILED" in out_fail


def test_degraded_run_between_2pct_and_5pct_failure_rate(tmp_path, monkeypatch, capsys):
    """When symbols_ok >= 2000 and 2% < error/symbols_total <= 5%, verdict is DEGRADED and exit is 0."""
    universe = [f"SYM{i:04d}" for i in range(2364)]
    monkeypatch.setattr(nse_screener, "fetch_nse_symbols", lambda max_n=None: universe)
    monkeypatch.setattr(nse_screener.time, "sleep", lambda _s: None)

    cand_path = tmp_path / "degraded_candidates.csv"
    status_path = tmp_path / "degraded_status.json"
    monkeypatch.setattr(nse_screener, "CAND", str(cand_path))
    monkeypatch.setattr(nse_screener, "STATUS", str(status_path))

    df_cand = _df_candidate(ath=419.5, cur=415.0, vol=250_000.0)
    df_below = _df_filtered_below_queue()
    empty_df = pd.DataFrame()

    # Inject 80 failures out of 2,364 symbols (3.38% failure rate -> DEGRADED; ok = 2,284 >= 2,000)
    class _DegradedTicker:
        def __init__(self, ticker_str: str):
            self.idx = int(ticker_str.split(".")[0].replace("SYM", ""))

        def history(self, period="max", auto_adjust=True):
            if self.idx < 20:
                raise PermissionError("403 Forbidden: invalid crumb auth")
            if self.idx < 50:
                raise RuntimeError("HTTP 429 Too Many Requests: rate limit exceeded")
            if self.idx < 65:
                raise ValueError("JSONDecodeError: Expecting value: line 1 column 1")
            if self.idx < 80:
                return empty_df
            if self.idx < 85:
                return df_cand  # 5 qualifying candidates
            return df_below

    monkeypatch.setattr(nse_screener.yf, "Ticker", _DegradedTicker)

    rc = nse_screener.main(["scan"])
    out = capsys.readouterr().out
    art = json.loads(status_path.read_text(encoding="utf-8"))

    assert rc == 0
    assert art["verdict"] == "DEGRADED"
    assert art["symbols_total"] == 2364
    assert art["ok"] == 2284
    assert art["error"] == 80
    assert 0.02 < art["error"] / art["symbols_total"] <= 0.05
    assert art["by_error_type"] == {
        "auth": 20,
        "http": 30,
        "parse": 15,
        "empty": 15,
    }
    assert art["candidates"] == 5
    assert art["filtered_by_reason"]["below_queue_threshold"] == 2279
    assert "verdict=DEGRADED" in out

    # Confirm candidates CSV format is unchanged and consumable by watchlist_sync
    cands = watchlist_sync._read_screener_candidates(str(cand_path))
    assert len(cands) == 5
    assert cands[0]["symbol"] == "SYM0080"
    assert cands[0]["rim"] == 419.5
    assert cands[0]["trigger"] == 436.3


def test_rate_limit_subset_over_5pct_fails_with_nonzero_exit(tmp_path, monkeypatch, capsys):
    """Injecting >5% rate-limit HTTP 429 failures (e.g. 180 / 2364 = 7.61%) yields FAILED and exit 1."""
    universe = [f"SYM{i:04d}" for i in range(2364)]
    monkeypatch.setattr(nse_screener, "fetch_nse_symbols", lambda max_n=None: universe)
    monkeypatch.setattr(nse_screener.time, "sleep", lambda _s: None)

    cand_path = tmp_path / "ratelimit_candidates.csv"
    status_path = tmp_path / "ratelimit_status.json"
    monkeypatch.setattr(nse_screener, "CAND", str(cand_path))
    monkeypatch.setattr(nse_screener, "STATUS", str(status_path))

    df_below = _df_filtered_below_queue()

    class _RateLimitTicker:
        def __init__(self, ticker_str: str):
            self.idx = int(ticker_str.split(".")[0].replace("SYM", ""))

        def history(self, period="max", auto_adjust=True):
            if self.idx < 180:
                raise urllib.error.HTTPError(
                    url="https://query2.finance.yahoo.com",
                    code=429,
                    msg="Too Many Requests",
                    hdrs=None,
                    fp=None,
                )
            return df_below

    monkeypatch.setattr(nse_screener.yf, "Ticker", _RateLimitTicker)

    rc = nse_screener.main(["scan"])
    out = capsys.readouterr().out
    art = json.loads(status_path.read_text(encoding="utf-8"))

    assert rc == 1
    assert art["verdict"] == "FAILED"
    assert art["symbols_total"] == 2364
    assert art["ok"] == 2184
    assert art["error"] == 180
    assert art["by_error_type"]["http"] == 180
    assert "verdict=FAILED" in out


def test_symbols_ok_below_2000_fails_even_with_zero_errors(tmp_path, monkeypatch):
    """Even if error == 0, if symbols_ok < 2000 (e.g. truncated universe of 500), verdict is FAILED."""
    universe = [f"SYM{i:04d}" for i in range(500)]
    monkeypatch.setattr(nse_screener, "fetch_nse_symbols", lambda max_n=None: universe)
    monkeypatch.setattr(nse_screener.time, "sleep", lambda _s: None)

    cand_path = tmp_path / "short_univ_candidates.csv"
    status_path = tmp_path / "short_univ_status.json"
    monkeypatch.setattr(nse_screener, "CAND", str(cand_path))
    monkeypatch.setattr(nse_screener, "STATUS", str(status_path))

    df_below = _df_filtered_below_queue()
    monkeypatch.setattr(
        nse_screener.yf,
        "Ticker",
        lambda _s: type("_T", (), {"history": lambda self, **kw: df_below})(),
    )

    rc = nse_screener.main(["scan"])
    art = json.loads(status_path.read_text(encoding="utf-8"))

    assert rc == 1
    assert art["verdict"] == "FAILED"
    assert art["ok"] == 500
    assert art["error"] == 0
