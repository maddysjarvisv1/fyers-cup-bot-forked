import os
import time
import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from db import init_db
from scanner import nightly_scan, intraday_monitor
import alerts

IST = pytz.timezone("Asia/Kolkata")
INTRADAY_INTERVAL = int(os.getenv("INTRADAY_SCAN_INTERVAL_MIN", 15))


def main():
    print("[Main] Cup Strategy Scanner starting...")
    init_db()

    scheduler = BlockingScheduler(timezone=IST)

    # Nightly full scan — runs at 8:00 PM IST every weekday
    scheduler.add_job(
        nightly_scan,
        trigger="cron",
        day_of_week="mon-fri",
        hour=20,
        minute=0,
        id="nightly_scan",
        name="Nightly ATH full scan"
    )

    # Intraday monitor — every N minutes
    scheduler.add_job(
        intraday_monitor,
        trigger="interval",
        minutes=INTRADAY_INTERVAL,
        id="intraday_monitor",
        name="Intraday position monitor"
    )

    alerts.send("ℹ️ <b>Cup Strategy Scanner started</b>\n"
                f"Intraday check every {INTRADAY_INTERVAL} min\n"
                "Nightly scan at 8:00 PM IST")

    print("[Main] Scheduler running. Press Ctrl+C to stop.")

    # Run nightly scan immediately on startup (to populate queue)
    print("[Main] Running initial scan on startup...")
    try:
        nightly_scan()
    except Exception as e:
        alerts.alert_error("Startup scan", str(e))

    scheduler.start()


if __name__ == "__main__":
    main()
