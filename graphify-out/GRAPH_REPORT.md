# Graph Report - fyers-cup-bot-forked  (2026-10-16)

## Corpus Check
- 71 files · ~45,871 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 13 file(s) not represented in the graph (top: (none) 6, .example 2, .csv 2)

## Summary
- 840 nodes · 1685 edges · 35 communities (27 shown, 8 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 88 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `d2c5581e`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- CupBot
- test_intraday_signal.py
- PaperBroker
- test_ledger_tracking.py
- scanner.py
- App.jsx
- LiveBroker
- backend/main.py
- run_paper.py
- datetime
- auth_ui.py
- auth.py
- test_watchlist_sync.py
- dashboard.py
- test_round2_live_risk.py
- fyers_daily_auth.py
- LiveBrokerDouble
- Ledger
- run.py
- FyersClient
- AGENTS.md — read this first: start with the graph, not grep
- notify.py
- Cup Strategy — Malkansview ATH Breakout System
- GTT / Bracket / Trailing Stop-Loss & Profit-Taking — how to use Fyers' inbuilt risk tools
- WALKTHROUGH — how this bot works, in plain English
- test_live_entry_guard.py
- TPM Rules — TRADING (project-scoped)
- Running locally without Docker (Windows)
- SPEC — Round 2: Address reviewer findings (live-risk fix)

## God Nodes (most connected - your core abstractions)
1. `CupBot` - 48 edges
2. `PaperBroker` - 34 edges
3. `Ledger` - 32 edges
4. `LiveBroker` - 30 edges
5. `FyersClient` - 25 edges
6. `LiveBrokerDouble` - 19 edges
7. `Tracker` - 18 edges
8. `main()` - 17 edges
9. `_bot()` - 16 edges
10. `PaperTracker` - 15 edges

## Surprising Connections (you probably didn't know these)
- `5. What is verified vs. NOT verified against real Fyers` --references--> `status()`  [INFERRED]
  docs/LIVE_MODE_DESIGN.md → auth_ui.py
- `1. Where this came from: two independent fix efforts, merged` --references--> `_cancel_ok()`  [INFERRED]
  docs/LIVE_MODE_DESIGN.md → bot.py
- `1. Where this came from: two independent fix efforts, merged` --references--> `_mutator_ok()`  [INFERRED]
  docs/LIVE_MODE_DESIGN.md → bot.py
- `1. Where this came from: two independent fix efforts, merged` --references--> `LiveBroker`  [INFERRED]
  docs/LIVE_MODE_DESIGN.md → broker.py
- `4. Catch-and-alert instead of raise-and-abort` --references--> `LiveBroker`  [INFERRED]
  docs/LIVE_MODE_DESIGN.md → broker.py

## Import Cycles
- None detected.

## Communities (35 total, 8 thin omitted)

### Community 0 - "CupBot"
Cohesion: 0.05
Nodes (40): _cancel_ok(), CupBot, _demo(), LiveOrderError, log(), _mutator_ok(), ScriptedFeed, _today() (+32 more)

### Community 1 - "test_intraday_signal.py"
Cohesion: 0.06
Nodes (29): compute_net_pnl(), detect_signal(), _exit(), initial_stop(), sample_prior_resistance(), simulate_exits(), PaperTracker, PaperTrade (+21 more)

### Community 2 - "PaperBroker"
Cohesion: 0.08
Nodes (16): _now(), PaperBroker, _today(), check(), FakeFyers, fixed_price(), open_position(), test_adoption_scope() (+8 more)

### Community 3 - "test_ledger_tracking.py"
Cohesion: 0.10
Nodes (7): test_duplicate_status_is_idempotent(), test_ledger_upsert_and_report(), test_report_flags_awaiting_ready_and_cap_full(), test_report_pre_trade_split_by_price(), test_tracker_error_neither_crashes_nor_touches_good_state(), test_tracker_maps_alerts_to_ledger(), Tracker

### Community 4 - "scanner.py"
Cohesion: 0.08
Nodes (32): alert_breakeven(), alert_buy(), alert_error(), alert_partial_book(), alert_queue(), alert_sl_hit(), alert_trail_exit(), alert_trail_update() (+24 more)

### Community 5 - "App.jsx"
Cohesion: 0.07
Nodes (36): dependencies, lucide-react, react, react-dom, devDependencies, vite, @vitejs/plugin-react, name (+28 more)

### Community 6 - "LiveBroker"
Cohesion: 0.07
Nodes (20): LiveBroker, 2. `modify_stop_qty`: atomic-with-fallback, 5. What is verified vs. NOT verified against real Fyers, 3.10 `test_live_order.py` itself had a bug, 3.2 `modify_order` sent a payload Fyers would reject, 3.3 Naive local time broke day-roll logic, 3.4 No rate-limit awareness on the orderbook, 3.5 Resizing a resting stop had no safe design (+12 more)

### Community 7 - "backend/main.py"
Cohesion: 0.10
Nodes (22): _post(), add_position_manually(), add_stock(), AddStockBody, close_position_manually(), ConfigUpdate, get_config(), get_conn() (+14 more)

### Community 8 - "run_paper.py"
Cohesion: 0.10
Nodes (17): BarBuilder, current_bar(), finalize_bar(), new_bar(), update_bar(), build_track_symbols(), is_market_hours(), main() (+9 more)

### Community 9 - "datetime"
Cohesion: 0.12
Nodes (18): atm_strike(), candidate_symbols(), _is_weekly(), month_code_3(), month_letter(), monthly_symbol(), next_week_days(), resolve_atm_symbols() (+10 more)

### Community 10 - "auth_ui.py"
Cohesion: 0.10
Nodes (17): apply_from_text(), bot_pids(), clear_stale_lock(), extract_code(), restart_bot(), status(), _today(), 1. One-paragraph summary (+9 more)

### Community 11 - "auth.py"
Cohesion: 0.13
Nodes (11): get_token_totp(), main(), manual_login(), _save_token(), totp_login(), _totp_now(), TotpLoginError, 6. TOTP auto-login (`auth.py`) (+3 more)

### Community 12 - "test_watchlist_sync.py"
Cohesion: 0.06
Nodes (22): _cand_row(), _SimpleCapsys, _SimpleMonkeypatch, test_budget_refusal_and_arithmetic_reported(), test_dedupe_already_present_symbol_not_added_twice(), test_dry_run_writes_nothing(), test_max_watchlist_cap_enforced(), test_no_trailing_newline_handled_correctly() (+14 more)

### Community 13 - "dashboard.py"
Cohesion: 0.15
Nodes (5): App, Handler, load_watchlist(), main(), pnl_stats()

### Community 14 - "test_round2_live_risk.py"
Cohesion: 0.10
Nodes (23): alert_log(), double(), _bot(), _state(), test_live_reconcile_does_not_duplicate_sell_stop(), test_live_reconcile_gone_position_cleans_up_no_phantom_sell_stop(), test_live_reconcile_new_position_creates_pos_state_and_sell_stop(), _bot() (+15 more)

### Community 15 - "fyers_daily_auth.py"
Cohesion: 0.23
Nodes (10): apply_auth_code(), fresh(), load_env(), login_url(), main(), refresh_token(), _save_token(), today() (+2 more)

### Community 18 - "run.py"
Cohesion: 0.23
Nodes (7): load_env(), _acquire_single_instance(), env(), _funds_preflight(), is_market_hours(), load_watchlist(), main()

### Community 19 - "FyersClient"
Cohesion: 0.06
Nodes (13): main(), 1. Where this came from: two independent fix efforts, merged, FyersClient, FyersError, nse(), _ok(), _order_id(), plain() (+5 more)

### Community 20 - "AGENTS.md — read this first: start with the graph, not grep"
Cohesion: 0.40
Nodes (4): AGENTS.md — read this first: start with the graph, not grep, Human-readable orientation, Safety rules for this repo — LIVE MONEY, Your first move in this repo

### Community 23 - "notify.py"
Cohesion: 0.33
Nodes (9): armed(), breakeven(), entry(), error(), exit_(), partial(), report(), send() (+1 more)

### Community 24 - "Cup Strategy — Malkansview ATH Breakout System"
Cohesion: 0.20
Nodes (9): 1. Configure, 2. Start, 3. Access, Bulk Import, Cup Strategy — Malkansview ATH Breakout System, How it works, Phase 2 (Fyers API integration), Setup in 3 steps (+1 more)

### Community 25 - "GTT / Bracket / Trailing Stop-Loss & Profit-Taking — how to use Fyers' inbuilt risk tools"
Cohesion: 0.20
Nodes (9): 1. Smart Stop-loss & Target (replaced legacy Bracket BO / Cover CO), 2. Trailing Stop-Loss (Smart Trail order), 3. TP/SL offsets on a normal/SL-M order, 4. GTT multi-leg (OCO) — `place_gtt_order`, GTT / Bracket / Trailing Stop-Loss & Profit-Taking — how to use Fyers' inbuilt risk tools, Mapping to our cup rules (SL 6% · breakeven +10% · partial-½ +20% · trail 10% of high), Open question to Fyers, Recommended next step (Route B) (+1 more)

### Community 27 - "WALKTHROUGH — how this bot works, in plain English"
Cohesion: 0.20
Nodes (9): 1. What the bot does, in one paragraph, 2. The jobs, in the order they run each day, 3. The money rules (the guards that keep you safe), 4. The files that matter, 5. `watchlist.csv` — the most important file to understand, 6. Where to look when something is wrong, 7. Run the tests, 8. Honest limitation: this is a ONE-strategy bot (+1 more)

### Community 29 - "test_live_entry_guard.py"
Cohesion: 0.43
Nodes (6): _bot(), _state(), test_entry_pass_rearms_after_in_flight_order_cancelled(), test_entry_pass_skips_existing_position(), test_entry_pass_skips_open_order(), test_entry_pass_skips_symbol_in_pos_state()

### Community 31 - "TPM Rules — TRADING (project-scoped)"
Cohesion: 0.33
Nodes (5): Channel map, Persona, TPM Rules — TRADING (project-scoped), Trading invariants (high-stakes — never violated), Working agreements

### Community 32 - "Running locally without Docker (Windows)"
Cohesion: 0.40
Nodes (4): Notes / differences from the Docker setup, One-time setup (already done), Running locally without Docker (Windows), Start the app (run each in its own terminal)

### Community 33 - "SPEC — Round 2: Address reviewer findings (live-risk fix)"
Cohesion: 0.40
Nodes (4): Constraints (unchanged), Deliverables in final reply, MAJOR fixes, SPEC — Round 2: Address reviewer findings (live-risk fix)

## Knowledge Gaps
- **66 isolated node(s):** `name`, `version`, `type`, `dev`, `build` (+61 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 311 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `CupBot` connect `CupBot` to `PaperBroker`, `test_ledger_tracking.py`, `auth_ui.py`, `test_round2_live_risk.py`, `run.py`, `test_live_entry_guard.py`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Why does `LiveBroker` connect `LiveBroker` to `FyersClient`, `CupBot`, `run.py`, `PaperBroker`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Why does `PaperBroker` connect `PaperBroker` to `CupBot`, `run.py`, `test_ledger_tracking.py`, `LiveBroker`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `PaperBroker` (e.g. with `2. Where everything is (file map)` and `Files`) actually correct?**
  _`PaperBroker` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `Ledger` (e.g. with `App` and `Tracker`) actually correct?**
  _`Ledger` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `LiveBroker` (e.g. with `1. Where this came from: two independent fix efforts, merged` and `4. Catch-and-alert instead of raise-and-abort`) actually correct?**
  _`LiveBroker` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `FyersClient` (e.g. with `main()` and `main()`) actually correct?**
  _`FyersClient` has 5 INFERRED edges - model-reasoned connections that need verification._