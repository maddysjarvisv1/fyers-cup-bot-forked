"""
run.py — live-quote runner for the lean Cup bot.

Reads watchlist.csv, pulls LIVE Fyers quotes, runs the near-gate entry + SL/trail
exit strategy on a loop during NSE market hours. Sends Telegram alerts.

MODE (from .env or env var):
  paper  -> PaperBroker: simulated fills against live quotes (SAFE, default)
  live   -> LiveBroker: REAL Fyers orders. Requires CONFIRM_LIVE=YES guard.

  python run.py

Prereqs: run `python auth.py` first (daily Fyers token -> token.json).
"""
import os
import csv
import time
from datetime import datetime, timezone, timedelta

from fyers_client import FyersClient, load_env
from broker import PaperBroker, LiveBroker
from bot import CupBot
import notify

IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST


def load_watchlist(path="watchlist.csv"):
    wl = []
    with open(path) as f:
        for r in csv.DictReader(f):
            wl.append({"symbol": r["symbol"].strip().upper(),
                       "rim": float(r["rim"]),
                       "trigger": float(r["trigger"]),
                       "qty": int(float(r["qty"]))})
    return wl


def is_market_hours():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def env(key, default):
    cfg = load_env()
    return os.getenv(key, cfg.get(key, default))


def main():
    mode = env("MODE", "paper").lower()
    poll = int(env("POLL_SEC", "30"))
    near = float(env("NEAR_PCT", "1.0")) / 100
    alerts = env("ALERTS", "true").lower() == "true"

    # --- LIVE safety guard: never trade real money by accident ---
    if mode == "live" and env("CONFIRM_LIVE", "").upper() != "YES":
        print("MODE=live but CONFIRM_LIVE is not YES. Refusing to run live.\n"
              "Set CONFIRM_LIVE=YES in .env only when you truly intend real orders.")
        return

    client = FyersClient.from_files()        # needs token.json (run auth.py)
    watchlist = load_watchlist()

    if mode == "live":
        broker, tag = LiveBroker(client), "LIVE"
    else:
        broker, tag = PaperBroker(state_path="paper_state.json"), "PAPER"

    bot = CupBot(broker, client.ltps, watchlist, near_pct=near, mode=tag, alerts=alerts)

    print(f"[{tag}] runner start | {len(watchlist)} stocks | poll {poll}s | near {near*100:.1f}%")
    if alerts:
        notify.send(f"🚀 <b>[{tag}] Cup bot started</b>\n{len(watchlist)} stocks | poll {poll}s | near {near*100:.1f}%")

    try:
        while True:
            if is_market_hours():
                try:
                    prices, fills = bot.cycle()
                    npos = len(broker.positions())
                    norders = len(broker.open_orders())
                    print(f"[{datetime.now(IST):%H:%M:%S}] cycle ok | quotes {len(prices)} | "
                          f"open orders {norders} | positions {npos}")
                except Exception as e:
                    print(f"cycle error: {e}")
            else:
                print(f"[{datetime.now(IST):%H:%M:%S}] market closed — idle")
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        r = broker.report()
        print("\n=== REPORT ===")
        print(f"mode {r.get('mode')} | realized P&L {r.get('realized_pnl')} | "
              f"wins {r.get('sell_wins')} losses {r.get('sell_losses')} | "
              f"open positions {len(r.get('open_positions', []))}")
        if alerts:
            notify.report(tag, r.get("realized_pnl", 0), r.get("sell_wins", 0),
                          r.get("sell_losses", 0), r.get("open_positions", []))


if __name__ == "__main__":
    main()
