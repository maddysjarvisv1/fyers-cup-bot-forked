import os
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ICONS = {
    "queue":   "👀",
    "buy":     "🟢",
    "sl":      "🔴",
    "be":      "🟡",
    "partial": "💰",
    "trail":   "🔁",
    "exit":    "✅",
    "error":   "🚨",
    "info":    "ℹ️",
}


def send(message: str, silent: bool = False):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[TELEGRAM disabled] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print(f"[TELEGRAM error] {resp.text}")
    except Exception as e:
        print(f"[TELEGRAM exception] {e}")


def alert_queue(symbol, ath, current_price, pct_from_ath, buy_price):
    send(
        f"{ICONS['queue']} <b>QUEUE: {symbol}</b>\n"
        f"Approaching ATH ₹{ath:,.2f}\n"
        f"Current: ₹{current_price:,.2f} ({pct_from_ath:+.1f}% from ATH)\n"
        f"Buy trigger @ ₹{buy_price:,.2f} (ATH+4%)"
    )


def alert_buy(symbol, buy_price, sl_price, qty, capital, ath_date):
    send(
        f"{ICONS['buy']} <b>BUY SIGNAL: {symbol}</b>\n"
        f"Buy @ ₹{buy_price:,.2f}\n"
        f"Qty: {qty} shares | Capital: ₹{capital:,.0f}\n"
        f"SL @ ₹{sl_price:,.2f} (6% below)\n"
        f"ATH set on: {ath_date}"
    )


def alert_sl_hit(symbol, exit_price, loss):
    send(
        f"{ICONS['sl']} <b>SL HIT: {symbol}</b>\n"
        f"Exited @ ₹{exit_price:,.2f}\n"
        f"Loss: ₹{abs(loss):,.0f}"
    )


def alert_breakeven(symbol, buy_price, current_price):
    send(
        f"{ICONS['be']} <b>BREAKEVEN SET: {symbol}</b>\n"
        f"+10% reached @ ₹{current_price:,.2f}\n"
        f"SL moved to buy price ₹{buy_price:,.2f}\n"
        f"Trade is now risk-free ✓"
    )


def alert_partial_book(symbol, exit_price, qty_booked, profit, trail_sl):
    send(
        f"{ICONS['partial']} <b>PARTIAL BOOKED: {symbol}</b>\n"
        f"+20% reached @ ₹{exit_price:,.2f}\n"
        f"Booked {qty_booked} shares | Profit: ₹{profit:,.0f}\n"
        f"Trail SL set @ ₹{trail_sl:,.2f} (10% below high)\n"
        f"Remaining position trailing 🚀"
    )


def alert_trail_exit(symbol, exit_price, profit):
    send(
        f"{ICONS['exit']} <b>TRAIL EXIT: {symbol}</b>\n"
        f"Trail SL hit @ ₹{exit_price:,.2f}\n"
        f"Total profit on remaining: ₹{profit:,.0f}"
    )


def alert_trail_update(symbol, new_sl, highest_price):
    send(
        f"{ICONS['trail']} <b>TRAIL UPDATED: {symbol}</b>\n"
        f"New high: ₹{highest_price:,.2f}\n"
        f"Trail SL moved to ₹{new_sl:,.2f}",
        silent=True  # silent — informational only
    )


def alert_error(context: str, detail: str):
    send(
        f"{ICONS['error']} <b>ERROR: {context}</b>\n"
        f"{detail}"
    )
