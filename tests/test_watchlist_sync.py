"""
Tests for watchlist_sync.py and the nse_screener.py liquidity filter.

Covers:
  a. a watchlist.csv WITHOUT a trailing newline is handled correctly
  b. dedupe - an already-present symbol is not added twice (case-insensitive)
  c. budget refusal - an addition that would breach CAPITAL_BUDGET is refused
  d. run.load_watchlist() successfully loads whatever watchlist_sync.py writes
  e. --dry-run writes nothing
  + max-watchlist cap, backup + self-validation rollback, and screener liquidity filter
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Ensure optional runtime dependencies are stubbed if running outside .venv
for _mod in ("fyers_apiv3", "requests"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            _m = types.ModuleType(_mod)
            if _mod == "fyers_apiv3":
                _m.fyersModel = types.SimpleNamespace(FyersModel=object)
            sys.modules[_mod] = _m

import run
import watchlist_sync


CANDIDATES_HEADER = (
    "symbol,ath,ath_date,buy_price,sl_price,qty,capital,current,pct_from_ath\n"
)


def _cand_row(symbol="NAZARA", ath=419.5, buy_price=436.28, qty=28, current=405.2):
    sl_price = round(buy_price * 0.94, 2)
    capital = round(buy_price * qty, 2)
    pct = round((current - ath) / ath * 100, 2)
    return (
        f"{symbol},{ath},2021-10-11,{buy_price},{sl_price},"
        f"{qty},{capital},{current},{pct}\n"
    )


def test_no_trailing_newline_handled_correctly(tmp_path):
    """(a) + (d): A watchlist.csv WITHOUT a trailing newline is rewritten
    atomically with newline-terminated lines and loads cleanly via run.load_watchlist()."""
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"
    env_path = tmp_path / ".env"

    # Deliberately omit trailing newline (exact regression scenario)
    raw_initial = "symbol,rim,trigger,qty\nLALPATHLAB,2045.0,2126.8,5"
    assert not raw_initial.endswith("\n")
    wl_path.write_bytes(raw_initial.encode("utf-8"))
    cand_path.write_text(CANDIDATES_HEADER + _cand_row("NAZARA", 419.5, 436.28, 28), encoding="utf-8")
    env_path.write_text("CAPITAL_BUDGET=50000\n", encoding="utf-8")

    rc = watchlist_sync.main([
        "--watchlist", str(wl_path),
        "--candidates", str(cand_path),
        "--env", str(env_path),
    ])
    assert rc == 0

    new_text = wl_path.read_text(encoding="utf-8")
    assert "5NAZARA" not in new_text, "blind-append bug must never recur"
    assert new_text.endswith("\n"), "written watchlist.csv must be newline-terminated"
    lines = new_text.splitlines(keepends=True)
    assert all(ln.endswith("\n") for ln in lines)

    # (d) run.load_watchlist() successfully loads whatever watchlist_sync.py writes
    loaded = run.load_watchlist(str(wl_path))
    assert loaded == [
        {"symbol": "LALPATHLAB", "rim": 2045.0, "trigger": 2126.8, "qty": 5},
        {"symbol": "NAZARA", "rim": 419.5, "trigger": 436.28, "qty": 28},
    ]

    # Verify backup was created with original no-trailing-newline content
    baks = glob.glob(str(wl_path) + ".bak-*")
    assert len(baks) == 1
    assert Path(baks[0]).read_bytes() == raw_initial.encode("utf-8")


def test_dedupe_already_present_symbol_not_added_twice(tmp_path):
    """(b) + (d): An already-present symbol is not added twice (case-insensitive)."""
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"

    wl_path.write_text(
        "symbol,rim,trigger,qty\n"
        "LALPATHLAB,2045.0,2126.8,5\n"
        "NAZARA,419.5,436.28,28\n",
        encoding="utf-8",
    )
    cand_path.write_text(
        CANDIDATES_HEADER
        + _cand_row("nazara", 419.5, 436.28, 28)
        + _cand_row("NAZARA", 419.5, 436.28, 28)
        + _cand_row("lAlPaThLaB", 2045.0, 2126.8, 5),
        encoding="utf-8",
    )

    res = watchlist_sync.sync_watchlist(
        watchlist_path=str(wl_path),
        candidates_path=str(cand_path),
        capital_budget=50000,
    )
    assert res["added"] == []
    assert len(res["skipped"]) == 3
    assert all("dedupe" in s["reason"].lower() for s in res["skipped"])

    loaded = run.load_watchlist(str(wl_path))
    assert [r["symbol"] for r in loaded] == ["LALPATHLAB", "NAZARA"]


def test_budget_refusal_and_arithmetic_reported(tmp_path, capsys):
    """(c) + (d): An addition that would breach CAPITAL_BUDGET is refused and reports arithmetic."""
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"
    env_path = tmp_path / ".env"

    # Existing notional: LALPATHLAB (2126.8 * 5 = 10634.0) + NAZARA (436.28 * 28 = 12215.84) = 22849.84
    wl_path.write_text(
        "symbol,rim,trigger,qty\n"
        "LALPATHLAB,2045.0,2126.8,5\n"
        "NAZARA,419.5,436.28,28\n",
        encoding="utf-8",
    )
    # Candidate BIGCO: trigger=1000.0 * qty=30 = 30000.0 -> 22849.84 + 30000.00 = 52849.84 > 50000
    cand_path.write_text(
        CANDIDATES_HEADER + _cand_row("BIGCO", 961.54, 1000.0, 30),
        encoding="utf-8",
    )
    env_path.write_text("CAPITAL_BUDGET=50000\n", encoding="utf-8")

    res = watchlist_sync.sync_watchlist(
        watchlist_path=str(wl_path),
        candidates_path=str(cand_path),
        env_path=str(env_path),
    )
    out = capsys.readouterr().out

    assert res["added"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["symbol"] == "BIGCO"
    reason = res["skipped"][0]["reason"]
    assert "22849.84" in reason
    assert "30000.00" in reason
    assert "52849.84" in reason
    assert "50000.00" in reason
    assert "52849.84" in out

    loaded = run.load_watchlist(str(wl_path))
    assert [r["symbol"] for r in loaded] == ["LALPATHLAB", "NAZARA"]


def test_dry_run_writes_nothing(tmp_path, capsys):
    """(e): --dry-run prints what would change and writes nothing (no file change, no backup)."""
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"
    env_path = tmp_path / ".env"

    raw_initial = b"symbol,rim,trigger,qty\nLALPATHLAB,2045.0,2126.8,5"
    wl_path.write_bytes(raw_initial)
    cand_path.write_text(
        CANDIDATES_HEADER + _cand_row("NAZARA", 419.5, 436.28, 28),
        encoding="utf-8",
    )
    env_path.write_text("CAPITAL_BUDGET=50000\n", encoding="utf-8")

    before_files = sorted(os.listdir(tmp_path))

    rc = watchlist_sync.main([
        "--dry-run",
        "--watchlist", str(wl_path),
        "--candidates", str(cand_path),
        "--env", str(env_path),
    ])
    out = capsys.readouterr().out

    assert rc == 0
    assert "NAZARA" in out
    assert "DRY-RUN" in out
    # Exact byte-for-byte preservation (still lacks trailing newline, NAZARA not written)
    assert wl_path.read_bytes() == raw_initial
    # No backup or temp files created
    assert sorted(os.listdir(tmp_path)) == before_files


def test_max_watchlist_cap_enforced(tmp_path):
    """Rule 2: Refuse to grow the list beyond MAX_WATCHLIST."""
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"

    wl_path.write_text(
        "symbol,rim,trigger,qty\n"
        "LALPATHLAB,2045.0,2126.8,5\n"
        "NAZARA,419.5,436.28,28\n",
        encoding="utf-8",
    )
    cand_path.write_text(
        CANDIDATES_HEADER + _cand_row("SMALLCO", 100.0, 104.0, 10),
        encoding="utf-8",
    )

    rc = watchlist_sync.main([
        "--max-watchlist", "2",
        "--watchlist", str(wl_path),
        "--candidates", str(cand_path),
        "--capital-budget", "50000",
    ])
    assert rc == 0
    loaded = run.load_watchlist(str(wl_path))
    assert len(loaded) == 2
    assert [r["symbol"] for r in loaded] == ["LALPATHLAB", "NAZARA"]


def test_self_validate_restores_backup_on_load_error(tmp_path, monkeypatch):
    """Rule 6: If run.load_watchlist() raises after write, restore backup and exit non-zero."""
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"

    original = "symbol,rim,trigger,qty\nLALPATHLAB,2045.0,2126.8,5\n"
    wl_path.write_text(original, encoding="utf-8")
    cand_path.write_text(
        CANDIDATES_HEADER + _cand_row("NAZARA", 419.5, 436.28, 28),
        encoding="utf-8",
    )

    def _boom(path="watchlist.csv", allow_t2t=False):
        raise ValueError("simulated load_watchlist failure")

    monkeypatch.setattr(run, "load_watchlist", _boom)

    rc = watchlist_sync.main([
        "--watchlist", str(wl_path),
        "--candidates", str(cand_path),
        "--capital-budget", "50000",
    ])
    assert rc != 0
    assert wl_path.read_text(encoding="utf-8") == original


def test_screener_liquidity_filter(monkeypatch):
    """Deliverable 2: nse_screener rejects 60-day average volume < MIN_AVG_VOLUME (100000)."""
    try:
        import pandas as pd
        import nse_screener
    except ImportError:
        return  # skipped only if pandas/yfinance unavailable outside .venv

    assert nse_screener.MIN_AVG_VOLUME == 100000
    assert nse_screener.ATH_MIN_YEARS == 3
    assert nse_screener.QUEUE_PCT == 5.0
    assert nse_screener.BREAK_PCT == 2.0
    assert nse_screener.SL_PCT == 6.0 / 100

    dates = pd.date_range("2020-01-01", periods=100, freq="D")

    def _make_df(vol: float) -> pd.DataFrame:
        highs = [100.0] * len(dates)
        highs[0] = 200.0  # ATH in 2020 (> 3 years ago)
        closes = [195.0] * len(dates)  # -2.5% from ATH
        vols = [vol] * len(dates)
        return pd.DataFrame({"High": highs, "Close": closes, "Volume": vols}, index=dates)

    class _FakeTicker:
        def __init__(self, vol):
            self._vol = vol

        def history(self, period="max", auto_adjust=True):
            return _make_df(self._vol)

    monkeypatch.setattr(nse_screener.yf, "Ticker", lambda _s: _FakeTicker(50000))
    assert nse_screener.screen_symbol("THINCO") is None

    monkeypatch.setattr(nse_screener.yf, "Ticker", lambda _s: _FakeTicker(150000))
    res = nse_screener.screen_symbol("LIQUIDCO")
    assert res is not None
    assert res["symbol"] == "LIQUIDCO"


if __name__ == "__main__":
    try:
        import pytest
        sys.exit(pytest.main([__file__, "-v"]))
    except ImportError:
        import contextlib
        import io

        class _SimpleCapsys:
            def __init__(self, buf):
                self._buf = buf

            def readouterr(self):
                val = self._buf.getvalue()
                self._buf.seek(0)
                self._buf.truncate(0)
                return types.SimpleNamespace(out=val, err="")

        class _SimpleMonkeypatch:
            def __init__(self):
                self._patches = []

            def setattr(self, obj, name, value):
                self._patches.append((obj, name, getattr(obj, name)))
                setattr(obj, name, value)

            def undo(self):
                for obj, name, orig in reversed(self._patches):
                    setattr(obj, name, orig)
                self._patches.clear()

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for idx, fn in enumerate([
                test_no_trailing_newline_handled_correctly,
                test_dedupe_already_present_symbol_not_added_twice,
                test_budget_refusal_and_arithmetic_reported,
                test_dry_run_writes_nothing,
                test_max_watchlist_cap_enforced,
                test_self_validate_restores_backup_on_load_error,
                test_screener_liquidity_filter,
            ]):
                sub = base / f"case_{idx}"
                sub.mkdir()
                buf = io.StringIO()
                cap = _SimpleCapsys(buf)
                mp = _SimpleMonkeypatch()
                try:
                    with contextlib.redirect_stdout(buf):
                        code = fn.__code__
                        args = []
                        for arg_name in code.co_varnames[:code.co_argcount]:
                            if arg_name == "tmp_path":
                                args.append(sub)
                            elif arg_name == "capsys":
                                args.append(cap)
                            elif arg_name == "monkeypatch":
                                args.append(mp)
                        fn(*args)
                finally:
                    mp.undo()
                print(f"PASS {fn.__name__}")
            print("All 7 tests passed.")

