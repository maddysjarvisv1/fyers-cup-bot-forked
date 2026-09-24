# Handoff: live-mode fix pack for the Cup-breakout bot

**For:** Hermes (or whichever agent picks this up next)
**Repo:** `maddysjarvisv1/fyers-cup-bot-forked`
**Branch:** `fix/live-mode-merged` (built on top of `fix/live-risk-flash-test`)
**Status:** all automated tests pass; NOT yet run against a real Fyers account — see §5 before enabling real money.

This file is the operator's manual: what changed, exactly where, how to turn
it on, and the precise test sequence that defines "this works." For the
*why* behind each design decision, read `docs/LIVE_MODE_DESIGN.md` in this
same branch — this file stays deliberately short and action-oriented.

---

## 1. One-paragraph summary

Two independent people had separately tried to make this bot live-safe: one
pass fixed the low-level Fyers order/response handling (`fyers_client.py`,
`broker.py`), the other pass fixed the bot's crash-safety and reconciliation
logic (`bot.py`) — but each was built assuming the other one's bugs were
still present. This branch merges both, closes a real gap neither had
caught (a cancelled stop that vanishes without the position's quantity
changing was never detected before), adds TOTP-based auto-login so the bot
doesn't need a human to open a browser every trading morning, and hardens
`run.py` with position caps, funds preflight, and T2T filtering.

---

## 2. Where everything is (file map)

| File | What it's responsible for |
|---|---|
| `fyers_client.py` | Thin wrapper over the Fyers SDK: places/modifies/cancels orders, validates every response (`_ok`, `_order_id`), raises `FyersError` on anything Fyers rejects. |
| `broker.py` | `PaperBroker` (simulated fills, for testing/demo) and `LiveBroker` (real orders). Both expose the same interface so `bot.py` never knows which one it's talking to. `LiveBroker.modify_stop_qty` (line 289) is the trickiest piece — see §4.3. |
| `bot.py` | `CupBot` — the strategy loop. `cycle()` (line 161) is the entry point called every poll. `_exit_pass()` (line 453) is where stop-loss/trail/partial-book/self-heal logic lives. `_reconcile_live()` (line 250) is the LIVE-only safety net. |
| `cup_sl_engine.py` | Pure SL/trail/partial math (`step()`). Untouched by this branch — same rules as always (6% initial SL, breakeven at +10%, book half + trail at +20%, trail 10% below high). |
| `run.py` | The actual runner you execute (`python run.py`). Reads `.env` + `watchlist.csv`, drives the poll loop, sends Telegram alerts. |
| `auth.py` | Gets `token.json`. Manual browser login (default) or TOTP auto-login (opt-in) — see §4.5. |
| `.env.example` | Every config variable, commented. Copy to `.env` and fill in — `.env` is gitignored, never commit it. |
| `docs/LIVE_MODE_DESIGN.md` | The *why* behind every decision below, plus what's verified vs. assumed about real Fyers behavior. |
| `tests/` (pytest) + `test_live_fixes.py` (standalone) | The test suite — see §6. |
| `test_live_order.py` | The one script that touches real Fyers. Manual, asks for typed confirmation, places a harmless non-filling order to sanity-check the API shapes. |

---

## 3. What was actually broken, and what fixed it

### 3.1 Fyers responses were never validated
**Problem:** the original code assumed every Fyers API call succeeded and
just read `resp["id"]` etc. A rejected order (bad symbol, insufficient
margin, price outside band) would return `{"s": "error", ...}` with no
`id`, which the old code would have silently treated as if an order id of
`None` was fine.
**Fix:** `fyers_client.py`'s `_ok()` (line 56) and `_order_id()` (line 67)
check `s == "ok"` and require a real id string, raising `FyersError`
otherwise. Every `place_*`/`modify_order` call goes through these.

### 3.2 `modify_order` sent a payload Fyers would reject
**Problem:** the original modify call was missing/misnamed fields.
**Fix:** `fyers_client.py`'s `modify_order()` (line 162) now builds
`{"id", "type", "limitPrice", "stopPrice"?, "qty"?}` — the shape Fyers v3
actually documents. **This specific payload has not been fired against a
real Fyers account yet** — see §5, step 1.

### 3.3 Naive local time broke day-roll logic
**Problem:** DAY-validity orders expire at NSE close; the original code
used naive local time to detect "is this a new trading day," which is
wrong if the server isn't in IST.
**Fix:** `broker.py` and `bot.py` both define `IST = timezone(timedelta(hours=5, minutes=30))`
and use it everywhere a "what day is it" decision is made.

### 3.4 No rate-limit awareness on the orderbook
**Problem:** every single check re-fetched the full orderbook from Fyers —
fine in a demo, a real problem against per-second rate limits in a live
poll loop.
**Fix:** `LiveBroker._orderbook()` (broker.py line 237) caches for
`ORDERBOOK_TTL = 3.0` seconds, invalidated immediately after any write
(place/modify/cancel), so a mutation is never masked by a stale cache.

### 3.5 Resizing a resting stop had no safe design
**Problem:** shrinking a stop's quantity (e.g. after booking half the
position at +20%) needs either an atomic modify or a cancel-then-replace —
and cancel-then-replace done naively leaves a window with either zero
protection (cancel succeeded, replace hasn't landed yet) or two stops
resting on overlapping quantity (replace fired before the cancel was
confirmed).
**Fix:** `LiveBroker.modify_stop_qty()` (broker.py line 289) tries a single
atomic modify first (no window at all — the ideal case). Only if Fyers
rejects it does it fall back to cancel-then-replace, and even then it
refuses to place the replacement unless the cancel came back **confirmed**
`s == "ok"`. An unconfirmed cancel means: stop, alert, leave the old stop
resting, let next cycle's self-heal sort it out. Never two stops at once.

### 3.6 A cancelled stop with unchanged quantity was invisible
**Problem — the one genuine bug this branch found, not inherited from
either prior effort:** if Fyers (or you, by hand) cancelled a resting
protective stop but the position's quantity hadn't changed, *nothing*
detected it. The old reconciliation only compared quantities; a same-qty
"the stop just isn't there anymore" case fell through every check.
**Fix:** `bot.py`'s `_exit_pass()` (line 453, comment starts "Detect a stop
that vanished") now checks every cycle, in every mode (PAPER too — this
isn't gated to LIVE), whether the recorded stop is *actually still
resting*. If it isn't, it's treated exactly like "no stop was ever placed"
and immediately re-armed — unless price has already fallen through the
last known stop level, in which case it correctly falls through to an
immediate market-sell instead of arming a stop that's already behind the
market.

### 3.7 A modify/sell/cancel failure used to fail *silently*
**Problem:** on the original `fix/live-risk-flash-test` branch, a failed
`modify_stop`/`market_sell`/`cancel` raised all the way out of `cycle()`,
where `run.py`'s bare `except Exception: print(...)` swallowed it with a
console print and **zero Telegram alert**. A broken trailing stop could
fail for days with nobody knowing.
**Fix:** these failures are now caught *inside* `_exit_pass`, fire
`self._alert("error", ...)` (so you get pinged), and the position
self-heals on the next cycle — without aborting the whole cycle, which
would also have starved exit management for every *other* symbol that
cycle. See `_mutator_ok`/`_cancel_ok` (bot.py lines 60/75): a bare `None`
result from a LIVE mutator is treated as a **failure**, never success,
because a real `LiveBroker` call always returns a dict or raises — a
`None` in LIVE means an exception got swallowed somewhere underneath.

### 3.8 Manual daily browser login
**Problem:** `auth.py` required a human to open a URL, log in, and paste
back an `auth_code` every single trading morning.
**Fix:** opt-in TOTP auto-login — see §4.5.

### 3.9 `run.py` had no position cap, no funds check, no T2T guard
**Fix:** see §4.

### 3.10 `test_live_order.py` itself had a bug
**Problem:** the manual real-order verification script called
`modify_order(oid, limitPrice=...)` — `limitPrice` isn't a valid keyword on
the fixed `modify_order()` signature (it's `limit_price`), and it also
relied on `modify_order`'s default `order_type=3` (SL-M), which is wrong
for the plain LIMIT order this script places. It would have crashed the
moment you ran it.
**Fix:** now calls `c.modify_order(oid, limit_price=..., order_type=1)`.

---

## 4. Configuration — what's new in `.env`

Copy `.env.example` to `.env` and fill in. New variables this branch adds:

| Variable | Default | What it does |
|---|---|---|
| `MAX_POSITIONS` | `0` (unlimited) | Caps concurrent open positions. **Set this to `1` for your first live session.** |
| `ORDER_TAG` | `cup` | The Fyers `orderTag`/Algo-ID stamped on every order this bot places, and what reconciliation uses to recognize "this order is ours." Keep it ≤20 characters. |
| `ALLOW_T2T` | `false` | T2T/"BE"-series stocks are delivery-only — a same-day SL-M stop physically cannot fill intraday. `run.py` now skips any watchlist symbol in this category unless you explicitly opt in. |
| `FYERS_ID` | (blank) | Your Fyers **client/login ID** (e.g. `XY01234`) — NOT the same as `FYERS_APP_ID`. Only needed for TOTP auto-login. |
| `FYERS_TOTP_SECRET` | (blank) | The TOTP secret shown in your Fyers profile when you set up 2FA. Only needed for TOTP auto-login. As sensitive as a password. |
| `FYERS_PIN` | (blank) | Your 4-digit Fyers trading PIN. Only needed for TOTP auto-login. As sensitive as a password. |

### 4.5 TOTP auto-login — read this before turning it on

Leave `FYERS_ID` / `FYERS_TOTP_SECRET` / `FYERS_PIN` blank and nothing
changes: you keep running `python auth.py` and pasting an `auth_code`
every morning, exactly as before.

Set all three, and `run.py` will call `auth.get_token_totp()`
automatically whenever `token.json` is missing or not from today —
`auth.py`'s implementation (function `totp_login`, line 88) replays the
same four HTTP calls Fyers's own web login page makes
(`send_login_otp_v2` → `verify_otp` → `verify_pin_v2` → the token/auth_code
redirect), generating the 2FA code itself instead of you typing it in.

**This is not a documented Fyers API.** It's a reverse-engineered replay of
Fyers's internal web-login endpoints, cross-checked against the community
reference at `github.com/tkanhe/fyers-api-access-token-v3`. Fyers can
change this without notice. If it breaks, it raises `TotpLoginError` loudly
— it never silently proceeds without a token. If you'd rather not carry
that risk, just leave the three env vars blank; the manual flow is always
there and always works the same way it always has.

---

## 5. What to verify before trusting this with real money

In order — do not skip ahead:

1. **`python test_live_order.py`** — places one real, non-filling LIMIT
   order 5% below market, inspects the real orderbook JSON, modifies it,
   cancels it. This is the check for §3.2 above: whether Fyers's real
   response field names match what `broker.py`/`fyers_client.py` expect.
   Read the printed JSON. If any field name looks different from what the
   code expects (`orderTag`, `tradedPrice`, order `status` codes), that's
   the thing to fix before going further — not a reason to skip this step.
2. **A single-symbol live session with `MAX_POSITIONS=1`** during market
   hours, `ALERTS=true`, watched in real time against the Fyers app's own
   order/position screens. Confirm: the buy-stop arms and fills correctly,
   the protective sell-stop appears in the Fyers app with the right
   trigger/qty, and a Telegram alert fires for each state transition.
3. Only after (1) and (2) both look correct: raise `MAX_POSITIONS`
   gradually.

---

## 6. Test suite — what "success" means

Three layers. **All three passing is the bar for "this branch works."**
None of them touch real Fyers except `test_live_order.py`, which is
intentionally manual (see §5, step 1).

```bash
# 1. Mocked reconciliation/decision-logic tests (fast, no real broker code)
pytest tests/ -q
# EXPECT: 33 passed

# 2. Tests against the REAL broker.py/fyers_client.py, via a fake Fyers double
python test_live_fixes.py
# EXPECT: "37 passed, 0 failed" / "all good"

# 3. Paper-mode regression — proves nothing about strategy behavior moved
python bot.py
# EXPECT the run to end with exactly:
#   mode PAPER | start Rs 1,000,000.0 | realized P&L Rs 990.0 | sell wins 2 losses 1
```

If you change anything in `bot.py`, `broker.py`, or `fyers_client.py`, **re-run
all three** before calling the change done. If test 3's P&L number changes,
that's a signal the strategy's actual behavior shifted, not just its
plumbing — treat that as a stop-and-investigate, not a number to update.

A quick note if you add or change a test in `tests/`: several existing
tests hand-set `bot.pos_state["SYM"]["sell_stop_id"] = "L00001"` directly,
without that order existing in the mock broker's `double.pending` list. Fix
3.6 above means the bot now checks every cycle whether a recorded stop is
*actually* still resting — so a test that hand-sets an id without
registering a matching entry in `double.pending` will have that stop
treated as vanished and get an unexpected self-heal. Look at the comment
next to `double.pending.append(...)` in e.g.
`tests/test_live_failure.py::test_exit_pass_modify_stop_failure_alerts_and_self_heals`
for the pattern to copy.

---

## 7. What's explicitly NOT done yet

- The combined qty+trigger `modify_order` payload (§3.2 / §4.3) has not
  been confirmed to actually work against a real Fyers account — only
  against the test doubles. §5 step 1 is how you find out.
- There's no dedicated real-order dry run for the SL-M order type
  specifically (`test_live_order.py` places a plain LIMIT order to test
  mechanics). A copy of that script using `order_type=3` would close that
  gap but hasn't been written.
- No push to GitHub from this sandbox was possible (proxy authorization);
  the branch was delivered as a git bundle/patch instead. Confirm the
  branch actually landed on `origin/fix/live-mode-merged` before treating
  any of this as "in the repo."
