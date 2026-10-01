"""
run.py — live-quote runner for the lean Cup bot.

Reads watchlist.csv, pulls LIVE Fyers quotes, runs the near-gate entry + SL/trail
exit strategy on a loop during NSE market hours. Sends Telegram alerts.

MODE (from .env or env var):
  paper  -> PaperBroker: simulated fills against live quotes (SAFE, default)
  live   -> LiveBroker: REAL Fyers orders. Requires CONFIRM_LIVE=YES guard.

  python run.py

Prereqs: run `python auth.py` first (daily Fyers token -> token.json), unless
FYERS_ID/FYERS_TOTP_SECRET/FYERS_PIN are set in .env, in which case this file
refreshes a stale/missing token itself via auth.get_token_totp() — see
auth.py's module docstring for what that trades off before you turn it on.
"""
import os
import csv
import time
import sys
from datetime import datetime, timezone, timedelta

from fyers_client import FyersClient, load_env, nse, SYMBOL_OVERRIDES
from broker import PaperBroker, LiveBroker
from bot import CupBot
from ledger import Ledger
from tracking import Tracker
import notify

IST = timezone(timedelta(hours=5, minutes=30))   # India has no DST


def load_watchlist(path="watchlist.csv", allow_t2t=False):
    wl = []
    for r in csv.DictReader(open(path)):
        sym = r["symbol"].strip().upper()
        # T2T ("BE" series) stocks are delivery-only — a same-day SL-M stop
        # can't execute intraday, so a naive watchlist entry for one is a
        # silent trap: the bot will happily arm/track it, and the "protective"
        # stop just never fires the way it does for a normal -EQ stock. Skip
        # these unless the user has explicitly opted in.
        if sym in SYMBOL_OVERRIDES and not allow_t2t:
            print(f"  SKIP  {sym}: T2T/BE-series symbol — same-day SL can't "
                  f"execute intraday. Set ALLOW_T2T=true in .env to include it anyway.")
            continue
        wl.append({"symbol": sym, "rim": float(r["rim"]),
                   "trigger": float(r["trigger"]), "qty": int(float(r["qty"]))})
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


def _ensure_fresh_token(cfg):
    """token.json is only good for the trading day it was issued. If it's
    missing/stale and TOTP env vars are configured, refresh it automatically;
    otherwise tell the user to run `python auth.py` and stop, rather than
    limping forward on a token FyersClient.from_files() may not even find."""
    import json
    today = datetime.now(IST).strftime("%Y-%m-%d")
    stale = True
    if os.path.exists("token.json"):
        try:
            blob = json.load(open("token.json"))
            stale = blob.get("date") != today
        except Exception:
            stale = True
    if not stale:
        return
    have_totp = all(cfg.get(k) for k in ("FYERS_ID", "FYERS_TOTP_SECRET", "FYERS_PIN"))
    if not have_totp:
        if not os.path.exists("token.json"):
            print("No token.json found. Run `python auth.py` first (or set "
                  "FYERS_ID/FYERS_TOTP_SECRET/FYERS_PIN in .env for auto-login).")
        else:
            print("token.json looks stale (not from today). Run `python auth.py` "
                  "again, or set FYERS_ID/FYERS_TOTP_SECRET/FYERS_PIN in .env "
                  "for this file to refresh it automatically.")
        raise SystemExit(1)
    import auth
    print("token.json is missing/stale — refreshing via TOTP auto-login...")
    auth.get_token_totp(cfg)   # raises auth.TotpLoginError on failure; never swallowed
    print("✓ token refreshed.")


def _funds_preflight(client):
    """A cheap, early sanity check before going LIVE: confirm the token
    actually works and print the available balance, so a bad/expired token
    fails loudly here instead of on the first real order placement."""
    try:
        resp = client.funds()
    except Exception as e:
        print(f"FUNDS PREFLIGHT FAILED: {e}\nRefusing to start LIVE with an "
              f"unverified token/connection.")
        raise SystemExit(1)
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        print(f"FUNDS PREFLIGHT FAILED: Fyers returned {resp!r}\nRefusing to "
              f"start LIVE with an unverified token/connection.")
        raise SystemExit(1)
    bal = None
    for row in resp.get("fund_limit", []):
        if "available" in str(row.get("title", "")).lower():
            bal = row.get("equityAmount", row.get("value"))
            break
    print(f"funds preflight ok" + (f" | available balance ~ Rs {bal}" if bal is not None else ""))


def _acquire_single_instance(lock_path="run.lock"):
    """Prevent two cup-bots running on the same Fyers account.

    A release-on-exit flock on a lockfile: if another run.py already holds
    it, refuse to start. Prevents accidental duplicate live instances.
    """
    import fcntl
    fh = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), lock_path), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another cup-bot is already running (run.lock held). Refusing to start a duplicate.", flush=True)
        sys.exit(2)
    fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
    return fh


def main():
    _lock_fh = _acquire_single_instance()   # single-instance guard: no parallel bots
    cfg = load_env()
    mode = env("MODE", "paper").lower()
    poll = int(env("POLL_SEC", "30"))
    near = float(env("NEAR_PCT", "1.0")) / 100
    alerts = env("ALERTS", "true").lower() == "true"
    max_positions = int(env("MAX_POSITIONS", "0"))       # 0 = unlimited
    capital_budget = env("CAPITAL_BUDGET", None)
    capital_budget = int(capital_budget) if capital_budget else None
    order_tag = env("ORDER_TAG", "cup")[:20]               # Fyers orderTag cap
    allow_t2t = env("ALLOW_T2T", "false").lower() == "true"

    # --- LIVE safety guard: never trade real money by accident ---
    if mode == "live" and env("CONFIRM_LIVE", "").upper() != "YES":
        print("MODE=live but CONFIRM_LIVE is not YES. Refusing to run live.\n"
              "Set CONFIRM_LIVE=YES in .env only when you truly intend real orders.")
        return

    _ensure_fresh_token(cfg)
    client = FyersClient.from_files()        # needs token.json (see auth.py)
    watchlist = load_watchlist(allow_t2t=allow_t2t)

    if mode == "live":
        _funds_preflight(client)
        broker, tag = LiveBroker(client, tag=order_tag), "LIVE"
    else:
        broker, tag = PaperBroker(state_path="paper_state.json"), "PAPER"

    # Separate bot_state per mode: PAPER's tracked positions/stop ids must
    # never bleed into a LIVE run (or vice versa) if MODE is toggled between
    # runs against the same working directory.
    bot_state_path = f"bot_state_{tag.lower()}.json"
    tracker = Tracker(ledger=Ledger("fyers_ledger.db"))
    bot = CupBot(broker, client.ltps, watchlist, state_path=bot_state_path,
                 near_pct=near, mode=tag, alerts=alerts, tag=order_tag,
                 max_positions=max_positions, tracker=tracker,
                 capital_budget=capital_budget)

    print(f"[{tag}] runner start | {len(watchlist)} stocks | poll {poll}s | "
          f"near {near*100:.1f}% | tag={order_tag} | max_positions={max_positions or 'unlimited'}")
    if alerts:
        notify.send(f"🚀 <b>[{tag}] Cup bot started</b>\n{len(watchlist)} stocks | "
                    f"poll {poll}s | near {near*100:.1f}%")

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
                    if alerts:
                        notify.send(f"⚠️ [{tag}] cycle error: {e}")
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
