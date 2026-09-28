# SPEC — Round 3: Close remaining live-risk findings

An independent reviewer re-reviewed your Round-2 diff. 5 of its 9 findings are CLOSED
(1,2,3,5,8). Four still create real money exposure. Fix ALL four below (strict TDD:
tests first RED, then impl GREEN). Repo: /opt/jarvis-dsh/scratch/fyers-cup-bot-forked
(cwd, branch fix/live-risk-mgmt). Test cmd: `.venv/bin/python -m pytest tests/ -q`.
Read the reviewer report at /opt/jarvis-dsh/scratch/reviewer_report_round2.txt first; your
round-2 code and its reasoning are the baseline.

## R3-1 (CRITICAL, NEW-1): `_reconcile_qty` MUST cancel/modify the old stop before re-placing — never stack sell-stops.
Current bug: on qty drift the code calls `_place_sell(...)` WITHOUT cancelling the existing
`ps["sell_stop_id"]` first, so 40->100 leaves a 40-lot and a 100-lot stop resting = 140 of
SELL against 100 held -> oversell/short. Reached by (a) genuine partial fills, (b) a failed
cancel in partial-book persisting 50-qty then drifting to 100, (c) position-lag right after a
market_sell.
Fix (use the same safe pattern as partial-book):
  1. `cancel(old_id)` -> `_check_live` -> set `ps["sell_stop_id"]=None` -> `_save()` -> then
     `_place_sell(sym, new_qty, st.sl)`.
  2. If cancel fails, do NOT place a new stop (no stacking); leave `sell_stop_id=None` for the
     self-heal and alert CRITICAL.
Test (name -> assertion):
  - `test_reconcile_qty_cancels_old_stop_before_replacing`: force drift (qty 40 then 100);
    assert old_id in double.cancelled, exactly ONE SELL stop resting after, and no oversell
    (total SELL stop qty <= held qty).

## R3-2 (CRITICAL, NEW-3): an entry-side failure must NOT starve exit management / the heal.
Current bug: `cycle()` order is `_reconcile_live` -> `_entry_pass` -> `_exit_pass`. If a
watchlist symbol's `place_buy_stop` raises every cycle (margin/circuit/reject, or a
`positions()`/`open_orders()` exception in `_entry_pass`), the whole cycle aborts before
`_exit_pass` runs — so no trailing, no partial booking, and no heal for an already-naked position.
Fix: make entry failure per-symbol isolated so exits/heal still run:
  - Wrap each symbol's entry block so an exception (LiveOrderError or broker error) is caught,
    alerts, and continues to the next symbol instead of aborting the cycle.
  - (Prefer) reorder so exit-management (`_exit_pass`) + self-heal run BEFORE `_entry_pass`, so a
    later entry failure can never prevent exit protection.
Test (name -> assertion):
  - `test_entry_failure_does_not_starve_exit_or_heal`: a symbol whose place_buy_stop always fails,
    while another managed position needs its SL healed / trailed; assert the heal and trail still
    happen (raise does not abort exit pass).

## R3-3 (from finding #4): `_cancel_tolerant` must NOT silently swallow a failed cancel during gone-cleanup.
Current bug: `_cancel_tolerant` does `except Exception: pass` and ignores the return value, then
`pos_state[sym]` is popped anyway — so a failed cancel leaves the resting SELL-stop forgotten
(-> later trigger opens a short).
Fix:
  - Only tolerate a cancel when it is effectively OK: success, or an explicit "already filled /
    not found / order not open" condition.
  - On any other failure (or an exception), do NOT silently drop it: alert CRITICAL and retain a
    record of the orphan order id (e.g. an `_orphan_stops` list or keep pos_state with a
    `_need_cancel` flag) so it is retried next cycle before the position is forgotten.
Test (name -> assertion):
  - `test_cancel_failure_during_gone_keeps_orphan_and_alerts`: cancel returns an error -> CRITICAL
    alert fired, orphan id retained/retried, position cleanup does NOT lose the stop silently.

## R3-4 (from finding #7): treat `None` as FAILURE for LIVE mutators (cancel/market_sell/modify_stop).
Current bug: `_request_ok(None)` is True in LIVE, so a LiveBroker method that swallows an
exception and returns None is counted as success (-> stale SL not corrected / orphan not flagged).
Fix: `_request_ok` must return True for `None` ONLY when `mode != "LIVE"`; in LIVE, `None` from a
mutator is a FAILURE. (place_* already raises on None/id-less — keep that.)
Test (name -> assertion):
  - `test_live_mutator_none_is_failure`: in LIVE mode, cancel/market_sell/modify_stop returning
    None raises LiveOrderError (or is treated as failure with an alert); in PAPER mode None still
    counts as success (paper regression).

## Constraints (unchanged)
- Strict TDD; mocks only (extend LiveBrokerDouble in conftest as needed); no network/real
  orders/credentials; do NOT modify cup_sl_engine.py; do NOT edit watchlist.csv; do NOT change
  auth.py or real FyersClient live-op signatures; stay on fix/live-risk-mgmt; commit logically
  (tests commit, impl commit); do not merge/push main. Keep ALL existing tests passing (28).
- Add `# === FIX(live-risk):` comment on each changed block.

## Deliverables in final reply
- Tests added (name -> behavior -> which finding R3-1..4 it closes)
- RED->GREEN evidence
- files changed with `=== FIX(live-risk):` reasons
- confirmation the full suite (prior 28 + new) passes