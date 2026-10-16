#!/usr/bin/env python3
"""
NSE cup/breakout screener for the fork's watchlist.

Pulls the OFFICIAL NSE equity universe (nsearchives EQUITY_L.csv — proven
reachable), then screens each liquid EQ-series stock for the cup setup the
bot trades: a 3+yr-old all-time-high with current price within threshold
of that ATH (breakout candidate).

Jarvis-safe: writes to a CANDIDATES file (nse_screener_candidates.csv), NEVER
touches the protected watchlist.csv. Adoption into the live watchlist is a
separate, explicit step.

Usage:
  nse_screener.py scan [max_symbols]   # full scan (default all); cap for a quick test
  nse_screener.py list                  # show last candidates
"""
from __future__ import annotations
import os, sys, io, time, csv
import urllib.request
import datetime

import pandas as pd
import yfinance as yf

from broker import align_tick

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
CAND = os.path.join(HERE, "nse_screener_candidates.csv")

ATH_MIN_YEARS = 3
QUEUE_PCT = 5.0        # queue if current price within 5% below ATH
BREAK_PCT = 2.0        # straight-to-buy if within 2% above ATH
FIXED_LOSS = 750.0
SL_PCT = 6.0 / 100
MIN_AVG_VOLUME = 100000
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def now_ist():
    return datetime.datetime.now(IST)


def fetch_nse_symbols(max_n=None):
    req = urllib.request.Request(NSE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        df = pd.read_csv(io.StringIO(r.read().decode("latin-1", "ignore")))
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    # keep liquid EQ series equities (exclude SME, ST, etc.)
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
    syms = df["SYMBOL"].dropna().astype(str).str.strip().tolist()
    if max_n:
        syms = syms[:max_n]
    print(f"[Screener] NSE universe: {len(syms)} EQ-series symbols fetched @ {now_ist()}")
    return syms


def calc_qty(buy_price):
    risk = buy_price * SL_PCT
    return max(int(FIXED_LOSS // risk), 1)


def screen_symbol(sym):
    """Return candidate dict if it's a cup setup, else None."""
    try:
        h = yf.Ticker(f"{sym}.NS").history(period="max", auto_adjust=True)
        if h.empty or len(h) < 10:
            return None
        if "Volume" not in h.columns:
            return None
        avg_vol = float(h["Volume"].tail(60).mean())
        if pd.isna(avg_vol) or avg_vol < MIN_AVG_VOLUME:
            return None  # illiquid — 60-day avg volume below MIN_AVG_VOLUME
        ath_idx = h["High"].idxmax()
        ath = float(h["High"].max())
        adate = ath_idx.date()
        cutoff = now_ist().date() - datetime.timedelta(days=ATH_MIN_YEARS * 365)
        if adate > cutoff:
            return None  # ATH too recent — not a cup setup
        cur = float(h["Close"].iloc[-1])
        pct = (cur - ath) / ath * 100
        buy = align_tick(ath * 1.04)
        sl = align_tick(buy * (1 - SL_PCT))
        return {
            "symbol": sym, "ath": round(ath, 2), "ath_date": str(adate),
            "buy_price": buy, "sl_price": sl,
            "qty": calc_qty(buy), "capital": round(buy * calc_qty(buy), 2),
            "current": round(cur, 2), "pct_from_ath": round(pct, 2),
        }
    except Exception:
        return None


def scan(max_n=None, cap=None):
    syms = fetch_nse_symbols(max_n)
    hits = []
    t0 = time.time()
    for i, s in enumerate(syms):
        if i % 100 == 0:
            print(f"  [{i}/{len(syms)}] {time.time()-t0:.0f}s  hits={len(hits)}")
        if cap and len(hits) >= cap:
            print("  (cap reached)"); break
        time.sleep(0.25)  # respectful ~4 req/s to yfinance
        row = screen_symbol(s)
        if row and row["pct_from_ath"] >= -QUEUE_PCT:
            hits.append(row)
    with open(CAND, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(hits[0].keys()) if hits else ["symbol"])
        wr.writeheader(); wr.writerows(hits)
    print(f"\n[!] {len(hits)} cup/breakout candidates -> {os.path.basename(CAND)}")
    for h in hits[:10]:
        print(f"   {h['symbol']:<12} ATH {h['ath']:>9} {h['ath_date']}  cur {h['current']:>9}  {h['pct_from_ath']:>6}%  qty {h['qty']}")
    if len(hits) > 10:
        print(f"   ... and {len(hits)-10} more")


def list_cands():
    if not os.path.exists(CAND):
        print("no candidates yet — run: nse_screener.py scan"); return
    rows = list(csv.DictReader(open(CAND)))
    print(f"{len(rows)} candidates in {os.path.basename(CAND)}:")
    for r in rows[:15]:
        print(f"   {r['symbol']:<12} {r.get('ath',''):>9}  cur {r.get('current',''):>9}  {r.get('pct_from_ath',''):>6}s  qty {r.get('qty','')}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "scan":
        scan(max_n=int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None)
    elif cmd == "list":
        list_cands()
    else:
        print(__doc__); sys.exit(2)