# WALKTHROUGH — how this bot works, in plain English

A guide for the owner. No jargon, no hand-waving. If you only read one file, read this one.

---

## 1. What the bot does, in one paragraph

It looks for NSE stocks that **once hit a high price, fell a long way, and are now climbing back up
towards that old high**. When a stock gets close, the bot waits. When the price actually reaches the
old high plus 4%, it **buys**. Immediately after buying, it places a **stop-loss** so that if the price
falls 6% below the buy price, the position is sold automatically. That's the whole strategy — one
buy rule, one safety sell rule.

This is called the **ATH cup-breakout**: "ATH" = all-time high, "cup" = the shape of the price chart
(falls, sits at the bottom, comes back up).

---

## 2. The jobs, in the order they run each day

| When (IST) | What happens |
|---|---|
| 08:00 | **The screener scans** the whole NSE market for cup candidates. Takes ~15–20 min. |
| 08:00 (same run) | **New candidates are added to the watchlist automatically** (`watchlist_sync.py`). |
| 08:45 | **You get a login reminder.** Fyers requires a fresh login every single day. |
| ~08:50 | **You paste the redirected URL back.** The bot exchanges it for a token and restarts itself. |
| 09:15 | Market opens. The bot polls prices every 30 seconds. |
| 15:30 | Market closes. |

**Why the daily login?** SEBI changed the rules — the old 15-day token was disabled, so the token is now
only valid for one trading day. There is no way around this.

---

## 3. The money rules (the guards that keep you safe)

- **₹50,000 capital budget.** The bot will not commit more than this to cup positions at once.
- **4 positions maximum.** No more than four stocks held at the same time.
- **6% stop-loss on every position.** Placed as a real stop order at the broker, not just in code.
- **`CONFIRM_LIVE=YES`** must be set for real orders. Without it the bot refuses to trade live.

---

## 4. The files that matter

| File | What it does |
|---|---|
| `nse_screener.py` | Scans NSE, finds cup candidates, writes them to a candidates file. |
| `watchlist_sync.py` | Moves candidates into `watchlist.csv`. **The only thing allowed to write that file.** |
| `watchlist.csv` | The shopping list: which stocks to watch, at what trigger price, how many shares. |
| `run.py` | The daily driver — checks the token, loads the watchlist, starts the bot. |
| `bot.py` | The brain — decides when to buy and when to sell. |
| `broker.py` | The hands — actually talks to Fyers (places orders, reads positions). |
| `cup_sl_engine.py` | The stop-loss and trailing-stop maths. |
| `auth.py` / `auth_ui.py` | The daily login and token exchange. |
| `ledger.py` / `fyers_ledger.db` | The record of what was ordered, filled, and made or lost. |
| `dashboard.py` | The read-only web view of state and P&L. |

---

## 5. `watchlist.csv` — the most important file to understand

Three columns, one line per stock:

```
symbol,rim,trigger,qty
NAZARA,419.5,436.28,28
```

- **rim** — the old all-time high price (₹419.50).
- **trigger** — the price at which to buy = rim × 1.04 (₹436.28). The 4% above the old high is the
  "breakout" confirmation.
- **qty** — how many shares to buy.

**Two things to know:**
1. The bot reads this file **only at startup**. If you change it while the bot is running, nothing
   happens until the next restart. The daily login restart picks up your changes.
2. **Never write to this file by hand.** It has no trailing newline, and a hand-edit once glued two
   rows together (`...5NAZARA,...`) which made the bot **refuse to start**. Use `watchlist_sync.py`,
   which writes the file safely and then checks it with the bot's own loader.

---

## 6. Where to look when something is wrong

1. **Is the token fresh?** `token.json` should have today's date.
2. **Is the bot running?** `ps aux | grep run.py`
3. **What does the log say?** `tail -50 /tmp/fyers_run_<date>.log`
4. **What did it order?** `sqlite3 fyers_ledger.db "select * from orders order by rowid desc limit 10;"`
5. **Did the screener run?** check `nse_screener_candidates.csv`

---

## 7. Run the tests

```sh
env -u PYTHONPATH .venv/bin/python -m pytest tests/ -q
```

The `env -u PYTHONPATH` part matters: without it the wrong Python libraries leak in and every import
fails with a confusing `numpy` error that is not actually a broken install.

---

## 8. Honest limitation: this is a ONE-strategy bot

The bot is built to run **one** strategy. If you later run two strategies on the same broker account,
several things break — and they are not obvious:

- **Budget doubles.** Each process enforces its own ₹50k, so two bots = ₹100k of real exposure.
- **Two stops on one stock.** Fyers keeps **one net position per stock**, so if two strategies hold the
  same stock, each can place a stop on the full amount → **selling twice what you own**.
- **Same daily token.** Two bots fight over one login.

**Do not simply copy this folder and run a second `run.py`.** The correct fix is a shared, account-level
layer that owns the money rules and stops *before* orders reach the broker. That design work is
documented separately — ask Jarvis for the multi-strategy review before adding strategy #2.
