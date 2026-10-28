# Handoff — Task 183: Momentum Screener & SONACOMS Order Module (Owner-Confirmed Cup Parameters)

## 1. Graphify Provenance
- Initial `graphify-out/graph.json` `built_at_commit` was `d2c5581ea7eb532837b4bda32e4d018772c85085` vs worktree HEAD `080215747c814a6b83b12ea5096c18dd445fbfa7` (`0802157`).
- Ran `graphify update . --force`, rebuilding at `built_at_commit: 080215747c814a6b83b12ea5096c18dd445fbfa7` (1,041 nodes before edits; 1,213 nodes / 2,622 edges after edits).
- Key graphify node citations:
  - `FyersClient` (`fyers_client.py:L137`, community `FyersClient`) -> `.place_buy_stop()` (`fyers_client.py:L246`), `.place_sell_stop()` (`fyers_client.py:L353`), `.modify_order()` (`fyers_client.py:L441`)
  - `CupBot` (`bot.py:L86`, community `CupBot`) -> `._reconcile_live()` (`bot.py:L305`), `._entry_one()` (`bot.py:L461`), `._on_fills()` (`bot.py:L517`), `._exit_pass()` (`bot.py:L552`)
  - `PosState` (`cup_sl_engine.py:L27`) & `step()` (`cup_sl_engine.py:L42`)
  - `load_watchlist()` (`run.py:L34`, id `run_load_watchlist`)
  - `resolve_capital_budget()` (`watchlist_sync.py:L42`)
  - `screen_symbol()` (`nse_screener.py:L66`) & `calc_qty()` (`nse_screener.py:L61`) — untouched
  - New nodes verified via `graphify explain`:
    - `breakout_leg_start()` (`momentum_screener.py:L223`)
    - `previous_ath_from_bars()` (`momentum_screener.py:L239`)
    - `size_qty_in_band()` (`momentum_screener.py:L309`)
    - `compute_cup_levels()` (`momentum_screener.py:L354`)

## 2. Applied File List & Adaptations from Opus's Draft

| File | Action | Adaptations vs. Opus 5.5 Draft |
|---|---|---|
| `fyers_client.py` | Modified (3 hunks) | Applied cleanly at `fyers_client.py:L135` (`stop_limit_buy_payload`), `fyers_client.py:L256` (`history`, `depth`, `holdings`), and `fyers_client.py:L335` (`place_buy_stop_limit`). |
| `cup_sl_engine.py` | Modified (3 hunks) | Applied cleanly at `cup_sl_engine.py:L18-L67`: added `time_stop_days` and `entry_day` on `PosState`, `sessions_since()`, and `time_exit` action in `step()`. Kept `SL_PCT = 0.06`, `BE_TRIGGER = 0.10`, `PARTIAL_TRIGGER = 0.20`, `TRAIL_PCT = 0.10` unchanged. |
| `bot.py` | Modified (5 hunks) | Applied cleanly (`self._rows`, `_new_pos_state`, `_reconcile_live`, `_on_fills`, `_entry_one` `valid_until` guard, `_exit_pass` `time_exit` cancel-then-market-sell). Updated `_new_pos_state` docstring to reflect the 6%-below-entry stop rather than ATR stop. Live runner's entry order path (`_place_buy` -> `broker.place_buy_stop`) is untouched. |
| `run.py` | Modified (2 hunks) | Applied `load_watchlist(..., today=None)` supporting `kind="momentum"` and opt-in `MOMENTUM_WATCHLIST` env var (unset by default -> loads `watchlist.csv` cup rows only). |
| `momentum_screener.py` | Added (new) | Overrode Opus's parameters with owner-confirmed rules: (1) explicit `breakout_leg_start()` + `previous_ath_from_bars()` computing previous ATH strictly before the current breakout leg (never latest high); (2) `entry = align_tick(prev_ath * 1.04)` (dropped `H * 1.003`); (3) `stop = align_tick(entry * (1 - 0.06))` (dropped `1 * ATR14`); (4) `size_qty_in_band()` sizing integer `qty` inside `[Rs 750, Rs 900]` loss band nearest to band midpoint (Rs 825) within `capital_budget`, recording `unsizable` and `unsizable_reasons` when budget or price cannot satisfy the band; (5) added `--fixture` CLI flag for offline fixture execution with zero network/API calls. |
| `scripts/sonacoms_order.py` | Added (new) | Adapted `prior_session()` and `compute_plan()` to use `previous_ath_from_bars()` (previous ATH), `trigger = align_tick(H * 1.04)`, `stop = align_tick(trigger * 0.94)` (6% below entry), and `size_qty_in_band()` (`[Rs 750, Rs 900]` loss band). Added `_validate_holdings_rows()` so missing `quantity` or `costPrice` in holdings fails closed with `FyersError` alongside `circuit_band()`'s fail-closed check for `lower_ckt`/`upper_ckt`. |
| `tests/test_momentum_screener.py` | Added (new) | Added `ROOT` `sys.path[0]` setup, adapted row assertions to owner parameters, and added tests for: previous ATH vs. new high today (single-bar and multi-bar breakout legs), SONACOMS acceptance fixture (`839 -> 872.55 -> 820.20 -> 52.35 -> 16 -> 837.60`), loss-band boundary controls (`qty=15`, `qty=17`, and budget-unsizable rejection), unchanged take-profit ladder (`+10%` BE / `+20%` half / `10%` trail), fail-closed checks on unverified Fyers fields, offline CLI `--fixture` execution, and explicit skip when `token.json` is stale. |
| `tests/test_momentum_runner.py` | Added (new) | Added `ROOT` `sys.path[0]` guard (preventing `auth_ui.py`'s `sys.path.insert(0, BOT)` during full-suite collection from shadowing the worktree's `run.py`) and updated `MOMO` fixture to SONACOMS owner parameters (`rim=839.0, trigger=872.55, qty=16, stop=820.20`). |

## 3. Owner-Confirmed Parameters & Pinning Tests

1. **Previous ATH (never latest high)**:
   - Rule in `momentum_screener.py:L222-L244`:
     `# Rule: previous ATH is the max High strictly before the current breakout leg (asof plus any contiguous prior bars with strictly rising highs and closes).`
   - Pinned by `test_new_high_today_measured_against_previous_ath_not_latest_high` (`tests/test_momentum_screener.py:L97`).
2. **Entry = Previous ATH x 1.04 (tick-aligned to 0.05) & Stop = 6% below Entry (tick-aligned to 0.05)**:
   - `839.0 * 1.04 = 872.56 -> align_tick -> 872.55`
   - `872.55 * 0.94 = 820.197 -> align_tick -> 820.20`
   - `risk_per_share = 872.55 - 820.20 = 52.35`
   - Pinned by `test_sonacoms_acceptance_fixture` (`tests/test_momentum_screener.py:L131`) and `test_row_semantics_use_owner_parameters` (`tests/test_momentum_screener.py:L247`).
3. **Defined-Loss Band Rs 750 to Rs 900 (`size_qty_in_band`)**:
   - `SONACOMS` (`prev_ath=839`): `qty = 16`, `risk = 837.60` (inside `[750, 900]`).
   - Boundary control `qty = 15`: `prev_ath=880 -> entry=915.20, stop=860.30, risk/sh=54.90 -> qty=15, risk=823.50` (and `SONACOMS` at `capital_budget=13,100 -> qty=15, risk=785.25`).
   - Boundary control `qty = 17`: `prev_ath=780 -> entry=811.20, stop=762.55, risk/sh=48.65 -> qty=17, risk=827.05`.
   - Unsatisfiable budget control: `SONACOMS` at `capital_budget=10,000` (max affordable 11 shares -> risk Rs 575.85 < Rs 750) returns `qty=None` and reports `insufficient_budget_for_loss_band` in `status["unsizable"]` / `status["unsizable_reasons"]`.
   - Pinned by `test_loss_band_boundary_controls_qty_15_16_17_and_budget_rejection` (`tests/test_momentum_screener.py:L157`).
4. **Take-Profit Ladder Unchanged**:
   - `+10%` breakeven (`SL -> entry`), `+20%` partial book (50% qty) + start trailing `10%` below running high.
   - Pinned by `test_take_profit_ladder_unchanged` (`tests/test_momentum_screener.py:L265`).
5. **Fail-Loud Accounting**:
   - Per-symbol outcome counts and exception-type counts (`ScanStats`), atomic `momentum_status.json` artifact, `OK` (`symbols_ok >= 2000` and `error_rate <= 2%`), `DEGRADED` (`(2%, 5%]`), `FAILED` (`> 5%`, `symbols_ok < 2000`, `aborted`, or `> 50%` stale), exit code `2` on `FAILED`.
   - Pinned by `test_clean_zero_candidate_run_is_distinguishable_from_all_fail_run`, `test_counter_wrapper_counts_every_exception_by_type`, `test_verdict_error_rate_bands`, `test_default_floor_requires_2000_symbols_ok`.

## 4. Unverified Fyers Field Names Disclosure
Two Fyers field-name pairs in `scripts/sonacoms_order.py` come from the Fyers v3 API documentation and have **no evidence in this repository's client or captured response fixtures**:
1. `lower_ckt` and `upper_ckt` in `FyersClient.depth()` response (`scripts/sonacoms_order.py:circuit_band`).
2. `quantity` and `costPrice` in `FyersClient.holdings()` response (`scripts/sonacoms_order.py:_validate_holdings_rows`, used by `held_qty` and `deployed_notional`).

Both are guarded by explicit **fail-closed** checks that raise `FyersError` before any order can be sent if the fields are missing or empty, pinned by `test_unverified_fyers_fields_fail_closed` (`tests/test_momentum_screener.py:L290`).

## 5. Confirmation: Existing Thresholds & Live Runner Order Path Untouched
- `nse_screener.py` is completely untouched (`ATH_MIN_YEARS = 3`, `QUEUE_PCT = 5.0`, `BREAK_PCT = 2.0`, `SL_PCT = 0.06`, `MIN_AVG_VOLUME = 100000`).
- `watchlist.csv`, `.env`, `token.json`, and `docs/HANDOFF.md` are untouched.
- `run.py` and `bot.py` do not import `momentum_screener` or `scripts.sonacoms_order`, and `CupBot._place_buy` / `LiveBroker.place_buy_stop` remain the sole order-placement path in the live runner.

## 6. Executed Verification Output

### New Test Suite (`tests/test_momentum_screener.py` & `tests/test_momentum_runner.py`)
```text
$ env -u PYTHONPATH /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/pytest tests/test_momentum_screener.py tests/test_momentum_runner.py -v -rs
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0 -- /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/python
cachedir: .pytest_cache
rootdir: /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.claude/worktrees/eng-task-183
collected 39 items

tests/test_momentum_screener.py::test_positive_fixture_is_a_hit_labelled_ath PASSED [  2%]
tests/test_momentum_screener.py::test_52w_high_that_is_not_an_ath_is_labelled_52w PASSED [  5%]
tests/test_momentum_screener.py::test_new_high_today_measured_against_previous_ath_not_latest_high PASSED [  7%]
tests/test_momentum_screener.py::test_sonacoms_acceptance_fixture PASSED [ 10%]
tests/test_momentum_screener.py::test_loss_band_boundary_controls_qty_15_16_17_and_budget_rejection PASSED [ 12%]
tests/test_momentum_screener.py::test_negative_close_below_prior_52w_high PASSED [ 15%]
tests/test_momentum_screener.py::test_negative_volume_below_2x_average PASSED [ 17%]
tests/test_momentum_screener.py::test_volume_exactly_2x_average_passes PASSED [ 20%]
tests/test_momentum_screener.py::test_negative_close_outside_top_third PASSED [ 23%]
tests/test_momentum_screener.py::test_close_exactly_at_two_thirds_passes PASSED [ 25%]
tests/test_momentum_screener.py::test_negative_illiquid_average_volume PASSED [ 28%]
tests/test_momentum_screener.py::test_stale_feed_is_not_a_hit PASSED     [ 30%]
tests/test_momentum_screener.py::test_row_semantics_use_owner_parameters PASSED [ 33%]
tests/test_momentum_screener.py::test_take_profit_ladder_unchanged PASSED [ 35%]
tests/test_momentum_screener.py::test_unverified_fyers_fields_fail_closed PASSED [ 38%]
tests/test_momentum_screener.py::test_counter_wrapper_counts_every_exception_by_type PASSED [ 41%]
tests/test_momentum_screener.py::test_clean_zero_candidate_run_is_distinguishable_from_all_fail_run PASSED [ 43%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[0-OK] PASSED [ 46%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[2-OK] PASSED [ 48%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[3-DEGRADED] PASSED [ 51%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[5-DEGRADED] PASSED [ 53%]
tests/test_momentum_screener.py::test_verdict_error_rate_bands[6-FAILED] PASSED [ 56%]
tests/test_momentum_screener.py::test_default_floor_requires_2000_symbols_ok PASSED [ 58%]
tests/test_momentum_screener.py::test_stale_majority_fails_the_run PASSED [ 61%]
tests/test_momentum_screener.py::test_offline_cli_fixture_scan_and_list PASSED [ 64%]
tests/test_momentum_screener.py::test_live_fyers_history_skipped_when_token_stale SKIPPED [ 66%]
tests/test_momentum_runner.py::test_momentum_row_arms_buy_stop_near_rim PASSED [ 69%]
tests/test_momentum_runner.py::test_expired_momentum_row_never_arms PASSED [ 71%]
tests/test_momentum_runner.py::test_momentum_fill_uses_row_stop PASSED   [ 74%]
tests/test_momentum_runner.py::test_momentum_stop_above_fill_falls_back_to_engine_default PASSED [ 76%]
tests/test_momentum_runner.py::test_cup_row_keeps_engine_default_stop PASSED [ 79%]
tests/test_momentum_runner.py::test_time_stop_cancels_stop_then_sells PASSED [ 82%]
tests/test_momentum_runner.py::test_time_stop_not_before_n_sessions PASSED [ 84%]
tests/test_momentum_runner.py::test_time_stop_refuses_to_sell_when_cancel_unconfirmed PASSED [ 87%]
tests/test_momentum_runner.py::test_engine_time_exit_counts_sessions PASSED [ 89%]
tests/test_momentum_runner.py::test_engine_time_stop_ignored_after_breakeven PASSED [ 92%]
tests/test_momentum_runner.py::test_load_watchlist_momentum_rows PASSED  [ 94%]
tests/test_momentum_runner.py::test_load_watchlist_rejects_stop_not_below_trigger PASSED [ 97%]
tests/test_momentum_runner.py::test_load_watchlist_cup_rows_unchanged PASSED [100%]

=========================== short test summary info ============================
SKIPPED [1] tests/test_momentum_screener.py:425: Live Fyers market token.json is missing or stale today; skipping live API check
======================== 38 passed, 1 skipped in 0.57s =========================
```

### Full Repository Pytest Suite & `test_live_fixes.py`
```text
$ env -u PYTHONPATH /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/pytest tests/ -q -rs
178 passed, 1 skipped, 5 warnings in 3.01s

$ env -u PYTHONPATH /opt/jarvis-dsh/scratch/fyers-cup-bot-forked/.venv/bin/python test_live_fixes.py
37 passed, 0 failed
all good
```

### Offline Screener Run Against Fixtures
```text
[Momentum] asof 2026-10-21 | universe 7 | CAPITAL_BUDGET Rs 50,000
  [0/7] 0s ok=0 err=0 hits=0

[Momentum] verdict OK | ok 7 | errors 0 (0.00%) {} | candidates 3
   SONACOMS     ATH rim 839.0 trig 872.55 stop 820.2 qty 16 risk 837.6 vol x5.0
   BAND15       ATH rim 880.0 trig 915.2 stop 860.3 qty 15 risk 823.5 vol x4.5
   BAND17       ATH rim 780.0 trig 811.2 stop 762.55 qty 17 risk 827.05 vol x4.0

--- momentum_candidates.csv ---
symbol,rim,trigger,qty,kind,stop,risk_per_share,risk,time_stop_days,valid_until,asof,high_kind,ath_check,atr14,close,vol_mult,range_pos
SONACOMS,839.0,872.55,16,momentum,820.2,52.35,837.6,5,2026-10-22,2026-10-21,ATH,ok,24.43,851.85,5.0,0.891
BAND15,880.0,915.2,15,momentum,860.3,54.9,823.5,5,2026-10-22,2026-10-21,ATH,ok,27.64,895.0,4.5,0.833
BAND17,780.0,811.2,17,momentum,762.55,48.65,827.05,5,2026-10-22,2026-10-21,ATH,ok,20.5,795.0,4.0,0.833
```
