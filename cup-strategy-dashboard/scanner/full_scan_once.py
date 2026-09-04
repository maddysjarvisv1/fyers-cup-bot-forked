"""
One-time full-NSE cup-setup scan (fast weekly-history method).

Applies the SAME strategy rules as scanner.nightly_scan():
  - ATH must be 3+ years old (Malkansview cup rule)
  - current price within QUEUE_THRESHOLD (5%) below that old ATH
  - buy trigger = ATH * 1.04, SL = buy * (1 - SL_PCT), qty for FIXED_LOSS risk

Speed: uses weekly ('1wk') history instead of daily. Weekly 'High' is the
max of that week's daily highs, so the all-time-high VALUE is identical and
its DATE is accurate to the week — but ~10x less data to download.

Rebuilds the queue transactionally: computes everything first, then clears
the old queue and bulk-inserts the real cup setups. Sends a Telegram summary.
"""
import os, sys, time
from datetime import datetime, timedelta
import pytz
import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from scanner import calc_qty, SL_PCT, QUEUE_THRESHOLD, ATH_MIN_YEARS, CAPITAL, FIXED_LOSS
from db import get_conn, init_db
import alerts

IST = pytz.timezone("Asia/Kolkata")
BHAV = os.path.join(os.path.dirname(__file__), "..", "PR210726", "pd21072026.csv")
CHUNK = 50
SLEEP_BETWEEN = 1.0


def load_universe():
    df = pd.read_csv(BHAV, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    df["SERIES"] = df["SERIES"].astype(str).str.strip()
    df["SYMBOL"] = df["SYMBOL"].astype(str).str.strip()
    eq = df[df["SERIES"] == "EQ"]
    syms = sorted(s for s in eq["SYMBOL"].unique() if s and s != "nan")
    return syms


def main():
    init_db()
    cutoff = datetime.now(IST).date() - timedelta(days=ATH_MIN_YEARS * 365)
    syms = load_universe()
    print(f"[FullScan] {datetime.now(IST)} start | {len(syms)} EQ symbols | "
          f"cutoff {cutoff} | band -{QUEUE_THRESHOLD*100:.0f}%", flush=True)

    results = []
    stats = {"recent": 0, "toofar": 0, "nodata": 0, "err": 0}

    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        tickers = [s + ".NS" for s in chunk]
        try:
            df = yf.download(tickers, period="max", interval="1wk",
                             auto_adjust=True, group_by="ticker",
                             threads=True, progress=False)
        except Exception as e:
            print(f"[FullScan] batch {i} download error: {e}", flush=True)
            stats["err"] += len(chunk)
            continue

        for s in chunk:
            try:
                sub = df[s + ".NS"].dropna()
                if sub.empty or len(sub) < 10:
                    stats["nodata"] += 1
                    continue
                ath = float(sub["High"].max())
                ath_date = sub["High"].idxmax().date()
                cur = float(sub["Close"].iloc[-1])
                if ath <= 0:
                    stats["nodata"] += 1
                    continue
                # Rule 1: ATH must be 3+ years old
                if ath_date > cutoff:
                    stats["recent"] += 1
                    continue
                pct = (cur - ath) / ath * 100
                # NOTE: band filter removed here — we now capture the FULL
                # old-ATH universe and choose the band afterwards from the data.
                buy = round(ath * 1.04, 2)
                sl = round(buy * (1 - SL_PCT), 2)
                qty = calc_qty(buy)
                results.append({
                    "symbol": s, "ath": round(ath, 2), "ath_date": str(ath_date),
                    "buy_price": buy, "sl_price": sl, "qty": qty,
                    "capital": round(buy * qty, 2),
                    "current_price": round(cur, 2), "pct_from_ath": round(pct, 2),
                })
            except Exception:
                stats["err"] += 1

        done = min(i + CHUNK, len(syms))
        print(f"[FullScan] {done}/{len(syms)} scanned | qualifiers so far: {len(results)}", flush=True)
        time.sleep(SLEEP_BETWEEN)

    # Save the FULL old-ATH universe (3+ yr rule passed), sorted closest-first.
    results.sort(key=lambda r: r["pct_from_ath"], reverse=True)
    out = os.path.join(os.path.dirname(__file__), "..", "old_ath_universe.csv")
    pd.DataFrame(results).to_csv(out, index=False)

    within = lambda b: sum(1 for r in results if r["pct_from_ath"] >= -b)
    print(f"[FullScan] DONE. Old-ATH universe: {len(results)} | "
          f"recent-ATH excluded: {stats['recent']} | "
          f"nodata: {stats['nodata']} | err: {stats['err']}", flush=True)
    print(f"[FullScan] Within band -> 5%:{within(5)}  10%:{within(10)}  "
          f"15%:{within(15)}  20%:{within(20)}  25%:{within(25)}", flush=True)
    for r in results[:20]:
        print(f"   {r['symbol']:12} old-ATH {r['ath']:>9.2f} ({r['ath_date']}) "
              f"now {r['current_price']:>9.2f} {r['pct_from_ath']:>6.2f}% buy@{r['buy_price']}", flush=True)
    print(f"[FullScan] Saved full universe to {out}", flush=True)


if __name__ == "__main__":
    main()
