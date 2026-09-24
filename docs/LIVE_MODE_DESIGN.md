# Live-mode design notes (for whoever/whatever picks this up next — "Hermes")

This document exists so a future session — human or AI — can understand
*why* this codebase looks the way it does, not just what it does. The code
has comments at the point of each decision; this is the connective tissue
between them.

## 1. Where this came from: two independent fix efforts, merged

Two separate passes at making this bot live-safe existed before this branch:

1. **The broker/client layer fix** (`fyers_client.py`, `broker.py`): the
   original code trusted Fyers responses without checking `s == "ok"`, built
   a `modify_order` payload Fyers would have rejected, used naive local time
   instead of IST (so day-roll logic fired at the wrong moment), and re-fetched
   the orderbook on every single call with no rate-limit awareness. This pass
   fixed all of that: `_ok()`/`_order_id()` validate every response and raise
   `FyersError` rather than silently returning garbage; `modify_order` sends
   the fields Fyers's v3 API actually expects; timestamps are IST-aware;
   `LiveBroker` caches the orderbook with a 3s TTL, invalidated on every write.

2. **The reconciliation/crash-safety fix** (`bot.py`, on the
   `fix/live-risk-flash-test` branch upstream): written *against the still-broken
   broker layer above*, so it couldn't trust `feed()`'s fill events and instead
   did everything by diffing `positions()` against saved state every cycle —
   a strictly more paranoid, defense-in-depth style. It's also what introduced
   `_mutator_ok`/`_cancel_ok`, the "a None result in LIVE is a failure, not
   success" invariant, and the discipline of never placing a second stop until
   a cancel is confirmed.

**Neither alone is live-safe.** The first pass without the second would place
correct orders but has no safety net if a fill event is ever missed. The
second pass without the first is reconciling against a broker layer that
returns wrong order ids and rejected modify payloads. This branch combines
both: the broker layer is trustworthy, and the reconciliation layer still
runs anyway, because "trustworthy in normal operation" and "certain to never
fail" are different things.

## 2. `modify_stop_qty`: atomic-with-fallback

The single trickiest piece is resizing a resting SELL stop-loss order (e.g.
the partial-book at +20%, or a qty-drift resync). Two unsafe extremes exist:

- **Naive modify**: assume Fyers accepts a combined qty+trigger modify on an
  SL-M order. If it doesn't, you get an exception and the position may end
  up unprotected or double-protected depending on when it failed.
- **Naive cancel-then-place**: cancel the old stop, place a new one. Between
  the two calls there is a window — however short — where NOTHING protects
  the position. If the cancel silently fails (network blip, Fyers momentarily
  unresponsive) and you place anyway, you now have TWO resting stops on
  overlapping quantity — a real risk of an accidental short if both fire.

`broker.py`'s `LiveBroker.modify_stop_qty` does the safer thing: try the
atomic combined-modify first (zero window, the ideal case). Only if that
raises does it fall back to cancel-then-replace — and even then, it refuses
to place the replacement unless the cancel came back with a *confirmed*
`s == "ok"`. An unconfirmed cancel means: stop resizing, alert, leave the
old (correctly-sized, or slightly-stale) stop in place, and let next cycle's
self-heal/reconciliation sort it out. This is strictly safer than either
extreme — no window when Fyers cooperates, no double-stop when it doesn't.

Whether Fyers's API actually accepts the combined qty+trigger modify on an
SL-M order **has not been confirmed against a real account** — see §5.

## 3. Two reconciliation nets, and why both matter

`bot.py` runs THREE separate mechanisms that overlap by design:

1. **`broker.feed()`** — the primary source of truth for fills. Reads the
   orderbook, matches rows by remembered order id OR echoed `orderTag`,
   turns a filled BUY into an ENTRY and a filled SELL-stop into a STOP_EXIT.
   Accurate fill price, immediate.
2. **`_reconcile_live()`** (LIVE-only) — diffs `positions()` against saved
   state every cycle. Catches what `feed()` might miss: a fill whose
   `orderTag` didn't echo back, an orderbook row that aged out of Fyers's
   window before we polled it, a position that appeared from outside the
   bot's own flow. Also does qty-drift resync (see below).
3. **The exit-pass self-heal + stale-stop check** (mode-independent, runs in
   PAPER too) — every cycle, before anything else, checks whether the
   recorded `sell_stop_id` for each managed position is *actually still
   resting* at the broker (`_has_live_stop`). If it's vanished — manual
   cancel, a DAY-validity expiry, an unseen rejection — it's treated exactly
   like "no stop was ever recorded," and a fresh one is re-armed immediately
   (unless the price has already blown through the last known SL — see the
   comment at that check-site in `_exit_pass`, which routes straight to a
   fallback market-sell instead of arming a doomed stop behind the market).

Why not just trust `feed()`? Because a *tag* not echoing back, or a
transient API hiccup, is not hypothetical — it's exactly the kind of thing
that shows up once a strategy has real money behind it and never showed up
in a demo. Layering these three costs a small amount of extra API calls
(all cached/cheap) in exchange for "the position was never left silently
naked for more than one cycle," which is the whole point of live mode.

This is also *why* it's safe for `_exit_pass` to persist an SL-engine state
mutation (e.g. `qty_remaining` after a partial book) **before** its
corresponding broker call is confirmed: if that broker call then fails
partway, `_reconcile_live`'s qty-drift resync (LIVE) reconciles the mismatch
against the broker's real position on the very next cycle. Persist-then-net
is a documented tradeoff, not an oversight — see the comment directly above
`ps["state"] = asdict(st)` in `_exit_pass`.

## 4. Catch-and-alert instead of raise-and-abort

The original `fix/live-risk-flash-test` branch let a failed
`modify_stop`/`market_sell`/`cancel` raise straight out of `cycle()`, relying
on `run.py`'s bare `except Exception: print(...)` to catch it — meaning a
broken trailing stop failed with a console print and **no Telegram alert**,
since the exception never reached `bot._alert()`. That's a silent-failure
gap: exactly the kind of thing you don't notice until you check the account
balance days later.

This branch catches these failures *inside* `_exit_pass`, fires
`self._alert("error", ...)`, and lets the position self-heal on a later
cycle — instead of aborting the whole cycle, which would also have skipped
exit management and entries for every OTHER symbol being managed that
cycle. One broken mutator for one symbol no longer takes down risk
management for everything else. See `tests/test_live_failure.py`'s own
merge-note docstring for the exact behavioral contract this replaced.

The flip side of "don't raise on failure" is `_mutator_ok`/`_cancel_ok`:
a bare `None` result from a LIVE mutator must be treated as a **failure**,
never success — a real `LiveBroker` mutator always returns a dict or raises,
so a `None` in LIVE means an exception got swallowed somewhere below, and
silently accepting that as "it worked" is the actual gap.

## 5. What is verified vs. NOT verified against real Fyers

Everything above has been proven against `tests/` (33 mocked
`LiveBrokerDouble` tests) and `test_live_fixes.py` (37 tests exercising the
REAL `broker.py`/`fyers_client.py` against a `FakeFyers` double) — but a
double is only as honest as its author. The following are still assumptions
about Fyers's actual v3 API behavior, not proven facts:

- That the combined qty+trigger `modify_order` payload (`{"id", "type",
  "limitPrice", "stopPrice", "qty"}`) is actually accepted for an SL-M order,
  rather than rejected (which would just mean the atomic path always falls
  back to cancel-then-replace — safe, just not the "zero window" ideal case).
- That Fyers echoes back the `orderTag` we send on a filled order exactly as
  sent, in the orderbook field `feed()` expects — this is what lets `feed()`
  recognize "this fill is ours" without needing to also match by id.
- The exact orderbook JSON field names `feed()`/`_orderbook()` parse
  (`orderTag`, `tradedPrice`, `status` codes, etc.) — these were written from
  documentation and the original branch's own notes, not a captured real
  response.

**How to verify, in order, before trusting this with real money:**

1. `python test_live_order.py` — places a real but non-filling LIMIT order
   (5% below market), inspects the orderbook response shape, modifies it,
   cancels it. Paste the printed JSON back to whoever/whatever is
   maintaining this so the field names above can be confirmed or corrected.
   (This exercises plain order mechanics, not the SL-M-specific
   `modify_stop_qty` path — a dedicated SL-M place/modify/cancel dry run
   would be a good addition and does not exist yet.)
2. A single-symbol `MAX_POSITIONS=1` LIVE session during market hours, with
   `ALERTS=true` so every state transition pings Telegram, watched in
   real time against the Fyers app's own order/position screens.
3. Only after (1) and (2) look right: raise `MAX_POSITIONS` gradually.

## 6. TOTP auto-login (`auth.py`)

`auth.py` now supports two ways to get `token.json`:

- **Manual** (unchanged, always available): open a URL, log in in a browser,
  paste back the `auth_code`. Zero risk beyond the existing Fyers app
  credentials, but needs a human every trading morning.
- **TOTP auto-login** (new, strictly opt-in via `FYERS_ID` /
  `FYERS_TOTP_SECRET` / `FYERS_PIN` in `.env`): replays the same four HTTP
  requests Fyers's own web login page makes
  (`send_login_otp_v2` → `verify_otp` → `verify_pin_v2` → the authorization
  `token` redirect that carries `auth_code`), generating the TOTP code from
  `FYERS_TOTP_SECRET` with a hand-written RFC 6238 implementation (no new
  dependency) instead of a human typing it in.

**This is explicitly NOT a documented Fyers API.** It's a reverse-engineered
replay of internal web-login plumbing, sourced from and cross-checked
against the community reference implementation at
`github.com/tkanhe/fyers-api-access-token-v3`. Fyers can change these
endpoints or payload shapes at any time without notice — this is the kind of
thing that's genuinely useful right up until it silently stops working, so
`totp_login()`/`get_token_totp()` raise `TotpLoginError` loudly on any
unexpected response rather than ever returning a falsy/partial token.
`run.py`'s own `_ensure_fresh_token()` only calls this automatically when
the TOTP env vars are actually set — leave them blank and nothing about
`run.py`'s or `auth.py`'s existing behavior changes.

`FYERS_TOTP_SECRET` and `FYERS_PIN` are as sensitive as a password: `.env`
only, gitignored, never logged, never echoed back in an alert.

## 7. New `.env` knobs from this branch

- `MAX_POSITIONS` — cap on concurrent open positions (0 = unlimited). Start
  at 1 for the first live session (see §5).
- `ORDER_TAG` — the Fyers `orderTag`/Algo-ID used on every order this bot
  places, and what `feed()`/reconciliation use to recognize "ours." Keep it
  under 20 characters (Fyers's cap).
- `ALLOW_T2T` — T2T ("BE"-series) stocks are delivery-only; a same-day SL-M
  stop physically cannot fill intraday for them. `run.py`'s
  `load_watchlist()` now skips any symbol in `fyers_client.SYMBOL_OVERRIDES`
  (the known BE-series list) unless this is set — a silent trap otherwise,
  since the bot would happily arm/track one and its "protective" stop would
  just never behave the way it does for a normal `-EQ` stock.
- `FYERS_ID` / `FYERS_TOTP_SECRET` / `FYERS_PIN` — see §6.

`run.py` also now: uses a separate `bot_state_<mode>.json` per MODE (so a
PAPER run's tracked positions/stop-ids can never bleed into a LIVE run
against the same working directory or vice versa), and runs a funds
preflight (`client.funds()`) before entering the LIVE loop so a bad/expired
token fails loudly at startup instead of on the first real order placement.

## 8. Test suite map

- `tests/` (pytest, 33 tests) — mocked `LiveBrokerDouble` from
  `tests/conftest.py`. Fast, no real broker layer involved; exercises
  `bot.py`'s decision logic and failure handling in isolation. Several of
  these hand-set `pos_state["sell_stop_id"]` directly without registering a
  matching entry in `double.pending` — when a test does this, it MUST also
  append to `double.pending` if it wants `_has_live_stop` (and therefore the
  stale-stop self-heal check) to see that stop as resting; several tests
  have a comment at the point of doing this precisely because the stale-stop
  check (added in this branch) will otherwise self-heal a "phantom" stop
  before the test's real assertion path ever runs.
- `test_live_fixes.py` (standalone, 37 tests) — a `FakeFyers` double that
  exercises the REAL `broker.py`/`fyers_client.py`, not a bot-level mock.
  Closer to the metal; this is what would catch a bug in, say, the
  `modify_order` payload shape itself (within the limits of `FakeFyers`
  being an honest-but-unverified stand-in for real Fyers — see §5).
- `test_live_order.py` — the one test that touches real Fyers. Manual,
  interactive, asks for a typed `YES`, places a real but harmless order.

Run all three before trusting a change:
```
pytest tests/ -q
python test_live_fixes.py
python bot.py            # paper-mode regression: should end with realized P&L 990.0
```
