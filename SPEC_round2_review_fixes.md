# SPEC — Round 2: Address reviewer findings (live-risk fix)

An independent reviewer (claude-opus-5.5) reviewed your Round-1 diff and returned
REQUEST_CHANGES. You must fix ALL CRITICAL and MAJOR findings below, using strict TDD
(tests first, RED, then impl, GREEN). Repo: /opt/jarvis-dsh/scratch/fyers-cup-bot-forked
(cwd, branch fix/live-risk-mgmt). Test cmd: `.venv/bin/python -m pytest tests/ -q`.
Read reviewer_report.txt in /opt/jarvis-dsh/scratch/ and the current code before coding.

## CRITICAL fixes (each = failing test first)

1. **Persist state even when a live op raises mid-cycle.**
   Wrap `cycle()` body in `try/finally` so `_save()` always runs. Additionally, persist
   `pos_state` (and `_in_flight`) immediately after each successful broker mutation
   (reconcile ENTRY, exit cleanup, partial book, modify). Rationale: a raise after sym A's
   sell-stop but before sym B's must not let a restart duplicate sym A's SELL-stop or
   re-book a partial. Test: two-symbol reconcile where B's place_sell_stop fails; assert A is
   persisted on disk (state file) after the raise AND next cycle does NOT place a 2nd SELL for A.

2. **No naked position when replace-stop fails after cancel.**
   In the partial-book path, if `cancel` succeeds but the replacement `place_sell_stop` fails:
   persist `ps["sell_stop_id"] = None` (SL missing marker) BEFORE raising, and add a self-heal:
   in `_exit_pass` (or reconcile), if a symbol in `pos_state` has `sell_stop_id` falsy/None and a
   position still exists, re-place the protective sell-stop (compute from current state.sl).
   Test: cancel ok + place fails -> raise, state marks SL missing, next cycle re-places the SL.

3. **Partial-book order: cancel FIRST, then market-sell, then re-place.**
   Reorder the partial-book path to `cancel(old stop) -> market_sell -> place(new stop)`
   so the full-qty old stop is not resting while the half-qty sell executes (prevents oversell /
   short). Verify each step; on any failure raise + persist state. Update the existing
   call-order test to assert `cancel` happens before `market_sell` before `place`.

4. **Gone-position cleanup must be robust against transient empty positions() and must cancel the old stop.**
   - Do NOT trust a single `positions()` snapshot: if `positions()` returns `[]` for a symbol
     that is in `pos_state`, require the absence to persist across `GONE_CONFIRM_CYCLES`
     (default 2) before treating it as an exit. Track a `_gone_since` counter in memory/state.
   - If/when confirmed gone, `cancel(sell_stop_id)` (tolerating "already filled/not found" as ok)
     before removing from `pos_state`.
   - Treat a failed/exceptional `positions()` call as an ERROR (set alert/flag), NOT as an
     empty list. Wrap the call; on exception, skip cleanup and alert.
   Tests: (a) one empty cycle does not wipe state; two empty cycles confirm exit AND cancel the
   stop; (b) when the position reappears after a transient empty, no duplicate SELL-stop.


## MAJOR fixes

5. **Reconcile only strategy positions.**  Only create a sell-stop for symbols that are in the
   watchlist OR in `_in_flight` OR already in `pos_state`. Ignore foreign/manual/holdings/long-dated
   positions. Test: a non-watchlist foreign position gets NO sell-stop.

6. **Handle partial fills / qty drift.**  In reconcile, if a symbol is in `pos_state` but its
   live `positions()` qty differs from `state.qty_remaining`, update the tracked quantity and
   re-place/modify the sell-stop to the correct qty. Test: qty goes 40 then 100 across cycles;
   stop qty ends at 100.

7. **Make `_request_ok` strict for LIVE and normalize order ids.**
   - In LIVE, `place_sell_stop`/`place_buy_stop` must return a non-empty STRING order id on
     success. If the broker returns a dict with an `id` field, extract that id before storing.
   - Mutators (`modify_stop`/`cancel`/`market_sell`) must return `s=="ok"` (case-insensitive) to
     count as success; any `error`/missing success -> failure.
   - Unit-test `_request_ok` (and id extraction) with: None, "", "L001",
     {"s":"ok"}, {"s":"error"}, {"s":"ok","id":"X"}.
   - Treat `None` from a LIVE place_* as a failure (it was swallowed).

8. **`_in_flight` lifecycle: check buy-stop result + clear on cancel/reject/expiry.**
   - Apply `_check_live`/result-check to `place_buy_stop` before `_in_flight.add(sym)` (a failed
     arm must not block the symbol, and must not fire a false ARMED alert).
   - Clear `_in_flight` for a symbol when it is in neither `open_orders()` nor `positions()` (it
     was cancelled/rejected/expired), so it can re-arm.
   - Note/keep the fact that `_in_flight` is intentionally NOT persisted and is reconciled from
     `open_orders()` each cycle; add a comment documenting that.
   Test: buy-stop fails -> symbol NOT blocked and no ARMED alert; buy-stop disappears from
   open_orders with no position -> symbol can re-arm next cycle.

9. **Exit P&L must use `qty_remaining`, not `qty_total`.**  In the gone-position reconcile and
   anywhere exit P&L is computed, use the quantity actually still held (qty_remaining) and compute
   against the stop price. Test for post-partial exit P&L.

## Constraints (unchanged)
- Strict TDD; mocks only (LiveBrokerDouble in conftest); no network/real orders/credentials;
- Do NOT modify cup_sl_engine.py pure logic; do NOT edit watchlist.csv data;
- Do NOT change auth.py or real FyersClient live-op signatures used by the bot;
- Stay on branch fix/live-risk-mgmt; commit logically (tests commit, impl commit); do not merge/push main;
- Add `# === FIX(live-risk):` comment above each changed block.
- Keep ALL existing tests passing (11) plus new ones.

## Deliverables in final reply
Tests added (name->behavior + which finding it closes), RED->GREEN evidence, files changed with
`=== FIX(live-risk):` reasons, and confirmation the previous 11 still pass.