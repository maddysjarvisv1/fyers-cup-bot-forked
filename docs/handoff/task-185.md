# Handoff — Task 185: Create Missing `--out-dir` Parent Directories in `momentum_screener.py`

## 1. Graphify Provenance
- Initial `graphify-out/graph.json` had `built_at_commit: d2c5581ea7eb532837b4bda32e4d018772c85085` vs. worktree `HEAD` `4e6f27e9f369303cee7265adebf305b9d095579d` (`4e6f27e`).
- Ran `graphify update . --force` before querying; rebuilt at `built_at_commit: 4e6f27e9f369303cee7265adebf305b9d095579d` (1,316 nodes, 2,893 edges, 70 communities before edits; 1,321 nodes, 2,906 edges, 78 communities after edits).
- Key graphify node citations:
  - `momentum_screener.py` (`momentum_screener.py:L1`, id `momentum_screener`, community `momentum_screener.py`)
  - `_atomic_write()` (`momentum_screener.py:L505` pre-edit / `L506` post-edit, id `momentum_screener_atomic_write`)
  - `run_scan()` (`momentum_screener.py:L528` pre-edit / `L557` post-edit, id `momentum_screener_run_scan`) — calls `_atomic_write()` (`momentum_screener.py:L583-584` pre-edit / `L612-613` post-edit)
  - `_fail_setup()` (`momentum_screener.py:L588` pre-edit / `L617` post-edit, id `momentum_screener_fail_setup`) — calls `_atomic_write()` (`momentum_screener.py:L597` pre-edit / `L626` post-edit)
  - `list_cands()` (`momentum_screener.py:L627` pre-edit / `L656` post-edit, id `momentum_screener_list_cands`)
  - `main()` (`momentum_screener.py:L638` pre-edit / `L668` post-edit, id `momentum_screener_main`)
  - `test_momentum_screener.py` (`tests/test_momentum_screener.py:L1`, id `tests_test_momentum_screener`)
  - `test_scan_creates_nonexistent_nested_out_dir_and_writes_status_and_candidates()` (`tests/test_momentum_screener.py:L422`, id `tests_test_momentum_screener_test_scan_creates_nonexistent_nested_out_dir_and_writes_status_and_candidates`)
  - `test_unwritable_out_dir_raises_clear_error()` (`tests/test_momentum_screener.py:L490`, id `tests_test_momentum_screener_test_unwritable_out_dir_raises_clear_error`)

## 2. Audit of Every Output & CLI Path in `momentum_screener.py`

| Symbol / Location | Path / Artifact | Pre-Fix Assumption | Fix Applied |
|---|---|---|---|
| `_atomic_write(path, text)` (`momentum_screener.py:L506-546`) | All file writes in `momentum_screener.py` | Assumed `d = os.path.dirname(os.path.abspath(path))` already existed before calling `tempfile.mkstemp(dir=d, prefix=".momentum_", suffix=".tmp")`, raising `FileNotFoundError` when `d` or its parent directories did not exist. | Validates the nearest existing ancestor directory is a directory (`NotADirectoryError` if blocked by a file) and writable (`PermissionError` on read-only mode bits or failed `os.access`), calls `os.makedirs(d, exist_ok=True)` to create `d` and all missing parents, creates the temp file in `d` via `tempfile.mkstemp(dir=d, prefix=".momentum_", suffix=".tmp")`, `fsync`s, and atomically replaces `path` via `os.replace(tmp, path)`. Re-raises `OSError` loudly with a clear message naming `d` if unwritable. |
| `run_scan(..., cand_path=CAND, status_path=STATUS)` (`momentum_screener.py:L557-614`) | `cand_path` (`momentum_candidates.csv`, `L612`) and `status_path` (`momentum_status.json`, `L613`) | Both writes go through `_atomic_write`. On `OK`/`DEGRADED` runs (`wrote=True`), `cand_path` at `L612` failed first when its parent directory did not exist; on `FAILED` runs (`wrote=False`), `status_path` at `L613` failed after the entire scan completed. | Fixed via `_atomic_write` creating `os.path.dirname(os.path.abspath(path))` before writing each artifact (including when `cand_path` and `status_path` live in nested or distinct directories). |
| `_fail_setup(status_path, asof, err_type, msg)` (`momentum_screener.py:L617-628`) | `status_path` (`momentum_status.json`, `L626`) | Called from `main()` on `SessionNotClosed` (`L689`) or setup exceptions (`L705`); called `_atomic_write(status_path, ...)` which crashed with `FileNotFoundError` instead of writing the `FAILED` status artifact if `--out-dir` did not exist yet. | Fixed via `_atomic_write` creating parent directories before writing `status_path`. |
| `main(argv=None)` `scan` CLI path (`momentum_screener.py:L671-719`) | `cand_path` (`L684`), `status_path` (`L685`), `--fixture` (`L693`) | Derived `cand_path` and `status_path` from `--out-dir` without creating `--out-dir`; opened `--fixture` without a context manager. | Both output artifacts now create `--out-dir` (and parents) via `_atomic_write`; `--fixture` opened with `with open(a.fixture, encoding="utf-8") as f:`. |
| `main(argv=None)` `list` CLI path & `list_cands(path=CAND)` (`momentum_screener.py:L656-683`) | `CAND` / `<out-dir>/momentum_candidates.csv` | `list` subparser did not accept `--out-dir` (unlike `scan --out-dir`), and `list_cands` opened `path` without a `with` block. | Added `--out-dir` to `list` subparser (resolving `<out-dir>/momentum_candidates.csv` when provided, else `CAND`), and used `with open(path, encoding="utf-8", newline="") as f:` in `list_cands`. |
| Module defaults `HERE`, `CAND`, `STATUS` (`momentum_screener.py:L53-56`) | Default `momentum_candidates.csv` and `momentum_status.json` in repo root | Default paths for `run_scan` and `list_cands`. | Untouched; routed through `_atomic_write` on write and `os.path.exists` on read. |

## 3. Minimal Diff (`momentum_screener.py`)

```diff
diff --git a/momentum_screener.py b/momentum_screener.py
index 642910a..92b06a8 100644
--- a/momentum_screener.py
+++ b/momentum_screener.py
@@ -24,7 +24,7 @@ Take-profit ladder (in cup_sl_engine.py, unchanged):
 
 Usage:
   momentum_screener.py scan [max_symbols] [--asof YYYY-MM-DD] [--min-ok N] [--interval S] [--out-dir D] [--fixture PATH]
-  momentum_screener.py list
+  momentum_screener.py list [--out-dir D]
 Writes momentum_candidates.csv (watchlist rows; run.py loads it when MOMENTUM_WATCHLIST is set)
 and momentum_status.json (machine-readable verdict). Exit: 0 = OK/DEGRADED, 2 = FAILED.
 """
@@ -33,6 +33,7 @@ from __future__ import annotations
 import argparse
 import csv
 import datetime as dt
+import errno
 import io
 import json
 import math
@@ -503,8 +504,36 @@ def judge(stats, *, min_ok=MIN_SYMBOLS_OK, aborted=False):
 
 # --- artifacts -----------------------------------------------------------------------
 def _atomic_write(path, text):
-    d = os.path.dirname(os.path.abspath(path)) or "."
-    fd, tmp = tempfile.mkstemp(dir=d, prefix=".momentum_", suffix=".tmp")
+    target = os.path.abspath(path)
+    d = os.path.dirname(target) or "."
+    probe = d
+    while probe and not os.path.exists(probe):
+        parent = os.path.dirname(probe)
+        if parent == probe:
+            break
+        probe = parent
+    if probe and os.path.exists(probe):
+        if not os.path.isdir(probe):
+            raise NotADirectoryError(
+                errno.ENOTDIR, f"Target directory path is not a directory: {d}", probe
+            )
+        st = os.stat(probe)
+        mode_unwritable = (st.st_mode & 0o222) == 0 or (
+            hasattr(os, "geteuid") and os.geteuid() == st.st_uid and (st.st_mode & 0o200) == 0
+        )
+        if not os.access(probe, os.W_OK | os.X_OK) or mode_unwritable:
+            raise PermissionError(
+                errno.EACCES, f"Target directory is not writable: {d}", probe
+            )
+    try:
+        os.makedirs(d, exist_ok=True)
+        fd, tmp = tempfile.mkstemp(dir=d, prefix=".momentum_", suffix=".tmp")
+    except OSError as exc:
+        raise type(exc)(
+            exc.errno,
+            f"Target directory is not writable ({d}): {exc.strerror or exc}",
+            exc.filename or d,
+        ) from exc
     try:
         with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
             f.write(text)
@@ -628,7 +657,8 @@ def list_cands(path=CAND):
     if not os.path.exists(path):
         print("no momentum candidates yet — run: momentum_screener.py scan")
         return
-    rows = list(csv.DictReader(open(path)))
+    with open(path, encoding="utf-8", newline="") as f:
+        rows = list(csv.DictReader(f))
     print(f"{len(rows)} momentum candidates in {os.path.basename(path)}:")
     for r in rows[:15]:
         print(f"   {r['symbol']:<12} {r['high_kind']:<3} rim {r['rim']:>9} trig {r['trigger']:>9} "
@@ -645,10 +675,11 @@ def main(argv=None):
     sc.add_argument("--interval", type=float, default=REQ_INTERVAL)
     sc.add_argument("--out-dir", default=HERE)
     sc.add_argument("--fixture", default=None, help="offline fixture JSON path (no network/API calls)")
-    sub.add_parser("list")
+    lp = sub.add_parser("list")
+    lp.add_argument("--out-dir", default=None)
     a = ap.parse_args(argv)
     if a.cmd == "list":
-        list_cands()
+        list_cands(os.path.join(a.out_dir, "momentum_candidates.csv") if a.out_dir else CAND)
         return 0
 
     cand_path = os.path.join(a.out_dir, "momentum_candidates.csv")
@@ -660,7 +691,8 @@ def main(argv=None):
                            f"asof {asof} is today and the session has not closed (now {now:%H:%M} IST)")
     try:
         if a.fixture:
-            payload = json.load(open(a.fixture))
+            with open(a.fixture, encoding="utf-8") as f:
+                payload = json.load(f)
             client = _FixtureFyersClient(payload)
             symbols = list(payload.get("symbols") or payload.get("series", {}).keys())
             if a.max_symbols:
```

## 4. New Tests & Full Repository Test Suite Output

### New Tests Added in `tests/test_momentum_screener.py`
1. `test_scan_creates_nonexistent_nested_out_dir_and_writes_status_and_candidates` (`tests/test_momentum_screener.py:L422`)
2. `test_unwritable_out_dir_raises_clear_error` (`tests/test_momentum_screener.py:L490`)

```text
$ env -u PYTHONPATH /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/python -m pytest -v tests/test_momentum_screener.py
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0 -- /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/python
cachedir: .pytest_cache
rootdir: /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.claude/worktrees/eng-task-185
collecting ... collected 28 items

tests/test_momentum_screener.py::test_positive_fixture_is_a_hit_labelled_ath PASSED [  3%]
tests/test_momentum_screener.py::test_52w_high_that_is_not_an_ath_is_labelled_52w PASSED [  7%]
tests/test_momentum_screener.py::test_new_high_today_measured_against_previous_ath_not_latest_high PASSED [ 10%]
tests/test_momentum_screener.py::test_sonacoms_acceptance_fixture PASSED [ 14%]
tests/test_momentum_screener.py::test_loss_band_boundary_controls_qty_15_16_17_and_budget_rejection PASSED [ 17%]
tests/test_momentum_screener.py::test_negative_close_below_prior_52w_high PASSED [ 21%]
tests/test_momentum_screener.py::test_negative_volume_below_2x_average PASSED [ 25%]
tests/test_momentum_screener.py::test_volume_exactly_2x_average_passes PASSED [ 28%]
tests/test_momentum_screener.py::test_negative_close_outside_top_third PASSED [ 32%]
tests/test_momentum_screener.py::test_close_exactly_at_two_thirds_passes PASSED [ 35%]
tests/test_momentum_screener.py::test_negative_illiquid_average_volume PASSED [ 39%]
tests/test_momentum_screener.py::test_stale_feed_is_not_a_hit PASSED     [ 42%]
tests/test_momentum_screener.py::test_row_semantics_use_owner_parameters PASSED [ 46%]
tests/test_momentum_screener.py::test_take_profit_ladder_unchanged PASSED [ 50%]
tests/test_momentum_screener.py::test_unverified_fyers_fields_fail_closed PASSED [ 53%]
tests/test_momentum_screener.py::test_counter_wrapper_counts_every_exception_by_type PASSED [ 57%]
tests/test_momentum_screener.py::test_clean_zero_candidate_run_is_distinguishable_from_all_fail_run PASSED [ 60%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[0-OK] PASSED [ 64%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[2-OK] PASSED [ 67%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[3-DEGRADED] PASSED [ 71%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[5-DEGRADED] PASSED [ 75%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[6-FAILED] PASSED [ 78%]
tests/test_momentum_screener.py::test_default_floor_requires_2000_symbols_ok PASSED [ 82%]
tests/test_momentum_screener.py::test_stale_majority_fails_the_run PASSED [ 85%]
tests/test_momentum_screener.py::test_offline_cli_fixture_scan_and_list PASSED [ 89%]
tests/test_momentum_screener.py::test_scan_creates_nonexistent_nested_out_dir_and_writes_status_and_candidates PASSED [ 92%]
tests/test_momentum_screener.py::test_unwritable_out_dir_raises_clear_error PASSED [ 96%]
tests/test_momentum_screener.py::test_live_fyers_history_skipped_when_token_stale SKIPPED [100%]

======================== 27 passed, 1 skipped in 0.61s =========================
```

### Full Repository Test Suite
```text
$ env -u PYTHONPATH /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/python -m pytest -v
...
================== 199 passed, 1 skipped, 5 warnings in 5.32s ==================
```

## 5. Real CLI Verification on a Fresh Nested `--out-dir` (40 Symbols)

```text
$ rm -rf /tmp/task-185-verify
$ cd /opt/jarvis-dsh/scratch/fyers-cup-bot-forked && env -u PYTHONPATH /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/python /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.claude/worktrees/eng-task-185/momentum_screener.py scan 40 --min-ok 40 --out-dir /tmp/task-185-verify/fresh/nested/out
[Momentum] asof 2026-10-27 | universe 40 | CAPITAL_BUDGET Rs 50,000
  [0/40] 0s ok=0 err=0 hits=0

[Momentum] verdict OK | ok 40 | errors 0 (0.00%) {} | candidates 0
exit_code=0

$ ls -la /tmp/task-185-verify/fresh/nested/out
total 16
drwxr-xr-x 2 root root 4096 Oct 28 04:38 .
drwxr-xr-x 3 root root 4096 Oct 28 04:38 ..
-rw------- 1 root root  136 Oct 28 04:38 momentum_candidates.csv
-rw------- 1 root root  852 Oct 28 04:38 momentum_status.json

$ cat /tmp/task-185-verify/fresh/nested/out/momentum_status.json
{
  "aborted": false,
  "asof": "2026-10-27",
  "ath_lookup_errors": {},
  "attempted": 40,
  "candidates": 0,
  "candidates_path": "/tmp/task-185-verify/fresh/nested/out/momentum_candidates.csv",
  "capital_budget": 50000.0,
  "error_rate": 0.0,
  "error_samples": {},
  "errors": 0,
  "errors_by_type": {},
  "exit_code": 0,
  "finished_at": "2026-10-28T10:08:25.823833+05:30",
  "outcomes": {
    "below_52w_high": 1,
    "illiquid": 11,
    "insufficient_history": 6,
    "no_volume_spike": 20,
    "weak_close": 2
  },
  "started_at": "2026-10-28T10:08:13.389304+05:30",
  "symbols_ok": 40,
  "thresholds": {
    "degraded_max_error_rate": 0.05,
    "min_symbols_ok": 40,
    "ok_max_error_rate": 0.02
  },
  "tool": "momentum_screener",
  "universe": 40,
  "unsizable": [],
  "unsizable_reasons": {},
  "verdict": "OK",
  "verdict_reasons": []
}

$ head -n 5 /tmp/task-185-verify/fresh/nested/out/momentum_candidates.csv
symbol,rim,trigger,qty,kind,stop,risk_per_share,risk,time_stop_days,valid_until,asof,high_kind,ath_check,atr14,close,vol_mult,range_pos
```
