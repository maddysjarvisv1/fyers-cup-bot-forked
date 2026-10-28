# Handoff — Task 182: Nightly Screener Loud-Failure Verdict & Separate Alert-Only New-High Detector

- **Date:** 2026-10-28
- **Branch:** `eng/task-182`
- **Worktree:** `/opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.claude/worktrees/eng-task-182`
- **Graphify Verification:** `graphify-out/graph.json` initially had `built_at_commit: d2c5581ea7eb532837b4bda32e4d018772c85085` while repo `HEAD` was `0802157`. Ran `graphify update . --force` before inspecting source files (rebuilt against `080215747c814a6b83b12ea5096c18dd445fbfa7`, then refreshed after adding Task 182 changes).

---

## 1. Graphify Evidence & Code Citations

- **`nse_screener.py` (`nse_screener.py:L1`, community `scanner.py`):**
  - `fetch_nse_symbols()` (`nse_screener.py:L46`), `calc_qty()` (`nse_screener.py:L61`), `screen_symbol()` (`nse_screener.py:L66`), `scan()` (`nse_screener.py:L97`), `list_cands()` (`nse_screener.py:L120`).
  - Previously wrapped all of `screen_symbol()` in `except Exception: return None` and returned `None` on empty DataFrames or filtered symbols alike, so `scan()` could not distinguish an all-fail/empty run from a clean 0-candidate scan.
- **`FyersClient` (`fyers_client.py:L137`, community `FyersClient`):**
  - `.from_files()` (`fyers_client.py:L205`), `.quotes()` (`fyers_client.py:L218`), `.ltps()` (`fyers_client.py:L223`), `nse()` (`fyers_client.py:L85`), `plain()` (`fyers_client.py:L95`), `FyersError` (`fyers_client.py:L113`).
  - Order-placement methods on `FyersClient` (`place_buy_stop` at `fyers_client.py:L246`, `place_buy_gtt` at `fyers_client.py:L302`, `place_sell_stop` at `fyers_client.py:L353`, `place_market_sell` at `fyers_client.py:L400`, `modify_order` at `fyers_client.py:L441`, `cancel_order` at `fyers_client.py:L510`) and the cup bot's order/state path (`CupBot` at `bot.py:L86`, `LiveBroker` at `broker.py:L209`, `PaperBroker` at `broker.py:L60`, `watchlist_sync.py:L1`, `run.py:L1`, `ledger.py:L80`) are never imported or called by `new_high_detector.py`.

---

## 2. Fix 1 — Nightly Cup Screener Loud Failure & Status Artifact (`nse_screener.py`)

### Summary of Changes
1. **Per-symbol outcome & error classification (`classify_error`, `_evaluate_symbol_core`, `evaluate_symbol`):**
   - Classifies every per-symbol failure into `by_error_type` keys `("auth", "http", "parse", "empty")`. Crucially, an empty DataFrame (`h.empty` / `len(h) == 0`) returned by `yfinance` is classified as an `"empty"` error rather than a clean non-candidate.
   - Tracks symbols that fetched and parsed cleanly (`ok`) and were legitimately filtered out in `filtered_by_reason` across `("insufficient_history", "low_volume", "ath_too_recent", "below_queue_threshold")`.
   - Accounting invariant holds on every scan: `symbols_total == ok + error`, `error == sum(by_error_type.values())`, and `ok == sum(filtered_by_reason.values()) + candidates`.
2. **Machine-readable status artifact (`nse_screener_status.json`):**
   - Written atomically on every run with `started_at`, `finished_at`, `symbols_total`, `ok`, `symbols_ok`, `error`, `error_rate`, `by_error_type`, `filtered_by_reason`, `candidates`, and `verdict`.
3. **Verdict & exit code (`compute_verdict`, `main`):**
   - `OK`: `symbols_ok >= 2000` and `error / symbols_total <= 0.02`. Exits `0`.
   - `DEGRADED`: `symbols_ok >= 2000` and `0.02 < error / symbols_total <= 0.05`. Exits `0`.
   - `FAILED`: `symbols_ok < 2000` or `error / symbols_total > 0.05` (or universe fetch failure). Exits `1` (non-zero).
   - Prints explicit summary counts and verdict; never prints a bare `ok`.
   - Preserves `nse_screener_candidates.csv` schema (`symbol,ath,ath_date,buy_price,sl_price,qty,capital,current,pct_from_ath`) and `screen_symbol()` tick-aligned return contract.

### Fix 1 — Real Command Acceptance Output

#### A. Clean Zero-Candidate Run (2,364 symbols, 0 errors -> `verdict=OK`, `EXIT_CODE=0`)
```text
[!] 0 cup/breakout candidates -> clean_cands.csv
[Screener] SUMMARY: verdict=OK | symbols_total=2364 ok=2364 error=0 (0.00%) | by_error_type={'auth': 0, 'http': 0, 'parse': 0, 'empty': 0} | filtered_by_reason={'insufficient_history': 591, 'low_volume': 591, 'ath_too_recent': 591, 'below_queue_threshold': 591} | candidates=0 | status=clean_status.json
EXIT_CODE=0
```
Status artifact (`clean_status.json`):
```json
{
  "started_at": "2026-10-28T08:43:42.244770+05:30",
  "finished_at": "2026-10-28T08:43:42.782342+05:30",
  "symbols_total": 2364,
  "ok": 2364,
  "symbols_ok": 2364,
  "error": 0,
  "error_rate": 0.0,
  "by_error_type": {
    "auth": 0,
    "http": 0,
    "parse": 0,
    "empty": 0
  },
  "filtered_by_reason": {
    "insufficient_history": 591,
    "low_volume": 591,
    "ath_too_recent": 591,
    "below_queue_threshold": 591
  },
  "candidates": 0,
  "verdict": "OK"
}
```

#### B. Degraded Run (2,364 symbols, 80 errors = 3.38% -> `verdict=DEGRADED`, `EXIT_CODE=0`)
```text
[!] 3 cup/breakout candidates -> degraded_cands.csv
   SYM0080      ATH     419.5 2020-01-01  cur     415.0   -1.07%  qty 28
   SYM0081      ATH     419.5 2020-01-01  cur     415.0   -1.07%  qty 28
   SYM0082      ATH     419.5 2020-01-01  cur     415.0   -1.07%  qty 28
[Screener] SUMMARY: verdict=DEGRADED | symbols_total=2364 ok=2284 error=80 (3.38%) | by_error_type={'auth': 20, 'http': 30, 'parse': 15, 'empty': 15} | filtered_by_reason={'insufficient_history': 0, 'low_volume': 0, 'ath_too_recent': 0, 'below_queue_threshold': 2281} | candidates=3 | status=degraded_status.json
EXIT_CODE=0
```
Status artifact (`degraded_status.json`):
```json
{
  "started_at": "2026-10-28T08:43:42.785791+05:30",
  "finished_at": "2026-10-28T08:43:43.459373+05:30",
  "symbols_total": 2364,
  "ok": 2284,
  "symbols_ok": 2284,
  "error": 80,
  "error_rate": 0.033841,
  "by_error_type": {
    "auth": 20,
    "http": 30,
    "parse": 15,
    "empty": 15
  },
  "filtered_by_reason": {
    "insufficient_history": 0,
    "low_volume": 0,
    "ath_too_recent": 0,
    "below_queue_threshold": 2281
  },
  "candidates": 3,
  "verdict": "DEGRADED"
}
```

#### C. All-Fail Run (2,364 symbols, 2,364 errors = 100.00% -> `verdict=FAILED`, `EXIT_CODE=1`)
```text
[!] 0 cup/breakout candidates -> fail_cands.csv
[Screener] SUMMARY: verdict=FAILED | symbols_total=2364 ok=0 error=2364 (100.00%) | by_error_type={'auth': 591, 'http': 591, 'parse': 591, 'empty': 591} | filtered_by_reason={'insufficient_history': 0, 'low_volume': 0, 'ath_too_recent': 0, 'below_queue_threshold': 0} | candidates=0 | status=fail_status.json
EXIT_CODE=1
```
Status artifact (`fail_status.json`):
```json
{
  "started_at": "2026-10-28T08:43:43.461624+05:30",
  "finished_at": "2026-10-28T08:43:43.473273+05:30",
  "symbols_total": 2364,
  "ok": 0,
  "symbols_ok": 0,
  "error": 2364,
  "error_rate": 1.0,
  "by_error_type": {
    "auth": 591,
    "http": 591,
    "parse": 591,
    "empty": 591
  },
  "filtered_by_reason": {
    "insufficient_history": 0,
    "low_volume": 0,
    "ath_too_recent": 0,
    "below_queue_threshold": 0
  },
  "candidates": 0,
  "verdict": "FAILED"
}
```

---

## 3. Fix 2 — Separate Alert-Only New-High + Volume Detector (`new_high_detector.py`)

### Summary of Design & Explicit Fyers Market-Data Capability Note
- **Detection Rule (all 4 clauses required on settled daily data):**
  1. `close` is at a 52-week high (`>= prior_52w_high`) or all-time high (`>= prior_ath`), recording `high_type` as `"ATH"` or `"52W"`.
  2. `volume >= 2.0x` the 50-day average volume.
  3. `close` is in the top third of the day's range (`(close - low) / (high - low) >= 2/3`).
  4. 50-day average volume `>= 1e5` (`100,000`).
- **Risk Sub-Limit Recorded in Header/Comments (Never Enforced by Trading):**
  - `RISK_SUB_LIMIT_RS = 15000.0` (Rs 15,000) and `MAX_POSITIONS_SUB_LIMIT = 1` (at most ONE position), documented for human review only.
- **Explicit Fyers Market-Data Capability Note:**
  - `FyersClient` (`fyers_client.py:L137`) exposes `.quotes(symbols)` (`fyers_client.py:L218`) and `.ltps(symbols)` (`fyers_client.py:L223`), which return single-point current/session quote snapshots only and do **not** provide 50-day historical volume or 52-week/all-time daily OHLCV candles; nor does `FyersClient` define a `.history()` wrapper method.
  - Settled daily candles require the underlying Fyers v3 SDK method `client.fy.history(data)` (`fyers_client.py:L142`, `resolution="1D"`, which Fyers v3 caps at 366 days per call, queried in 365-day chunks by `ReadOnlyFyersDataAdapter.fetch_settled_daily_bars`). If `.history()` / `.fy.history()` is unavailable on a supplied client instance, `new_high_detector.py` raises an explicit `FyersError` and records a loud `FAILED` status rather than silently substituting `yfinance`.

### Fix 2 — Real Command Acceptance Output

#### A. Positive Fixtures (`POS_ATH`, `POS_52W`) & Per-Clause Negative Controls (`NEG_LOW_VOL`, `NEG_BOT_THIRD`, `NEG_LOW_AVG_VOL`, `NEG_NOT_HIGH`)
```text
  POS_ATH          -> outcome=candidate filter_reason=None                   high_type=ATH
  POS_52W          -> outcome=candidate filter_reason=None                   high_type=52W
  NEG_LOW_VOL      -> outcome=filtered  filter_reason=low_volume             high_type=None
  NEG_BOT_THIRD    -> outcome=filtered  filter_reason=close_not_in_top_third high_type=None
  NEG_LOW_AVG_VOL  -> outcome=filtered  filter_reason=low_average_volume     high_type=None
  NEG_NOT_HIGH     -> outcome=filtered  filter_reason=not_new_high           high_type=None
[NewHighDetector] Alert-only scan (risk sub-limit Rs 15,000, max 1 position — human review only, NO orders placed)
   ALERT POS_ATH      2025-10-28 close=   210.00 [ATH] vol=500000 (2.50x 50d avg 200000) range_pos=0.83
   ALERT POS_52W      2025-10-28 close=   210.00 [52W] vol=500000 (2.50x 50d avg 200000) range_pos=0.83
[NewHighDetector] SUMMARY: verdict=OK | symbols_total=6 ok=6 error=0 (0.00%) | by_error_type={'auth': 0, 'http': 0, 'parse': 0, 'empty': 0} | filtered_by_reason={'insufficient_history': 0, 'low_average_volume': 1, 'not_new_high': 1, 'low_volume': 1, 'close_not_in_top_third': 1} | candidates=2 | alerts=new_high_alerts.csv | status=new_high_alerts_status.json
```

Generated `new_high_alerts.csv` (own schema, not cup watchlist format):
```csv
symbol,date,close,day_high,day_low,range_pos,volume,avg_vol_50d,vol_ratio,high_type,prior_52w_high,prior_ath
POS_ATH,2025-10-28,210.0,212.0,200.0,0.8333,500000,200000.0,2.5,ATH,200.0,200.0
POS_52W,2025-10-28,210.0,212.0,200.0,0.8333,500000,200000.0,2.5,52W,200.0,300.0
```

Generated `new_high_alerts_status.json` (and appended to `new_high_alerts_status.jsonl`):
```json
{
  "started_at": "2026-10-28T08:43:43.553545+05:30",
  "finished_at": "2026-10-28T08:43:43.556069+05:30",
  "symbols_total": 6,
  "ok": 6,
  "symbols_ok": 6,
  "error": 0,
  "error_rate": 0.0,
  "by_error_type": {
    "auth": 0,
    "http": 0,
    "parse": 0,
    "empty": 0
  },
  "filtered_by_reason": {
    "insufficient_history": 0,
    "low_average_volume": 1,
    "not_new_high": 1,
    "low_volume": 1,
    "close_not_in_top_third": 1
  },
  "candidates": 2,
  "verdict": "OK",
  "risk_sub_limit_rs": 15000.0,
  "max_positions_sub_limit": 1
}
```

#### B. Proof It Places No Orders (AST Import Audit of `new_high_detector.py`)
```text
  from __future__ import annotations
  import argparse
  import csv
  import datetime
  import io
  import json
  import math
  import os
  import sys
  import tempfile
  import time
  import urllib.error
  import urllib.request
  from fyers_client import FyersClient, FyersError, nse, plain
```
Zero imports of `bot`, `broker`, `cup_sl_engine`, `run`, `watchlist_sync`, `ledger`, or `yfinance`. In addition, `ReadOnlyFyersDataAdapter` explicitly raises `PermissionError` on any attempt to access order-mutating methods (`place_buy_stop`, `place_buy_gtt`, `place_sell_stop`, `place_market_sell`, `modify_order`, `cancel_order`).

---

## 4. Note on Cron / Prompt Line Update (Not Edited in Production)

In the daily 08:00 IST (02:30 UTC) screener cron job (documented in `WALKTHROUGH.md:L24-L25`), the shell invocation and reporting prompt line that currently runs `nse_screener.py scan` followed unconditionally by `watchlist_sync.py` and treats exit/file-presence as `"ok"` must be updated so that: (1) `watchlist_sync.py` is gated on `nse_screener.py scan` exiting 0 (`env -u PYTHONPATH .venv/bin/python nse_screener.py scan && env -u PYTHONPATH .venv/bin/python watchlist_sync.py`) so a `FAILED` scan (exit code `1`) aborts before syncing and propagates the non-zero exit code to cron, and (2) the cron/agent reporting prompt reads `nse_screener_status.json` (instead of checking `nse_screener_candidates.csv` line count or printing a bare `ok`) to report `verdict`, `ok`/`symbols_total`, `error` (`by_error_type`), `filtered_by_reason`, and `candidates`, routing any `verdict != "OK"` (`FAILED` or `DEGRADED`) to `#alerts`, while invoking `env -u PYTHONPATH .venv/bin/python new_high_detector.py` as a separate alert-only step that reads `new_high_alerts_status.json` and `new_high_alerts.csv` for `#signals` human review without ever touching `watchlist.csv`.
