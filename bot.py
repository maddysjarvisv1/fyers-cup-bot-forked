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


def log(msg):
    print(msg, flush=True)


class CupBot:
    def __init__(self, broker, price_source, watchlist, state_path="bot_state.json",
                 near_pct=NEAR_PCT, verbose=True, mode="PAPER", alerts=False, tag="cup"):
        self.broker = broker
        self.tag = tag                       # order tag / Algo-ID identifier
        self.price_source = price_source     # callable(symbols)->{SYM: ltp}
        self.watchlist = watchlist           # [{symbol, rim, trigger, qty}]
        self.near = near_pct
        self.state_path = state_path
        self.verbose = verbose
        self.mode = mode                     # "PAPER" | "LIVE" (for alert tags)
        self.alerts = alerts                 # send Telegram alerts?
        self.pos_state = {}                  # SYM -> {"state": {...}, "sell_stop_id": id}
        if os.path.exists(state_path):
            self.pos_state = json.load(open(state_path))

    def _alert(self, fn, *a, **k):
        if self.alerts:
            import notify
            getattr(notify, fn)(self.mode, *a, **k)

    def _save(self):
        json.dump(self.pos_state, open(self.state_path, "w"), indent=2)

    def _syms(self):
        s = {w["symbol"] for w in self.watchlist}
        s |= {p["symbol"] for p in self.broker.positions()}
        return list(s)

    def cycle(self):
        prices = self.price_source(self._syms())
        fills = self.broker.feed(prices)          # paper: process resting stops
        self._on_fills(fills)
        self._entry_pass(prices)
        self._exit_pass(prices)
        self._save()
        return prices, fills

    # -- entry: arm buy-stop only for near-rim, uncrossed, not-already-handled --
    def _entry_pass(self, prices):
        have = {p["symbol"] for p in self.broker.positions()}
        have |= {o["symbol"] for o in self.broker.open_orders()}
        for w in self.watchlist:
            sym, rim, trig, qty = w["symbol"], float(w["rim"]), float(w["trigger"]), int(w["qty"])
            if sym in have:
                continue
            ltp = prices.get(sym)
            if ltp is None or ltp >= trig or ltp < rim * (1 - self.near):
                continue
            self.broker.place_buy_stop(sym, qty, trig, order_tag=self.tag)
            if self.verbose:
                log(f"  ARM   {sym}: ltp {ltp} within 1% of rim {rim} -> buy-stop @ {trig} x{qty}")
            self._alert("armed", sym, ltp, trig, qty)

    # -- react to fills: entry -> start SL mgmt; stop-exit -> cleanup --
    def _on_fills(self, fills):
        for f in fills:
            sym = f["symbol"]
            if f["reason"] == "ENTRY":
                st = sl.PosState(entry=f["price"], qty_total=f["qty"], qty_remaining=f["qty"])
                sid = self.broker.place_sell_stop(sym, f["qty"], st.sl, order_tag=self.tag)
                self.pos_state[sym] = {"state": asdict(st), "sell_stop_id": sid}
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

    # -- exit mgmt: run SL engine, trail the resting sell-stop, book partials --
    def _exit_pass(self, prices):
        for pos in self.broker.positions():
            sym = pos["symbol"]
            ps = self.pos_state.get(sym)
            ltp = prices.get(sym)
            if not ps or ltp is None:
                continue
            st = sl.PosState(**ps["state"])
            prev_sl, prev_phase = st.sl, st.phase
            st, actions = sl.step(st, ltp)

            booked = [a for a in actions if a[0] == "book_partial"]
            if booked:
                q, px = booked[0][1], booked[0][2]
                self.broker.market_sell(sym, q, px)
                self.broker.cancel(ps["sell_stop_id"])
                ps["sell_stop_id"] = self.broker.place_sell_stop(sym, st.qty_remaining, st.sl, order_tag=self.tag)
                if self.verbose:
                    log(f"  BOOK  {sym} +20%: sold {q} @ {px}; trail SL -> {st.sl}, keep {st.qty_remaining}")
                self._alert("partial", sym, q, px, st.sl, st.qty_remaining)
            elif st.sl != prev_sl:
                self.broker.modify_stop(ps["sell_stop_id"], st.sl)
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
