"""
CupBot — the lean strategy: near-gate ENTRY + SL/trail EXIT, on any Broker.

Same logic runs in PAPER (PaperBroker) or LIVE (LiveBroker). The strategy never
knows which — it just calls the broker interface.

Run the built-in scripted demo (proves the full lifecycle + report, no market needed):
    python bot.py

=== MERGE NOTE (see docs/LIVE_MODE_DESIGN.md for the full write-up) ===
This file merges two independent fix efforts:
  1. broker.py / fyers_client.py fixes: real order-id validation, a working
     modify_order payload, IST timestamps + day-roll, orderbook caching.
  2. This file's reconciliation/crash-safety logic: originally written against
     the UNFIXED broker layer (github.com/.../fix/live-risk-flash-test), which
     is why it never trusted feed() and did everything via positions()-diffing.
Both halves are needed; neither alone is live-safe. See the design doc for why.
"""
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import cup_sl_engine as sl
from broker import PaperBroker

NEAR_PCT = 0.01   # arm a buy-stop only within 1% of the rim
IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST

# Number of consecutive cycles a position must be absent from positions()
# before it is treated as truly gone (guards against a transient empty
# snapshot from an API hiccup wiping state / faking an exit).
GONE_CONFIRM_CYCLES = 2


def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")


def log(msg):
    print(msg, flush=True)


class LiveOrderError(Exception):
    """Raised when a live broker order operation fails outright (not the
    tolerable "already filled / not found" shape cancel() can return)."""


# --- cancel-result classification -------------------------------------------
# cancel() is deliberately best-effort at the fyers_client layer (it never
# raises — see fyers_client.cancel_order): a cancel racing against the order
# having just filled, or against it already being gone, is not our failure.
# Anything else must NOT be silently dropped, or a still-resting SELL-stop is
# forgotten and a later trigger turns it into an accidental short.
_CANCEL_TOLERABLE_MARKERS = (
    "not found", "no such", "does not exist", "not open",
    "already", "filled", "completed", "cancelled", "canceled",
)


def _cancel_ok(result, mode=None):
    if isinstance(result, dict):
        if str(result.get("s", "")).lower() == "ok":
            return True
        msg = str(result.get("message", "")).lower()
        return any(m in msg for m in _CANCEL_TOLERABLE_MARKERS)
    if result is None:
        # PaperBroker.cancel() returns None on success. A real LiveBroker
        # mutator always returns a dict or raises (see broker.py) — a None
        # in LIVE means an exception was swallowed somewhere and must be
        # treated as a failure, not silently accepted.
        return mode != "LIVE"
    return bool(result)


def _mutator_ok(result, mode):
    """True if a non-cancel mutator (modify_stop / market_sell / the atomic
    path of modify_stop_qty) succeeded. Same None-is-LIVE-failure reasoning
    as _cancel_ok above."""
    if result is None:
        return mode != "LIVE"
    if isinstance(result, dict):
        return str(result.get("s", "")).lower() == "ok"
    return bool(result)


class CupBot:
    def __init__(self, broker, price_source, watchlist, state_path="bot_state.json",
                 near_pct=NEAR_PCT, verbose=True, mode="PAPER", alerts=False, tag="cup",
                 max_positions=0, gone_confirm_cycles=GONE_CONFIRM_CYCLES,
                 adopt_watchlist_only=True, tracker=None, capital_budget=None):
        self.broker = broker
        self.tag = tag                       # order tag / Algo-ID identifier
        self.price_source = price_source     # callable(symbols)->{SYM: ltp}
        self.watchlist = watchlist           # [{symbol, rim, trigger, qty}]
        self.near = near_pct
        self.state_path = state_path
        self.verbose = verbose
        self.mode = mode                     # "PAPER" | "LIVE" (for alert tags)
        self.alerts = alerts                 # send Telegram alerts?
        self.max_positions = int(max_positions or 0)      # 0 = unlimited
        self.gone_confirm_cycles = gone_confirm_cycles
        # Only ever adopt/protect positions whose symbol is on the watchlist (or
        # already ours), so the bot cannot slap a stop on stock bought by hand
        # in the same account.
        self.adopt_watchlist_only = bool(adopt_watchlist_only)
        self.tracker = tracker            # optional Tracking.Tracker -> ledger writes
        self.capital_budget = capital_budget  # ₹ cap on total deployed cup notional
        # symbol -> (trigger, qty) so we can price a candidate's notional
        self._wat = {str(w["symbol"]).upper(): (float(w["trigger"]), int(w["qty"]))
                     for w in watchlist}
        self.pos_state = {}                  # SYM -> {"state": {...}, "sell_stop_id": id}
        # Symbols whose entry buy-stop was placed this session but whose fill
        # has not yet produced a position. Intentionally NOT persisted — each
        # cycle it is reconciled from open_orders()/positions().
        self._in_flight = set()
        # Consecutive cycles each pos_state symbol has been absent from
        # positions(); in-memory only (a restart re-confirms, which is safe).
        self._gone_since = {}
        # Orphan stop ids whose cancel could not be confirmed (sym -> order_id).
        # Retained and retried; never silently forgotten.
        self._orphan_stops = {}
        self._warned_foreign = set()
        self.day = _today()
        if os.path.exists(state_path):
            blob = json.load(open(state_path))
            self.day = blob.pop("_day", self.day)
            self.pos_state = blob
        if self.day != _today():
            # DAY-validity stops died at the close. Forget the ids; the exit-pass
            # self-heal (and, in LIVE, _reconcile_live) re-places them.
            for ps in self.pos_state.values():
                ps["sell_stop_id"] = None
            self.day = _today()
            log("  new trading day — stored stop ids cleared, stops will be re-placed")

    def _watch_syms(self):
        return {w["symbol"].upper() for w in self.watchlist}

    def _managed(self):
        """Symbols this bot may adopt/protect: the watchlist, anything with a
        buy-stop in flight, and anything already tracked. Everything else —
        stock bought by hand in the same account — is left alone."""
        if not self.adopt_watchlist_only:
            return None   # sentinel: caller should treat every symbol as managed
        return self._watch_syms() | set(self._in_flight) | set(self.pos_state)

    def _alert(self, fn, *a, **k):
        if self.tracker is not None:
            self.tracker.on_alert(fn, *a)
        if not self.alerts:
            return
        import notify
        if fn == "send":
            notify.send(*a, **k)
            return
        getattr(notify, fn)(self.mode, *a, **k)

    def _save(self):
        blob = dict(self.pos_state)
        blob["_day"] = self.day
        json.dump(blob, open(self.state_path, "w"), indent=2)

    def _syms(self):
        s = {w["symbol"] for w in self.watchlist}
        s |= {p["symbol"] for p in self.broker.positions()}
        return list(s)

    def cycle(self):
        # Always persist (try/finally), so a live op that raises mid-cycle still
        # writes out any earlier successful mutation this cycle (prevents a
        # restart from duplicating or re-booking work already done).
        try:
            prices = self.price_source(self._syms())
            fills = self.broker.feed(prices)          # accurate fill price, tag-based
            self._on_fills(fills)
            self._reconcile_live()                    # LIVE-only safety net (see broker.py)
            # Exit management (+ self-heal) runs BEFORE entry, so a later
            # entry-side failure can never starve exit protection for an
            # already-open position.
            self._exit_pass(prices)
            # Entry is isolated per symbol: a rejected buy-stop or broker error
            # alerts and is skipped, it does not abort the cycle after exits
            # have already run.
            self._entry_pass_isolated(prices)
            return prices, fills
        finally:
            self._save()

    # -- verified order placement: raises LiveOrderError on a confirmed failure.
    # fyers_client.py already raises FyersError on a rejected place_* and never
    # returns a falsy id — but a falsy id in LIVE with no exception (a swallowed
    # error somewhere in a broker double, or a future regression) must not be
    # silently accepted as success either, so it's checked explicitly here too.
    def _place_sell(self, sym, qty, trigger):
        try:
            sid = self.broker.place_sell_stop(sym, qty, trigger, order_tag=self.tag)
        except Exception as e:
            raise LiveOrderError(f"place_sell_stop failed for {sym}: {e}") from e
        if not sid and self.mode == "LIVE":
            raise LiveOrderError(f"place_sell_stop for {sym} returned no order id")
        return sid

    def _place_buy(self, sym, qty, trigger):
        try:
            sid = self.broker.place_buy_stop(sym, qty, trigger, order_tag=self.tag)
        except Exception as e:
            raise LiveOrderError(f"place_buy_stop failed for {sym}: {e}") from e
        if not sid and self.mode == "LIVE":
            raise LiveOrderError(f"place_buy_stop for {sym} returned no order id")
        return sid

    # -- best-effort cancel; True only when the stop is confirmed gone. A hard
    # failure is retained in _orphan_stops and alerted, never silently dropped
    # (a still-resting SELL-stop would open a short on a later trigger) --
    def _cancel_tolerant(self, order_id, sym):
        if not order_id:
            return True
        try:
            res = self.broker.cancel(order_id)
        except Exception as e:
            res = e
        if _cancel_ok(res, self.mode):
            self._orphan_stops.pop(sym, None)
            return True
        self._orphan_stops[sym] = order_id
        if self.verbose:
            log(f"  CANCEL {sym}: orphan stop {order_id} not cancelled ({res!r}) — retaining for retry")
        self._alert("error", sym, f"cancel failed for orphan stop {order_id}: {res!r}")
        return False

    def _mutate(self, fn, *a, **k):
        """Call a non-cancel mutator (modify_stop / market_sell / modify_stop_qty)
        and raise if its result doesn't indicate success — including a bare
        None in LIVE mode, which real broker.py never returns on success (see
        _mutator_ok): a swallowed exception must not be mistaken for success."""
        res = fn(*a, **k)
        if not _mutator_ok(res, self.mode):
            raise LiveOrderError(f"{fn.__name__} returned {res!r}")
        return res

    def _has_live_stop(self, sym):
        for o in self.broker.open_orders(sym):
            if o.get("side") == "SELL":
                return True
        return False

    # === LIVE-only safety net ================================================
    # broker.feed() is the primary source of ENTRY/STOP_EXIT events (accurate
    # fill price, from the orderbook). This is the secondary net: it diffs
    # positions() against pos_state every cycle, so a fill feed() missed (a tag
    # Fyers didn't echo, an orderbook row that aged out before we polled) is
    # still caught — a new position gets a stop, a position that's genuinely
    # gone gets cleaned up. It is also what makes it safe to persist an
    # SL-engine mutation before its broker call is confirmed (see _exit_pass):
    # any resulting drift between local qty_remaining and the broker's actual
    # qty is corrected here on the very next cycle.
    def _reconcile_live(self):
        if self.mode != "LIVE":
            return
        try:
            positions = self.broker.positions()
        except Exception as e:
            # A failed snapshot is an ERROR, not an empty list — never wipe
            # state or fire a fake EXIT on a network/timeout failure.
            if self.verbose:
                log(f"  LIVE positions() failed: {e} — skipping reconcile cleanup")
            self._alert("error", "ALL", f"positions() failed: {e}")
            return
        pos_by_sym = {p["symbol"]: p for p in positions}

        managed = self._managed()   # None => everything managed (guard disabled)

        # NEW POSITION -> ENTRY: create PosState + place the protective sell-stop
        for sym, p in pos_by_sym.items():
            if managed is not None and sym not in managed:
                if sym not in self._warned_foreign:
                    self._warned_foreign.add(sym)
                    log(f"  SKIP  {sym}: position not on the watchlist and not opened "
                        f"by this bot — left alone (no stop placed)")
                continue
            if sym in self.pos_state:
                self._reconcile_qty(sym, p)   # partial-fill / drift resync
                continue
            st = sl.PosState(entry=p["avg"], qty_total=p["qty"], qty_remaining=p["qty"])
            try:
                sid = self._place_sell(sym, p["qty"], st.sl)
            except LiveOrderError as e:
                log(f"  ERROR {sym}: could not place protective stop: {e}")
                self._alert("error", sym, f"STOP PLACEMENT FAILED: {e}")
                continue
            self.pos_state[sym] = {"state": asdict(st), "sell_stop_id": sid}
            self._in_flight.discard(sym)
            self._save()   # persist immediately — a later failure in this loop can't lose it
            if self.verbose:
                log(f"  ENTRY {sym} reconciled @ {p['avg']} x{p['qty']} -> initial SL {st.sl} (stop {sid})")
            self._alert("entry", sym, p["avg"], p["qty"], st.sl)

        # POSITION GONE -> cleanup (never create a phantom sell-stop for it)
        for sym in list(self.pos_state):
            ps = self.pos_state.get(sym)
            if not ps:
                continue
            st = ps.get("state") or {}
            if st.get("qty_remaining", 0) <= 0 or sym in pos_by_sym:
                self._gone_since[sym] = 0
                continue
            # Require absence across gone_confirm_cycles: a transient empty
            # snapshot must not wipe state or fire a fake EXIT.
            cnt = self._gone_since.get(sym, 0) + 1
            self._gone_since[sym] = cnt
            if cnt < self.gone_confirm_cycles:
                continue
            order_id = ps.get("sell_stop_id") or self._orphan_stops.get(sym)
            if order_id and not self._cancel_tolerant(order_id, sym):
                ps["sell_stop_id"] = order_id
                self._save()
                continue   # keep the position; do NOT fire a fake EXIT
            entry = st.get("entry", 0.0)
            qty = st.get("qty_remaining", st.get("qty_total", 0))
            exit_px = st.get("sl", entry)
            pnl = (exit_px - entry) * qty
            if self.verbose:
                log(f"  EXIT  {sym} reconciled gone @ {exit_px} x{qty}")
            self._alert("exit_", sym, exit_px, qty, pnl)
            self.pos_state.pop(sym, None)
            self._in_flight.discard(sym)
            self._gone_since.pop(sym, None)
            self._save()

    def _reconcile_qty(self, sym, p):
        """Repair partial-fill / drift: if the broker's net qty no longer
        matches qty_remaining, resync and re-protect at the correct qty.
        Never stack a second stop on the old one — shrink/replace via
        modify_stop_qty (atomic-with-fallback; see broker.py), which itself
        refuses to place a second stop if a cancel can't be confirmed."""
        ps = self.pos_state.get(sym)
        if not ps:
            return
        st = sl.PosState(**ps["state"])
        if p["qty"] == st.qty_remaining:
            return
        old_qty = st.qty_remaining
        st.qty_remaining = int(p["qty"])
        if st.qty_remaining > st.qty_total:
            st.qty_total = st.qty_remaining
        ps["state"] = asdict(st)
        self._save()   # resynced qty persisted first — a failure below can't lose it
        old_id = ps.get("sell_stop_id")
        if not old_id:
            # no resting stop to resize — the exit-pass self-heal will place one
            if self.verbose:
                log(f"  SYNC  {sym} qty drift {old_qty}->{st.qty_remaining}: no stop to resize")
            return
        try:
            res = self._mutate(self.broker.modify_stop_qty, old_id, st.qty_remaining, st.sl,
                                symbol=sym, order_tag=self.tag)
        except Exception as e:
            self._orphan_stops[sym] = old_id
            ps["sell_stop_id"] = None
            self._save()
            log(f"  SYNC  {sym} qty drift -> {st.qty_remaining}: stop resize failed ({e}) "
                f"— deferring to self-heal")
            self._alert("error", sym, f"qty-drift stop resize failed: {e}")
            return
        if isinstance(res, dict) and res.get("id"):
            ps["sell_stop_id"] = str(res["id"])
        self._orphan_stops.pop(sym, None)
        self._save()
        if self.verbose:
            log(f"  SYNC  {sym} qty drift -> {st.qty_remaining} (stop {ps['sell_stop_id']})")

    # -- entry -----------------------------------------------------------------
    def _entry_have(self):
        have = {p["symbol"] for p in self.broker.positions()}
        have |= {o["symbol"] for o in self.broker.open_orders()}
        # _in_flight is intentionally not persisted; clear any symbol whose
        # buy-stop is no longer an open order AND produced no position (it was
        # cancelled/rejected/expired) so it can re-arm.
        for sym in list(self._in_flight):
            if sym not in have:
                self._in_flight.discard(sym)
        return have

    def _deployed_notional(self):
        """₹ of cup notional currently committed (filled positions + armed buy-stops).
        Only counts watchlist-symbol exposure (the ₹50k cup budget); unrelated
        account positions (e.g. a NIFTY F&O) are not charged to it."""
        wat = self._watch_syms()
        d = sum(float(p.get("avg", 0)) * int(p.get("qty", 0))
                for p in self.broker.positions() if p.get("symbol") in wat)
        d += sum(self._wat.get(s, (0, 0))[0] * self._wat.get(s, (0, 0))[1]
                 for s in self._in_flight)
        return d

    def _entry_one(self, w, have, prices):
        sym, rim, trig, qty = w["symbol"], float(w["rim"]), float(w["trigger"]), int(w["qty"])
        if sym in have or sym in self.pos_state or sym in self._in_flight:
            return
        ltp = prices.get(sym)
        if ltp is None or ltp >= trig or ltp < rim * (1 - self.near):
            return
        if self.capital_budget and self._deployed_notional() + trig * qty > self.capital_budget:
            if self.verbose:
                log(f"  CAP   {sym}: would deploy ₹{trig*qty:.0f} -> exceeds budget ₹{self.capital_budget:.0f} — no arm")
            self._alert("error", sym, f"capital budget ₹{self.capital_budget:.0f} exhausted — {sym} not armed")
            return
        try:
            sid = self._place_buy(sym, qty, trig)
        except LiveOrderError as e:
            # A failed arm must not block the symbol, nor fire a false ARMED alert.
            log(f"  ARM   {sym}: buy-stop rejected: {e}")
            self._alert("error", sym, f"buy-stop rejected: {e}")
            return
        self._in_flight.add(sym)
        if self.verbose:
            log(f"  ARM   {sym}: ltp {ltp} within 1% of rim {rim} -> buy-stop @ {trig} x{qty}")
        self._alert("armed", sym, ltp, trig, qty)

    def _entry_pass_isolated(self, prices):
        if self.max_positions:
            committed = len(set(self.pos_state) | self._in_flight)
            if committed >= self.max_positions:
                if self.verbose:
                    log(f"  cap reached ({committed}/{self.max_positions}) — no new arms")
                return
        try:
            have = self._entry_have()
        except Exception as e:
            if self.verbose:
                log(f"  ENTRY scan failed: {e} — skipping entries this cycle")
            self._alert("error", "ALL", f"entry scan failed: {e}")
            return
        for w in self.watchlist:
            try:
                self._entry_one(w, have, prices)
            except Exception as e:
                sym = w.get("symbol", "?")
                if self.verbose:
                    log(f"  ENTRY {sym} failed: {e} — continuing (exits already ran)")
                self._alert("error", sym, f"entry failed: {e}")

    # -- react to broker.feed() fills: entry -> start SL mgmt; exit -> cleanup --
    def _on_fills(self, fills):
        for f in fills:
            sym = f["symbol"]
            if f["reason"] == "ENTRY":
                if sym in self.pos_state:
                    continue   # already reconciled (e.g. by _reconcile_live) — no duplicate
                st = sl.PosState(entry=f["price"], qty_total=f["qty"], qty_remaining=f["qty"])
                try:
                    sid = self._place_sell(sym, f["qty"], st.sl)
                except LiveOrderError as e:
                    log(f"  ERROR {sym}: entry filled but stop placement failed: {e}")
                    self._alert("error", sym, f"STOP PLACEMENT FAILED after fill: {e}")
                    # leave it out of pos_state; _reconcile_live's next pass will
                    # see the live position and retry the adoption
                    continue
                self.pos_state[sym] = {"state": asdict(st), "sell_stop_id": sid}
                self._in_flight.discard(sym)
                self._save()
                if self.verbose:
                    log(f"  ENTRY {sym} filled @ {f['price']} x{f['qty']}  -> initial SL {st.sl} (stop {sid})")
                self._alert("entry", sym, f["price"], f["qty"], st.sl)
            elif f["reason"] == "STOP_EXIT":
                entry = (self.pos_state.get(sym, {}).get("state") or {}).get("entry", f["price"])
                pnl = (f["price"] - entry) * f["qty"]
                if self.verbose:
                    log(f"  EXIT  {sym} stop hit @ {f['price']} x{f['qty']}")
                self._alert("exit_", sym, f["price"], f["qty"], pnl)
                self.pos_state.pop(sym, None)
                self._in_flight.discard(sym)
                self._gone_since.pop(sym, None)
                self._save()

    # -- exit mgmt: run SL engine, trail the resting sell-stop, book partials --
    def _exit_pass(self, prices):
        for pos in self.broker.positions():
            sym = pos["symbol"]
            ps = self.pos_state.get(sym)
            ltp = prices.get(sym)
            if not ps or ltp is None:
                continue

            # Detect a stop that vanished WITHOUT a qty change we'd otherwise
            # catch: a manual/exchange cancel, a DAY-validity expiry, or a
            # rejection we didn't see the response for. _reconcile_live's
            # qty-drift check (LIVE-only) can't see this — qty is unchanged,
            # only the resting order is gone — so this check runs in every
            # mode. Treat it exactly like "sell_stop_id was never set": the
            # self-heal block right below re-places it this same cycle.
            if ps.get("sell_stop_id") and not self._has_live_stop(sym):
                if self.verbose:
                    log(f"  HEAL  {sym}: recorded stop {ps['sell_stop_id']} no longer resting — will re-arm")
                ps["sell_stop_id"] = None

            # Self-heal: if a managed position has no live sell-stop resting (a
            # resize/replace failed earlier, the exchange expired a DAY-
            # validity stop overnight, or the check just above caught a stop
            # that silently vanished), re-place it so the position is never
            # left naked longer than one cycle.
            #
            # Skip it, though, when ltp has already breached the recorded SL
            # this same cycle: re-arming a stop AT a price the market has
            # already passed is at best a no-op and at worst a broker
            # rejection (stop price behind the market). Leaving sell_stop_id
            # unset here falls through to the "exits" branch below, which
            # market-sells at the CURRENT ltp instead of a stale trigger.
            if not ps.get("sell_stop_id") and ltp > ps["state"].get("sl", 0):
                orphan = self._orphan_stops.get(sym)
                if orphan and not self._cancel_tolerant(orphan, sym):
                    if self.verbose:
                        log(f"  HEAL  {sym}: old stop {orphan} not confirmed cancelled — deferring re-place")
                    continue
                st0 = sl.PosState(**ps["state"])
                try:
                    sid = self._place_sell(sym, st0.qty_remaining, st0.sl)
                except LiveOrderError as e:
                    log(f"  ERROR {sym}: stop re-placement failed: {e}")
                    self._alert("error", sym, f"UNPROTECTED — stop re-place failed: {e}")
                    continue
                ps["sell_stop_id"] = sid
                self._save()
                if self.verbose:
                    log(f"  HEAL  {sym}: re-placed missing sell-stop {sid} @ {st0.sl} x{st0.qty_remaining}")
                self._alert("error", sym, "re-placed missing protective sell-stop")

            st = sl.PosState(**ps["state"])
            prev_sl, prev_phase = st.sl, st.phase
            prev_state = dict(ps["state"])
            st, actions = sl.step(st, ltp)

            # --- engine says the stop level was breached -----------------------------
            exits = [a for a in actions if a[0] == "exit"]
            if exits:
                qty = int(exits[0][1]) or int(pos.get("qty") or 0)
                if self._has_live_stop(sym):
                    # the broker's resting stop will fill on its own; feed() (or
                    # _reconcile_live) reports it next cycle. Don't fake an exit.
                    log(f"  SL touched {sym} @ {ltp} — broker stop resting, awaiting fill")
                    continue
                log(f"  FALLBACK EXIT {sym}: no resting stop, selling {qty} at market")
                try:
                    self._mutate(self.broker.market_sell, sym, qty, ltp, order_tag=self.tag)
                except Exception as e:
                    log(f"  ERROR {sym}: fallback exit failed: {e}")
                    self._alert("error", sym, f"EXIT FAILED: {e}")
                    continue
                pnl = (ltp - st.entry) * qty
                self._alert("exit_", sym, ltp, qty, pnl)
                self.pos_state.pop(sym, None)
                self._gone_since.pop(sym, None)
                self._save()
                continue

            # --- partial book at +20% -------------------------------------------------
            booked = [a for a in actions if a[0] == "book_partial"]
            if booked:
                q, px = int(booked[0][1]), booked[0][2]
                keep = int(st.qty_remaining)
                # Persist the engine's mutation before the broker call. This is
                # safe ONLY because _reconcile_live's qty-drift resync (LIVE) will
                # correct any mismatch against the broker's real position on the
                # very next cycle if the broker call below fails partway.
                ps["state"] = asdict(st)
                self._save()
                try:
                    res = self._mutate(self.broker.modify_stop_qty, ps["sell_stop_id"], keep, st.sl,
                                       symbol=sym, order_tag=self.tag)
                except Exception as e:
                    # Nothing changed at the broker (modify_stop_qty only mutates
                    # after a confirmed cancel on its fallback path) — the OLD
                    # stop is still resting at the OLD (full) qty, which still
                    # matches the OLD (unsold) holding. Abort the sell this cycle;
                    # LIVE's qty-drift resync will reconcile state either way.
                    log(f"  ERROR {sym}: partial-book stop resize failed: {e} — not selling this cycle")
                    self._alert("error", sym, f"PARTIAL BOOK STOP RESIZE FAILED: {e}")
                    ps["state"] = prev_state
                    self._save()
                    continue
                if isinstance(res, dict) and res.get("id"):
                    ps["sell_stop_id"] = str(res["id"])
                try:
                    self._mutate(self.broker.market_sell, sym, q, px, order_tag=self.tag)
                except Exception as e:
                    # Stop is already resized to `keep` — the booked qty is
                    # briefly unprotected (documented tradeoff; see design doc)
                    # until this retries. Do NOT restore the old stop size: the
                    # position truly is down to `keep` in intent, and LIVE's
                    # qty-drift resync will confirm/correct against the broker.
                    log(f"  ERROR {sym}: partial-book market-sell failed: {e} — will retry")
                    self._alert("error", sym, f"PARTIAL BOOK SELL FAILED (stop already resized): {e}")
                    self._save()
                    continue
                self._save()
                if self.verbose:
                    log(f"  BOOK  {sym} +20%: sold {q} @ {px}; trail SL -> {st.sl}, keep {keep}")
                self._alert("partial", sym, q, px, st.sl, keep)

            # --- breakeven / trail ----------------------------------------------------
            elif st.sl != prev_sl and ps.get("sell_stop_id"):
                try:
                    self._mutate(self.broker.modify_stop, ps["sell_stop_id"], st.sl)
                except Exception as e:
                    # Alert BEFORE the state marks the stop missing — this is the
                    # gap the original flash-test branch had: a modify failure
                    # here used to raise straight to run.py's bare print with no
                    # Telegram ping, so a broken trail would fail completely
                    # silently in production.
                    log(f"  ERROR {sym}: SL modify failed: {e} — will re-place next cycle")
                    self._alert("error", sym, f"SL MODIFY FAILED (will self-heal next cycle): {e}")
                    ps["sell_stop_id"] = None
                    self._save()
                else:
                    if self.verbose:
                        label = "breakeven" if prev_phase == "ACTIVE" else "trail"
                        log(f"  SL    {sym} {label}: SL {prev_sl} -> {st.sl} (high {st.highest})")
                    if prev_phase == "ACTIVE":
                        self._alert("breakeven", sym, st.sl)
                    else:
                        self._alert("trail", sym, st.sl, st.highest)

            ps["state"] = asdict(st)
            self._save()


# ---------------------------------------------------------------------------
# Scripted price source — advances one tick per call. For the demo/report.
# ---------------------------------------------------------------------------
class ScriptedFeed:
    def __init__(self, paths):
        self.paths = {k.upper(): list(v) for k, v in paths.items()}
        self.i = 0

    def __call__(self, symbols):
        out = {}
        for s in self.paths:
            seq = self.paths[s]
            out[s] = float(seq[min(self.i, len(seq) - 1)])
        self.i += 1
        return out

    def ticks(self):
        return max(len(v) for v in self.paths.values())


def _demo():
    # Two names: WINNER runs the full lifecycle; LOSER stops out at -6%.
    watchlist = [
        {"symbol": "WINNER", "rim": 100, "trigger": 104, "qty": 100},
        {"symbol": "LOSER",  "rim": 200, "trigger": 208, "qty": 50},
    ]
    feed = ScriptedFeed({
        # WINNER: near rim -> breakout -> +10% (BE) -> +20% (book half) -> +25% -> pullback -> trail-exit
        "WINNER": [99, 101, 104, 108, 114.4, 120, 124.8, 130, 128, 122, 116, 112, 110],
        # LOSER: near rim -> breakout -> immediately fades to -6% stop
        "LOSER":  [199, 205, 208, 206, 202, 198, 195, 195, 195, 195, 195, 195, 195],
    })
    broker = PaperBroker(state_path="paper_state.json", reset=True)
    bot = CupBot(broker, feed, watchlist, state_path="bot_state.json")

    log("=== PAPER-MODE SCRIPTED LIFECYCLE TEST ===")
    log("WINNER rim 100 / trig 104 / 100sh   |   LOSER rim 200 / trig 208 / 50sh\n")
    for _ in range(feed.ticks() + 1):
        prices, _ = bot.cycle()
        log(f"tick px: WINNER {prices.get('WINNER')}  LOSER {prices.get('LOSER')}")

    log("\n" + "=" * 60)
    log("FINAL REPORT")
    log("=" * 60)
    r = broker.report()
    log(f"mode {r['mode']} | start Rs {r['start_capital']:,} | realized P&L Rs {r['realized_pnl']:,} "
        f"| sell wins {r['sell_wins']} losses {r['sell_losses']}")
    log("\nTrade log:")
    for t in r["trades"]:
        log(f"  {t['t']}  {t['side']:4} {t['symbol']:7} {t['qty']:>4} @ {t['price']:>8}  "
            f"{t['reason']:10} pnl {t['pnl']:>8}")
    log(f"\nopen positions: {r['open_positions']}")


if __name__ == "__main__":
    _demo()
