"""run_paper.py — PAPER-ONLY intraday tracker for the 'open==low green 5-min bar'
ATM index-option strategy (NIFTY + BANKNIFTY).

READ-ONLY market data: pulls ATM option symbols live, builds 5-min bars from
30s ltp polls, prints every NEW finalized bar + a 'SIGNAL ...' line when the
trigger fires, and tracks OPEN paper trades across the three exit modes to a
JSONL ledger.

NO ORDERS. NO FUNDS MOVEMENT. NO external sends. A true paper tracker: it only
ever calls client.ltps / client.quotes (read-only) and writes a local ledger.

Usage (all read-only):
    .venv/bin/python run_paper.py                    # track NIFTY + BANKNIFTY ATM CE/PE
    .venv/bin/python run_paper.py --symbols=NIFTY    # NIFTY only (both sides)
    .venv/bin/python run_paper.py --symbols=NIFTY:CE # NIFTY CE only
    DRY_RUN=1 .venv/bin/python run_paper.py --polls=3 --poll=30   # smoke, 3 cycles
    .venv/bin/python run_paper.py --polls=3 --poll=30            # idle-marker smoke
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import force_ipv4  # noqa: must stay first (Fyers IPv4 egress)

from fyers_client import FyersClient
from intraday_paper.bars import BarBuilder, current_bar
from intraday_paper.symbols import resolve_atm_symbols, SPOT_SYMBOL, OPTION_UNDERLYING
from intraday_paper.tracker import PaperTracker

IST = timezone(timedelta(hours=5, minutes=30))


def is_market_hours(now_ist=None):
    now = now_ist or datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def parse_symbol_filter(tokens):
    """'NIFTY', 'NIFTY:CE', 'BANKNIFTY:PE', 'NIFTY,BANKNIFTY' -> set of
    (underlying, side) pairs to track. Empty set => track everything."""
    want = set()
    for tok in (tokens or []):
        tok = tok.strip().upper()
        if not tok:
            continue
        if ":" in tok:
            u, sd = tok.split(":", 1)
            for side in (sd,):
                if side in ("CE", "PE"):
                    want.add((u, side))
        elif tok in ("CE", "PE"):
            for u in ("NIFTY", "BANKNIFTY"):
                want.add((u, tok))
        elif tok in ("NIFTY", "BANKNIFTY"):
            want.add((tok, "CE")); want.add((tok, "PE"))
    return want


def build_track_symbols(resolved):
    """Flatten resolve_atm_symbols() output -> list of {underlying, side, symbol,
    spot, strike, ltp} that verified ltp>0."""
    out = []
    for u, info in resolved.items():
        for side, sym in (("CE", info["ce"]), ("PE", info["pe"])):
            if not sym:
                continue
            ltp = None
            for pool in (info["weeklies"], info["monthlies"]):
                if sym in pool:
                    ltp = pool[sym]
            out.append({"underlying": u, "side": side, "symbol": sym,
                        "spot": info["spot"], "strike": info["strike"], "ltp": ltp})
    return out


def render_bar_row(bb, sym, resolved_ltp):
    cb = current_bar(bb.bar_map, sym)
    if not cb:
        return None
    hist = bb.finalized(sym)
    last = hist[-1] if hist else None
    line = f"  {sym:<26s} spot~{resolved_ltp if resolved_ltp else '--':>8}"
    if last:
        line += (f"  [finalized #{len(hist)}] O {last['open']:<8.1f} H {last['high']:<8.1f} "
                 f"L {last['low']:<8.1f} C {last['close']:<8.1f}")
    line += f"  [live] O {cb['open']:<8.1f} H {cb['high']:<8.1f} L {cb['low']:<8.1f} C {cb['close']:<8.1f}"
    return line


def main():
    ap = argparse.ArgumentParser(description="Paper intraday ATM-option tracker (read-only)")
    ap.add_argument("--symbols", default=None,
                    help="comma list filter, e.g. NIFTY / NIFTY:CE / BANKNIFTY:PE (default: all)")
    ap.add_argument("--poll", type=int, default=30, help="poll interval seconds (default 30)")
    ap.add_argument("--polls", type=int, default=0, help="run N poll cycles then exit (0=forever)")
    ap.add_argument("--ledger", default="intraday_paper/ledger.jsonl")
    ap.add_argument("--sl-points", type=float, default=2.0)
    ap.add_argument("--cost-points", type=float, default=2.0)
    args = ap.parse_args()

    dry = os.getenv("DRY_RUN", "0") == "1"
    mode_tag = "[DRY]" if dry else "[PAPER]"

    print(f"{mode_tag} paper tracker start | poll {args.poll}s | "
          f"sl {args.sl_points}pts | cost {args.cost_points}pts | ledger {args.ledger}")

    # ---- load client (read-only usage; token.json must exist/fresh) ----
    client = FyersClient.from_files()

    # ---- resolve & verify live ATM symbols ----
    print("resolving live ATM symbols (read-only quotes)...")
    resolved = resolve_atm_symbols(client)
    for u, info in resolved.items():
        print(f"  {u}: spot={info['spot']:.1f} strike={info['strike']} "
              f"CE={info['ce']} PE={info['pe']} "
              f"(weeklies={len(info['weeklies'])}, monthlies={len(info['monthlies'])})")

    tracked = build_track_symbols(resolved)
    flt = parse_symbol_filter((args.symbols or "").split(","))
    if flt:
        tracked = [t for t in tracked if (t["underlying"], t["side"]) in flt]
    if not tracked:
        print("ERROR: no tracked symbols (filter matched nothing). Exiting.")
        return 1
    syms = [t["symbol"] for t in tracked]
    print(f"tracking {len(syms)} symbols: {', '.join(syms)}")

    bb = BarBuilder(bar_seconds=300)
    tracker = PaperTracker(ledger_path=args.ledger,
                           sl_points=args.sl_points,
                           cost_in_points=args.cost_points)
    last_ltp = {}
    seen_signals = set()
    cycle = 0

    try:
        while True:
            cycle += 1
            now = datetime.now(IST)
            stamp = now.strftime("%H:%M:%S")

            if not is_market_hours(now):
                tag = f"[{stamp}] market closed — idle"
                if args.polls:
                    print(tag);  break
                print(tag)
                time.sleep(args.poll)
                continue

            # ---- read-only poll of the ATM option symbols ----
            ltps = client.ltps(syms)      # {plain_core: ltp}
            # client.ltps keys are plain()d; map back to full symbol via tracked
            ltp_by_symbol = {}
            for t in tracked:
                seg = t["symbol"].split(":")[-1]
                plain = seg.rsplit("-", 1)[0] if "-" in seg else seg
                core = seg
                ltp_by_symbol[t["symbol"]] = (ltps.get(seg) or ltps.get(plain)
                                              or ltps.get(t["symbol"])
                                              or last_ltp.get(t["symbol"]))
            ts = now.timestamp()

            # feed bars; collect newly finalized bars
            new_finalized = []
            for t in tracked:
                ltp = ltp_by_symbol.get(t["symbol"])
                if ltp is None or ltp <= 0:
                    continue
                last_ltp[t["symbol"]] = ltp
                fin = bb.add(t["symbol"], ltp, ts)
                new_finalized.extend(
                    {**fb, "underlying": t["underlying"], "side": t["side"]}
                    for fb in fin if fb["sym"] == t["symbol"]
                )

            # ---- print table ----
            print(f"\n[{stamp}] cycle {cycle} | bars:")
            for t in tracked:
                row = render_bar_row(bb, t["symbol"], ltp_by_symbol.get(t["symbol"]))
                if row:
                    print(row)

            # ---- track finalized bars: signals + exits ----
            events = tracker.on_batch(new_finalized, now_ts=ts)
            for ev in events:
                if ev["event"] == "signal":
                    key = (ev["sym"], ev["slot"])
                    if key in seen_signals:
                        continue
                    seen_signals.add(key)
                    print(f"  >>> SIGNAL  {ev['sym']} slot#{ev['slot']} | "
                          f"entry {ev['entry']} | SL {ev['sl']} | "
                          f"cost {ev['cost_points']}pts  → logged")
                elif ev["event"] == "exit":
                    print(f"  >>> EXIT   {ev['sym']} slot#{ev['slot']} "
                          f"mode={ev['mode']} | exit {ev['exit_price']} | "
                          f"net_pnl {ev['net_pnl']:+.1f}pts | "
                          f"modes: " + ", ".join(f"{k}={v}" for k, v in ev["modes"].items()))
            print(f"  open paper trades: {tracker.open_count()}")

            if args.polls and cycle >= args.polls:
                print(f"[{stamp}] done after {args.polls} cycle(s) (smoke).")
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        print("ledger: " + args.ledger + " (paper fills only)")
        try:
            n = len(open(args.ledger).read().splitlines()) if os.path.exists(args.ledger) else 0
            print(f"ledger entries: {n}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())