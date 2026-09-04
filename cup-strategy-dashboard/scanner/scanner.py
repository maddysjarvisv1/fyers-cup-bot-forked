import os
import math
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import pytz

import alerts
from db import get_conn, init_db

IST = pytz.timezone("Asia/Kolkata")

# ── CONFIG ────────────────────────────────────────────────────────────────────
CAPITAL         = float(os.getenv("CAPITAL_PER_TRADE", 50000))
FIXED_LOSS      = float(os.getenv("FIXED_LOSS", 750))
SL_PCT          = float(os.getenv("SL_PCT", 6)) / 100
BE_TRIGGER      = float(os.getenv("BREAKEVEN_TRIGGER_PCT", 10)) / 100
PARTIAL_TRIGGER = float(os.getenv("PARTIAL_BOOK_TRIGGER_PCT", 20)) / 100
TRAIL_PCT       = float(os.getenv("TRAIL_PCT", 10)) / 100
QUEUE_THRESHOLD = float(os.getenv("QUEUE_THRESHOLD_PCT", 5)) / 100
ATH_MIN_YEARS   = 3


# ── HELPERS ───────────────────────────────────────────────────────────────────
def calc_qty(buy_price: float) -> int:
    """Shares = Fixed loss / (buy_price * SL_PCT)"""
    risk_per_share = buy_price * SL_PCT
    qty = math.floor(FIXED_LOSS / risk_per_share)
    return max(qty, 1)


def is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


# ── NSE SYMBOL LIST ───────────────────────────────────────────────────────────
def fetch_nse_symbols() -> list[str]:
    """Download full NSE equity symbol list from official NSE archives."""
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(pd.io.common.StringIO(resp.text))
        # Column is "SYMBOL"
        symbols = df["SYMBOL"].dropna().tolist()
        print(f"[Scanner] Fetched {len(symbols)} NSE symbols")
        return symbols
    except Exception as e:
        alerts.alert_error("NSE symbol fetch", str(e))
        return []


# ── ATH CALCULATION ───────────────────────────────────────────────────────────
def get_ath_data(symbol: str) -> dict | None:
    """
    Returns ATH price, ATH date, and whether ATH is 3+ years old.
    Uses yfinance with .NS suffix.
    """
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="max", auto_adjust=True)
        if hist.empty or len(hist) < 10:
            return None

        ath_idx  = hist["High"].idxmax()
        ath_val  = float(hist["High"].max())
        ath_date = ath_idx.date()

        # ATH must be at least 3 years old
        cutoff = datetime.now(IST).date() - timedelta(days=ATH_MIN_YEARS * 365)
        if ath_date > cutoff:
            return None     # ATH too recent — not a cup setup

        current_price = float(hist["Close"].iloc[-1])
        buy_price     = round(ath_val * (1 + 0.04), 2)   # ATH + 4%
        sl_price      = round(buy_price * (1 - SL_PCT), 2)
        qty           = calc_qty(buy_price)
        pct_from_ath  = ((current_price - ath_val) / ath_val) * 100

        return {
            "symbol":        symbol,
            "ath":           round(ath_val, 2),
            "ath_date":      str(ath_date),
            "buy_price":     buy_price,
            "sl_price":      sl_price,
            "qty":           qty,
            "capital":       round(buy_price * qty, 2),
            "current_price": round(current_price, 2),
            "pct_from_ath":  round(pct_from_ath, 2),
        }
    except Exception:
        return None


# ── NIGHTLY FULL SCAN ─────────────────────────────────────────────────────────
def nightly_scan():
    """
    Run once daily (post market close).
    Scans ALL NSE stocks, finds those with ATH 3+ years old
    and current price within QUEUE_THRESHOLD of ATH.
    Adds qualifying stocks to queue.
    """
    print(f"[Scanner] Nightly scan started at {datetime.now(IST)}")
    symbols = fetch_nse_symbols()
    if not symbols:
        alerts.alert_error("Nightly scan", "Could not fetch NSE symbol list")
        return

    added = 0
    errors = 0

    for i, sym in enumerate(symbols):
        if i % 100 == 0:
            print(f"[Scanner] Progress: {i}/{len(symbols)}")
        time.sleep(0.3)   # Respectful delay — ~3 req/sec to yfinance

        data = get_ath_data(sym)
        if data is None:
            continue

        # Only queue if price is within QUEUE_THRESHOLD below ATH
        # (already above ATH handled separately — straight to buy alert)
        pct = data["pct_from_ath"]
        if pct < -QUEUE_THRESHOLD * 100:
            continue   # Too far below ATH, not relevant yet

        conn = get_conn()
        try:
            conn.execute("""
                INSERT INTO queue (symbol, ath, ath_date, buy_price, sl_price,
                                   qty, capital, current_price, pct_from_ath)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    ath           = excluded.ath,
                    ath_date      = excluded.ath_date,
                    buy_price     = excluded.buy_price,
                    sl_price      = excluded.sl_price,
                    qty           = excluded.qty,
                    capital       = excluded.capital,
                    current_price = excluded.current_price,
                    pct_from_ath  = excluded.pct_from_ath,
                    updated_at    = datetime('now')
            """, (
                data["symbol"], data["ath"], data["ath_date"],
                data["buy_price"], data["sl_price"],
                data["qty"], data["capital"],
                data["current_price"], data["pct_from_ath"],
            ))
            conn.commit()
            added += 1

            # Alert if very close to ATH (within 1%)
            if pct >= -1.0:
                alerts.alert_queue(
                    data["symbol"], data["ath"],
                    data["current_price"], data["pct_from_ath"],
                    data["buy_price"]
                )
        except Exception as e:
            errors += 1
        finally:
            conn.close()

    print(f"[Scanner] Nightly scan done. Added/updated: {added}, errors: {errors}")
    if errors > 20:
        alerts.alert_error("Nightly scan", f"{errors} stocks failed to process")


# ── MANUAL STOCK ADD ──────────────────────────────────────────────────────────
def add_stock_manually(symbol: str, ath: float = None):
    """
    Add a single stock to the queue manually.
    If ATH is provided, use it. Otherwise fetch from yfinance.
    ATH_MIN_YEARS check is SKIPPED for manual adds (user decides).
    """
    symbol = symbol.upper().strip()
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist   = ticker.history(period="max", auto_adjust=True)
        if hist.empty:
            return {"error": f"No data found for {symbol}"}

        if ath is None:
            ath = float(hist["High"].max())
            ath_date = str(hist["High"].idxmax().date())
        else:
            # Find approximate date for provided ATH
            closest = (hist["High"] - ath).abs().idxmin()
            ath_date = str(closest.date())

        current_price = float(hist["Close"].iloc[-1])
        buy_price     = round(ath * 1.04, 2)
        sl_price      = round(buy_price * (1 - SL_PCT), 2)
        qty           = calc_qty(buy_price)
        pct_from_ath  = round(((current_price - ath) / ath) * 100, 2)

        conn = get_conn()
        conn.execute("""
            INSERT INTO queue (symbol, ath, ath_date, buy_price, sl_price,
                               qty, capital, current_price, pct_from_ath)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                ath           = excluded.ath,
                ath_date      = excluded.ath_date,
                buy_price     = excluded.buy_price,
                sl_price      = excluded.sl_price,
                qty           = excluded.qty,
                capital       = excluded.capital,
                current_price = excluded.current_price,
                pct_from_ath  = excluded.pct_from_ath,
                updated_at    = datetime('now')
        """, (symbol, round(ath,2), ath_date, buy_price, sl_price,
              qty, round(buy_price*qty,2), round(current_price,2), pct_from_ath))
        conn.commit()
        conn.close()

        return {
            "symbol": symbol, "ath": ath, "ath_date": ath_date,
            "buy_price": buy_price, "sl_price": sl_price,
            "qty": qty, "current_price": current_price,
            "pct_from_ath": pct_from_ath
        }
    except Exception as e:
        return {"error": str(e)}


# ── INTRADAY MONITOR ──────────────────────────────────────────────────────────
def intraday_monitor():
    """
    Runs every 15 min during market hours.
    1. Check queue stocks — did any trigger buy signal?
    2. Check active positions — SL hit? Target hit? Trail update?
    """
    if not is_market_hours():
        return

    print(f"[Monitor] Intraday check at {datetime.now(IST)}")
    _check_queue_triggers()
    _check_positions()


def _get_live_price(symbol: str) -> float | None:
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="1d", interval="5m", auto_adjust=True)
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _check_queue_triggers():
    conn = get_conn()
    queue = conn.execute("SELECT * FROM queue").fetchall()
    conn.close()

    for row in queue:
        price = _get_live_price(row["symbol"])
        if price is None:
            continue

        # Update current price in queue
        conn = get_conn()
        pct = round(((price - row["ath"]) / row["ath"]) * 100, 2)
        conn.execute("""
            UPDATE queue SET current_price=?, pct_from_ath=?, updated_at=datetime('now')
            WHERE symbol=?
        """, (price, pct, row["symbol"]))
        conn.commit()
        conn.close()

        # BUY trigger: price >= ATH + 4%
        if price >= row["buy_price"]:
            _open_position(row, price)


def _open_position(queue_row, trigger_price: float):
    symbol    = queue_row["symbol"]
    buy_price = queue_row["buy_price"]
    sl_price  = queue_row["sl_price"]
    qty       = queue_row["qty"]

    # Check not already in positions
    conn = get_conn()
    exists = conn.execute(
        "SELECT id FROM positions WHERE symbol=? AND status NOT IN ('CLOSED')",
        (symbol,)
    ).fetchone()
    if exists:
        conn.close()
        return

    conn.execute("""
        INSERT INTO positions (symbol, buy_price, sl_price, initial_sl,
                               qty_total, qty_remaining, capital, fixed_loss,
                               ath, highest_price)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (symbol, buy_price, sl_price, sl_price,
          qty, qty, queue_row["capital"], FIXED_LOSS,
          queue_row["ath"], trigger_price))
    conn.commit()
    conn.close()

    # Remove from queue
    conn = get_conn()
    conn.execute("DELETE FROM queue WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()

    alerts.alert_buy(symbol, buy_price, sl_price, qty,
                     queue_row["capital"], queue_row["ath_date"])
    print(f"[Monitor] BUY opened: {symbol} @ {buy_price}")


def _check_positions():
    conn = get_conn()
    positions = conn.execute(
        "SELECT * FROM positions WHERE status NOT IN ('CLOSED')"
    ).fetchall()
    conn.close()

    for pos in positions:
        price = _get_live_price(pos["symbol"])
        if price is None:
            continue

        symbol    = pos["symbol"]
        buy_price = pos["buy_price"]
        status    = pos["status"]
        qty_rem   = pos["qty_remaining"]
        highest   = pos["highest_price"] or price

        # Update highest price seen
        new_highest = max(highest, price)

        # ── 1. SL HIT ──────────────────────────────────────────────
        current_sl = pos["trail_sl"] if pos["trail_sl"] else pos["sl_price"]
        if price <= current_sl:
            pnl = (price - buy_price) * qty_rem
            _close_position(pos, price, pnl, "SL_HIT" if status == "ACTIVE" else "TRAIL_EXIT")
            if status == "ACTIVE":
                alerts.alert_sl_hit(symbol, price, pnl)
            else:
                alerts.alert_trail_exit(symbol, price, pnl)
            continue

        # ── 2. BREAKEVEN (+10%) ─────────────────────────────────────
        if status == "ACTIVE" and price >= buy_price * (1 + BE_TRIGGER):
            conn = get_conn()
            conn.execute("""
                UPDATE positions SET status='BE_SET', sl_price=buy_price,
                highest_price=?, updated_at=datetime('now') WHERE id=?
            """, (new_highest, pos["id"]))
            conn.commit()
            conn.close()
            alerts.alert_breakeven(symbol, buy_price, price)

        # ── 3. PARTIAL BOOK (+20%) ──────────────────────────────────
        elif status == "BE_SET" and price >= buy_price * (1 + PARTIAL_TRIGGER):
            qty_book = math.floor(qty_rem / 2)
            qty_keep = qty_rem - qty_book
            profit   = (price - buy_price) * qty_book
            trail_sl = round(price * (1 - TRAIL_PCT), 2)

            conn = get_conn()
            conn.execute("""
                UPDATE positions SET status='TRAILING',
                qty_remaining=?, trail_sl=?, highest_price=?,
                pnl=pnl+?, updated_at=datetime('now') WHERE id=?
            """, (qty_keep, trail_sl, new_highest, profit, pos["id"]))
            conn.commit()

            # Log the partial booking
            conn.execute("""
                INSERT INTO trade_log (symbol, buy_price, exit_price, qty, pnl, exit_reason, entry_date)
                VALUES (?,?,?,?,?,'PARTIAL_BOOK',?)
            """, (symbol, buy_price, price, qty_book, profit, pos["entry_date"]))
            conn.commit()
            conn.close()
            alerts.alert_partial_book(symbol, price, qty_book, profit, trail_sl)

        # ── 4. TRAIL SL UPDATE ──────────────────────────────────────
        elif status == "TRAILING" and new_highest > highest:
            new_trail_sl = round(new_highest * (1 - TRAIL_PCT), 2)
            if new_trail_sl > (pos["trail_sl"] or 0):
                conn = get_conn()
                conn.execute("""
                    UPDATE positions SET trail_sl=?, highest_price=?,
                    updated_at=datetime('now') WHERE id=?
                """, (new_trail_sl, new_highest, pos["id"]))
                conn.commit()
                conn.close()
                alerts.alert_trail_update(symbol, new_trail_sl, new_highest)

        else:
            # Just update highest price
            if new_highest > highest:
                conn = get_conn()
                conn.execute("""
                    UPDATE positions SET highest_price=?, updated_at=datetime('now')
                    WHERE id=?
                """, (new_highest, pos["id"]))
                conn.commit()
                conn.close()


def _close_position(pos, exit_price: float, pnl: float, reason: str):
    conn = get_conn()
    conn.execute("""
        UPDATE positions SET status='CLOSED', pnl=pnl+?,
        updated_at=datetime('now') WHERE id=?
    """, (pnl, pos["id"]))
    conn.execute("""
        INSERT INTO trade_log (symbol, buy_price, exit_price, qty, pnl, exit_reason, entry_date)
        VALUES (?,?,?,?,?,?,?)
    """, (pos["symbol"], pos["buy_price"], exit_price,
          pos["qty_remaining"], pnl, reason, pos["entry_date"]))
    conn.commit()
    conn.close()
