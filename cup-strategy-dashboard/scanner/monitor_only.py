"""
Monitor-only launcher (no full NSE nightly scan).

Polls the EXISTING queue (your curated watchlist) every N minutes during
NSE market hours and fires BUY/SL/target alerts via the same logic as the
full scanner. It intentionally does NOT run nightly_scan(), so it will not
overwrite a hand-loaded watchlist or hammer Yahoo with a 2000+ symbol scan.

Run the full scanner (scanner/main.py) instead if/when you want the
automated full-universe ATH scan.
"""
import os
import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from db import init_db
from scanner import intraday_monitor
import alerts

IST = pytz.timezone("Asia/Kolkata")
INTERVAL = int(os.getenv("INTRADAY_SCAN_INTERVAL_MIN", 15))


def main():
    print("[Monitor] Monitor-only mode starting (no full nightly scan)...")
    init_db()

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(
        intraday_monitor,
        trigger="interval",
        minutes=INTERVAL,
        id="intraday_monitor",
        name="Intraday position/queue monitor",
    )

    alerts.send(
        "ℹ️ <b>Cup Strategy monitor started</b> (monitor-only)\n"
        f"Polling watchlist every {INTERVAL} min during market hours.\n"
        "No full NSE scan will run."
    )
    print(f"[Monitor] Armed. Polls queue every {INTERVAL} min during NSE market hours "
          "(Mon-Fri 09:15-15:30 IST). Press Ctrl+C to stop.")
    scheduler.start()


if __name__ == "__main__":
    main()
