"""
CupBot — the lean strategy: near-gate ENTRY + SL/trail EXIT, on any Broker.

Same logic runs in PAPER (PaperBroker) or LIVE (LiveBroker). The strategy never
knows which — it just calls the broker interface.

Run the built-in scripted demo (proves the full lifecycle + report, no market needed):
    python bot.py
"""
import json
import os
from dataclasses import asdict
import cup_sl_engine as sl
from broker import PaperBroker

NEAR_PCT = 0.01   # arm a buy-stop only within 1% of the rim

# === FIX(live-risk): number of consecutive cycles a position must be absent
# from positions() before it is treated as truly gone (guards against a
# transient empty snapshot from an API hiccup wiping state / faking an exit).
GONE_CONFIRM_CYCLES = 2


def log(msg):
    print(msg, flush=True)


# === FIX(live-risk): a live order op failed — surface it so the runner raises,
# rather than silently continuing with a stale/missing stop-loss.
class LiveOrderError(Exception):
    """Raised when a live broker order operation fails."""


# === FIX(live-risk): strict success check for mutators (modify/cancel/sell).
# === FIX(live-risk): R3-4 — None is success ONLY outside LIVE. PaperBroker
# mutators return None; a LiveBroker mutator returns a Fyers dict, so a None
# there means a swallowed exception and MUST be treated as a failure (otherwise
# a stale SL / orphan stop is counted as corrected). In LIVE a dict must carry
# s == "ok" (case-insensitive); a missing/error "s" is a failure.
def _request_ok(result, mode=None):
    """True when a mutator result indicates success."""
    if result is None:
        return mode != "LIVE"
    if isinstance(result, dict):
        return str(result.get("s", "")).lower() == "ok"
    return bool(result)


# === FIX(live-risk): R3-3 — a cancel is only "effectively OK" on success or an
# explicit already-filled / not-found / not-open condition. Anything else must
# NOT be silently dropped, or a still-resting SELL-stop is forgotten and a later
# trigger turns it into a short.
_CANCEL_TOLERABLE_MARKERS = (
    "not found", "no such", "does not exist", "not open",
    "already", "filled", "completed", "cancelled", "canceled",
)


def _cancel_tolerable(result, mode=None):
    """True when a cancel result may be treated as the stop being gone."""
    if _request_ok(result, mode):
        return True
    if isinstance(result, dict):
        msg = str(result.get("message", "")).lower()
        return any(m in msg for m in _CANCEL_TOLERABLE_MARKERS)
    return False


# === FIX(live-risk): normalize a place_* result to a bare order-id string.
# A LIVE place_* must yield a non-empty id; if the broker returns a dict
# carrying "id", extract it (so sell_stop_id is never a whole dict). Returns
# None when no valid id is present (swallowed exception / error dict / empty).
def _extract_order_id(result):
    if isinstance(result, dict):
        # === FIX(live-risk): R3-4 — an explicit error dict must never yield an
        # id (a failed place_* must not be stored as sell_stop_id).
        if str(result.get("s", "")).lower() not in ("", "ok"):
            return None
        result = result.get("id")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


class CupBot:
    def __init__(self, broker, price_source, watchlist, state_path="bot_state.json",
                 near_pct=NEAR_PCT, verbose=True, mode="PAPER", alerts=False, tag="cup",
                 gone_confirm_cycles=GONE_CONFIRM_CYCLES):
        self.broker = broker
        self.tag = tag                       # order tag / Algo-ID identifier
        self.price_source = price_source     # callable(symbols)->{SYM: ltp}
        self.watchlist = watchlist           # [{symbol, rim, trigger, qty}]
        self.near = near_pct
        self.state_path = state_path
        self.verbose = verbose
        self.mode = mode                     # "PAPER" | "LIVE" (for alert tags)
        self.alerts = alerts                 # send Telegram alerts?
        self.gone_confirm_cycles = gone_confirm_cycles
        self.pos_state = {}                  # SYM -> {"state": {...}, "sell_stop_id": id}
        # === FIX(live-risk): track symbols whose entry buy-stop was placed this
        # session but whose fill has not yet produced a position (in-flight).
        # NOTE: intentionally NOT persisted — each cycle it is reconciled from
        # open_orders()/positions(); a stale entry is cleared below (_entry_pass).
        self._in_flight = set()
        # === FIX(live-risk): consecutive cycles each pos_state symbol has been
        # absent from positions(); in-memory only (restart re-confirms, safe).
        self._gone_since = {}
        # === FIX(live-risk): R3-3 — orphan stop ids whose cancel could not be
        # confirmed (sym -> order_id). Retained and retried; never silently
        # forgotten (a resting SELL-stop would open a short on a later trigger).
        self._orphan_stops = {}
        if os.path.exists(state_path):
            self.pos_state = json.load(open(state_path))

    def _alert(self, fn, *a, **k):
        if self.alerts:
            import notify
            getattr(notify, fn)(self.mode, *a, **k)

    def _save(self):
        json.dump(self.pos_state, open(self.state_path, "w"), indent=2)

    # === FIX(live-risk): verify a live broker op result and raise on failure so
    # the runner surfaces it instead of continuing with a stale SL.
    def _check_live(self, result, op, sym):
        # === FIX(live-risk): R3-4 — pass mode so a None mutator result fails in
        # LIVE (swallowed exception) but still succeeds in PAPER.
        if self.mode == "LIVE" and not _request_ok(result, self.mode):
            raise LiveOrderError(f"LIVE {op} failed for {sym}: {result!r}")

    def _syms(self):
        s = {w["symbol"] for w in self.watchlist}
        s |= {p["symbol"] for p in self.broker.positions()}
        return list(s)

    def cycle(self):
        # === FIX(live-risk): always persist state (try/finally) so a live op
        # that raises mid-cycle still writes out any earlier successful mutations
        # (prevents duplicate/partial re-booking after a restart).
        try:
            prices = self.price_source(self._syms())
            fills = self.broker.feed(prices)          # paper: process resting stops
            self._on_fills(fills)
            self._reconcile_live()
            # === FIX(live-risk): R3-2 — run exit management + the self-heal
            # BEFORE entry, so a later entry-side failure can never starve exit
            # protection for an already-naked position.
            self._exit_pass(prices)
            # === FIX(live-risk): R3-2 — entry is isolated per symbol: a rejected
            # buy-stop or broker error alerts and is skipped, it does not abort
            # the cycle after exits have run.
            self._entry_pass_isolated(prices)
            return prices, fills
        finally:
            self._save()

    # === FIX(live-risk): place + verify a sell-stop; returns a normalized id.
    # In LIVE the broker must yield a non-empty id (or a dict with "id"); a None
    # result (swallowed exception) or error is a failure.
    def _place_sell(self, sym, qty, trigger):
        result = self.broker.place_sell_stop(sym, qty, trigger, order_tag=self.tag)
        oid = _extract_order_id(result)
        if oid is None and self.mode == "LIVE":
            raise LiveOrderError(f"LIVE place_sell_stop returned no order id for {sym}: {result!r}")
        return oid

    # === FIX(live-risk): place + verify a buy-stop; same id normalization.
    def _place_buy(self, sym, qty, trigger):
        result = self.broker.place_buy_stop(sym, qty, trigger, order_tag=self.tag)
        oid = _extract_order_id(result)
        if oid is None and self.mode == "LIVE":
            raise LiveOrderError(f"LIVE place_buy_stop returned no order id for {sym}: {result!r}")
        return oid

    # === FIX(live-risk): best-effort cancel used by cleanup; returns True only
    # when the stop is confirmed gone. "already filled / not found / not open"
    # is tolerable and must not raise.
    # === FIX(live-risk): R3-3 — a hard failure or exception is NOT swallowed:
    # the orphan id is retained in _orphan_stops and a CRITICAL alert fires, so
    # the caller can keep the position and retry instead of forgetting a resting
    # SELL-stop (which would open a short on a later trigger).
    def _cancel_tolerant(self, order_id, sym):
        if not order_id:
            return True
        try:
            res = self.broker.cancel(order_id)
        except Exception as e:
            res = e
        if _cancel_tolerable(res, self.mode):
            self._orphan_stops.pop(sym, None)
            return True
        self._orphan_stops[sym] = order_id
        if self.verbose:
            log(f"  CANCEL {sym}: orphan stop {order_id} not cancelled ({res!r}) — retaining for retry")
        self._alert("error", sym, f"cancel failed for orphan stop {order_id}: {res!r}")
        return False

    # === FIX(live-risk): reconcile live positions with local pos_state, since
    # LiveBroker.feed() returns [] and _on_fills never fires in live mode.
    def _reconcile_live(self):
        if self.mode != "LIVE":
            return
        # === FIX(live-risk): a failed/exceptional positions() snapshot is an
        # ERROR, not an empty list — never wipe state or fire a fake EXIT on a
        # network/timeout failure.
        try:
            positions = self.broker.positions()
        except Exception as e:
            if self.verbose:
                log(f"  LIVE positions() failed: {e} — skipping reconcile cleanup")
            self._alert("error", "ALL", f"positions() failed: {e}")
            return
        # positions() already returns only filled net positions, so a symbol that
        # is merely a just-placed-pending buy order (no fill yet) is absent here.
        pos_by_sym = {p["symbol"]: p for p in positions}

        # === FIX(live-risk): only manage strategy symbols (watchlist, in-flight,
        # or already-tracked) — never adopt foreign/manual/holding positions.
        managed = {w["symbol"] for w in self.watchlist} | set(self._in_flight) | set(self.pos_state)

        # NEW POSITION -> ENTRY: create PosState + place the protective sell-stop
        for sym, p in pos_by_sym.items():
            if sym not in managed:
                continue  # foreign/manual position (not this strategy) — leave it alone
            if sym in self.pos_state:
                self._reconcile_qty(sym, p)  # partial-fill qty drift (below)
                continue  # already managed (no duplicate SELL-stop)
            st = sl.PosState(entry=p["avg"], qty_total=p["qty"], qty_remaining=p["qty"])
            sid = self._place_sell(sym, p["qty"], st.sl)
            self.pos_state[sym] = {"state": asdict(st), "sell_stop_id": sid}
            self._in_flight.discard(sym)
            # === FIX(live-risk): persist immediately after this broker mutation,
            # so a later failure in this loop can't lose it (finding 1).
            self._save()
            if self.verbose:
                log(f"  ENTRY {sym} reconciled @ {p['avg']} x{p['qty']} -> initial SL {st.sl} (sell-stop {sid})")
            self._alert("entry", sym, p["avg"], p["qty"], st.sl)

        # POSITION GONE -> STOP_EXIT cleanup (do NOT create a phantom sell-stop)
        for sym in list(self.pos_state):
            ps = self.pos_state.get(sym)
            if not ps:
                continue
            st = ps.get("state") or {}
            if st.get("qty_remaining", 0) <= 0 or sym in pos_by_sym:
                # still held (or already fully booked) — reset the absence counter
                self._gone_since[sym] = 0
                continue
            # === FIX(live-risk): require absence across GONE_CONFIRM_CYCLES, and
            # cancel the orphaned sell-stop before dropping the position, so a
            # transient empty snapshot can't wipe state or leak a short.
            cnt = self._gone_since.get(sym, 0) + 1
            self._gone_since[sym] = cnt
            if cnt < self.gone_confirm_cycles:
                continue  # not yet confirmed gone
            # === FIX(live-risk): R3-3 — cancel the orphan stop FIRST and only
            # forget the position once that stop is confirmed gone (or an
            # already-filled/not-found cancel). A failed cancel is retained (and
            # retried next cycle) instead of silently dropping a resting stop.
            order_id = ps.get("sell_stop_id") or self._orphan_stops.get(sym)
            if order_id and not self._cancel_tolerant(order_id, sym):
                ps["sell_stop_id"] = order_id
                ps["_need_cancel"] = True
                self._save()
                continue  # keep the position; do NOT fire a fake EXIT
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

    # === FIX(live-risk): repair partial-fill qty drift — if the broker's net
    # position qty no longer matches qty_remaining, re-sync and re-place the
    # protective stop at the correct qty (60 of 100 shares were unprotected).
    # === FIX(live-risk): R3-1 — cancel the OLD stop BEFORE placing the new one.
    # Never leave two SELL-stops resting (40-lot + 100-lot = 140 sold against
    # 100 held -> oversell/short). On a failed cancel do NOT stack: retain the
    # orphan and leave sell_stop_id None so the self-heal owns re-placement.
    def _reconcile_qty(self, sym, p):
        ps = self.pos_state.get(sym)
        if not ps:
            return
        st = sl.PosState(**ps["state"])
        if p["qty"] == st.qty_remaining:
            return
        st.qty_remaining = int(p["qty"])
        if st.qty_remaining > st.qty_total:
            st.qty_total = st.qty_remaining
        # persist the resynced qty first, so a failure below cannot lose it
        ps["state"] = asdict(st)
        self._save()
        old_id = ps.get("sell_stop_id")
        if old_id:
            try:
                res = self.broker.cancel(old_id)
            except Exception as e:
                res = e
            if not _cancel_tolerable(res, self.mode):
                # === FIX(live-risk): R3-1 — cancel failed: do NOT place a second
                # stop. Keep the orphan for retry and leave the SL missing so the
                # exit-pass self-heal can finish the job; alert CRITICAL.
                self._orphan_stops[sym] = old_id
                ps["sell_stop_id"] = None
                ps["_need_cancel"] = old_id
                self._save()
                if self.verbose:
                    log(f"  SYNC  {sym} qty drift -> {st.qty_remaining}: cancel {old_id} failed "
                        f"({res!r}) — deferring stop re-place")
                self._alert("error", sym, f"qty-drift cancel failed for {old_id}: {res!r}")
                return
            ps["sell_stop_id"] = None
            self._save()
        sid = self._place_sell(sym, st.qty_remaining, st.sl)
        ps["sell_stop_id"] = sid
        ps.pop("_need_cancel", None)
        self._orphan_stops.pop(sym, None)
        self._in_flight.discard(sym)
        self._save()
        if self.verbose:
            log(f"  SYNC  {sym} qty drift -> {st.qty_remaining} (sell-stop {sid})")

    # -- entry: arm buy-stop only for near-rim, uncrossed, not-already-handled --
    # === FIX(live-risk): R3-2 — gather held/pending symbols; raises on a broker
    # query error so the caller can isolate it.
    def _entry_have(self):
        have = {p["symbol"] for p in self.broker.positions()}
        have |= {o["symbol"] for o in self.broker.open_orders()}
        # === FIX(live-risk): _in_flight is intentionally NOT persisted; clear
        # any symbol whose buy-stop is no longer an open order AND produced no
        # position (it was cancelled/rejected/expired) so it can re-arm.
        for sym in list(self._in_flight):
            if sym not in have:
                self._in_flight.discard(sym)
        return have

    # === FIX(live-risk): R3-2 — entry for a single watchlist symbol; raises on
    # failure (callers decide whether to isolate).
    def _entry_one(self, w, have, prices):
        sym, rim, trig, qty = w["symbol"], float(w["rim"]), float(w["trigger"]), int(w["qty"])
        # === FIX(live-risk): re-arm guard — skip symbols already held, in
        # pos_state, or whose fill is still in-flight (no duplicate buy-stop).
        if sym in have or sym in self.pos_state or sym in self._in_flight:
            return
        ltp = prices.get(sym)
        if ltp is None or ltp >= trig or ltp < rim * (1 - self.near):
            return
        # === FIX(live-risk): verify the buy-stop result BEFORE marking
        # in-flight (a failed arm must not block the symbol nor fire a false
        # ARMED alert).
        sid = self._place_buy(sym, qty, trig)
        if sid is None:
            return
        self._in_flight.add(sym)
        if self.verbose:
            log(f"  ARM   {sym}: ltp {ltp} within 1% of rim {rim} -> buy-stop @ {trig} x{qty}")
        self._alert("armed", sym, ltp, trig, qty)

    def _entry_pass(self, prices):
        have = self._entry_have()
        for w in self.watchlist:
            self._entry_one(w, have, prices)

    # === FIX(live-risk): R3-2 — per-symbol isolation used by cycle(): a rejected
    # buy-stop (margin/circuit), a broker error, or a failed positions()/
    # open_orders() scan is alerted and skipped instead of aborting the cycle
    # (which would starve trailing / partial booking / the naked-position heal,
    # all of which have already run).
    def _entry_pass_isolated(self, prices):
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

    # -- react to fills: entry -> start SL mgmt; stop-exit -> cleanup --
    def _on_fills(self, fills):
        for f in fills:
            sym = f["symbol"]
            if f["reason"] == "ENTRY":
                st = sl.PosState(entry=f["price"], qty_total=f["qty"], qty_remaining=f["qty"])
                sid = self.broker.place_sell_stop(sym, f["qty"], st.sl, order_tag=self.tag)
                self.pos_state[sym] = {"state": asdict(st), "sell_stop_id": sid}
                self._in_flight.discard(sym)
                if self.verbose:
                    log(f"  ENTRY {sym} filled @ {f['price']} x{f['qty']}  -> initial SL {st.sl} (sell-stop {sid})")
                self._alert("entry", sym, f["price"], f["qty"], st.sl)
            elif f["reason"] == "STOP_EXIT":
                entry = (self.pos_state.get(sym, {}).get("state") or {}).get("entry", f["price"])
                pnl = (f["price"] - entry) * f["qty"]
                if self.verbose:
                    log(f"  EXIT  {sym} stop hit @ {f['price']} x{f['qty']}")
                self._alert("exit_", sym, f["price"], f["qty"], pnl)
                self.pos_state.pop(sym, None)
                self._in_flight.discard(sym)

    # -- exit mgmt: run SL engine, trail the resting sell-stop, book partials --
    def _exit_pass(self, prices):
        for pos in self.broker.positions():
            sym = pos["symbol"]
            ps = self.pos_state.get(sym)
            ltp = prices.get(sym)
            if not ps or ltp is None:
                continue
            st = sl.PosState(**ps["state"])
            # === FIX(live-risk): self-heal — if a managed position has no live
            # sell-stop resting (a replace failed after cancel), re-place it so
            # the position is never naked.
            if not ps.get("sell_stop_id"):
                # === FIX(live-risk): R3-1/R3-3 — if the old stop's cancel is
                # still unconfirmed, retry it and DEFER re-placement; never stack
                # a second SELL-stop on top of one that may still be resting.
                orphan = self._orphan_stops.get(sym)
                if orphan and not self._cancel_tolerant(orphan, sym):
                    if self.verbose:
                        log(f"  HEAL  {sym}: old stop {orphan} not confirmed cancelled — deferring re-place")
                    continue
                sid = self._place_sell(sym, st.qty_remaining, st.sl)
                ps["sell_stop_id"] = sid
                ps.pop("_need_cancel", None)
                self._save()
                if self.verbose:
                    log(f"  HEAL  {sym}: re-placed missing sell-stop {sid} @ {st.sl} x{st.qty_remaining}")
                self._alert("error", sym, "re-placed missing protective sell-stop")
            prev_sl, prev_phase = st.sl, st.phase
            st, actions = sl.step(st, ltp)

            booked = [a for a in actions if a[0] == "book_partial"]
            if booked:
                q, px = booked[0][1], booked[0][2]
                # === FIX(live-risk): persist the SL-engine's mutation (reduced
                # qty_remaining) BEFORE the broker ops, so a failure below still
                # leaves the partial recorded (finding 1/3).
                ps["state"] = asdict(st)
                self._save()
                # === FIX(live-risk): cancel the old full-qty stop FIRST, then
                # market-sell the partial, then re-place the reduced stop — the
                # old stop must not rest (and over-sell) during the partial sell.
                res = self.broker.cancel(ps["sell_stop_id"])
                self._check_live(res, "cancel", sym)
                # old stop is now gone — mark SL missing so any later failure in
                # this block leaves a self-healable (not stale) marker.
                ps["sell_stop_id"] = None
                res = self.broker.market_sell(sym, q, px)
                self._check_live(res, "market_sell", sym)
                try:
                    sid = self._place_sell(sym, st.qty_remaining, st.sl)
                except LiveOrderError:
                    # === FIX(live-risk): mark the SL missing and persist BEFORE
                    # re-raising, so a restart (or next-cycle self-heal) re-places
                    # it instead of leaving the position naked.
                    self._save()
                    self._alert("error", sym, "replacement sell-stop failed after partial book")
                    raise
                ps["sell_stop_id"] = sid
                self._save()
                if self.verbose:
                    log(f"  BOOK  {sym} +20%: sold {q} @ {px}; trail SL -> {st.sl}, keep {st.qty_remaining}")
                self._alert("partial", sym, q, px, st.sl, st.qty_remaining)
            elif st.sl != prev_sl:
                # === FIX(live-risk): verify modify_stop in live mode, else a
                # stale SL would silently keep protecting at the old level.
                res = self.broker.modify_stop(ps["sell_stop_id"], st.sl)
                self._check_live(res, "modify_stop", sym)
                if self.verbose:
                    tag = "breakeven" if prev_phase == "ACTIVE" else "trail"
                    log(f"  SL    {sym} {tag}: SL {prev_sl} -> {st.sl} (high {st.highest})")
                if prev_phase == "ACTIVE":
                    self._alert("breakeven", sym, st.sl)
                else:
                    self._alert("trail", sym, st.sl, st.highest)

            ps["state"] = asdict(st)


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
