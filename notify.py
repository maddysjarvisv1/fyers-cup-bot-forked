"""
Telegram alerts for the lean bot. Creds from .env:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...
If not set, alerts just print to the console (no crash).
"""
import os
import requests
from fyers_client import load_env

_cfg = load_env()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", _cfg.get("TELEGRAM_BOT_TOKEN", ""))
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", _cfg.get("TELEGRAM_CHAT_ID", ""))


def send(text: str, silent: bool = False):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[telegram off] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_notification": silent},
            timeout=10,
        )
    except Exception as e:
        print(f"[telegram error] {e}")


# --- event helpers (mode tag makes it obvious paper vs live) ---
def armed(mode, sym, ltp, trigger, qty):
    send(f"👀 <b>[{mode}] ARMED {sym}</b>\nLTP {ltp} → buy-stop @ {trigger} × {qty}")

def entry(mode, sym, price, qty, sl):
    send(f"🟢 <b>[{mode}] BUY FILLED {sym}</b>\n@ {price} × {qty}\nInitial SL {sl}")

def breakeven(mode, sym, sl):
    send(f"🟡 <b>[{mode}] BREAKEVEN {sym}</b>\nSL moved to {sl} — risk-free ✓")

def partial(mode, sym, qty, price, sl, keep):
    send(f"💰 <b>[{mode}] PARTIAL BOOK {sym}</b>\nSold {qty} @ {price}\nTrail SL {sl}, keeping {keep} 🚀")

def trail(mode, sym, sl, high):
    send(f"🔁 <b>[{mode}] TRAIL {sym}</b>\nSL → {sl} (high {high})", silent=True)

def exit_(mode, sym, price, qty, pnl):
    icon = "✅" if pnl >= 0 else "🔴"
    send(f"{icon} <b>[{mode}] EXIT {sym}</b>\n@ {price} × {qty}\nP&L {pnl:+.0f}")

def missed_entry(mode, sym, trigger, ltp, gap_pct=None, rim=None, qty=None, reason=None, day=None):
    gap_str = f" ({float(gap_pct):+.2f}%)" if gap_pct is not None else ""
    send(f"⏭️ <b>[{mode}] MISSED ENTRY {sym}</b>\n"
         f"Trigger {trigger} | LTP {ltp}{gap_str} — gapped past trigger, not chasing")

def error(mode, sym, msg):
    send(f"🚨 <b>[{mode}] CRITICAL {sym}</b>\n{msg}")

def report(mode, realized, wins, losses, open_positions):
    send(f"📊 <b>[{mode}] Session report</b>\nRealized P&L {realized:+.0f} | wins {wins} losses {losses}\n"
         f"Open positions: {len(open_positions)}")
