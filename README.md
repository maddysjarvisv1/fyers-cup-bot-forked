# cup-bot — lean Fyers ATH cup-breakout bot

A self-owned algo bot for the Malkansview ATH cup-breakout strategy on NSE.
Direct **Fyers** API (`fyers-apiv3`), ~20 packages, no heavyweight platform.
**Paper mode** (simulated fills vs live quotes) and **live mode** share one code path.

## Strategy
- Watchlist of stocks near an all-time-high **rim**; buy trigger = rim × 1.04.
- **Entry:** arm an SL-M buy-stop only when price is within `NEAR_PCT` (default 1%) of the rim.
- **Exit:** initial SL −6% → **breakeven at +10%** → **book half + trail at +20%** → trail 10% below the high → exit on stop.
- Telegram alerts on every event.

## Files
| File | Role |
|---|---|
| `run.py` | main runner — live quotes, market-hours loop, paper/live switch, report |
| `bot.py` | strategy: near-gate entry + SL/trail exit (+ scripted demo: `python bot.py`) |
| `cup_sl_engine.py` | pure SL / breakeven / partial / trail logic |
| `broker.py` | `PaperBroker` (simulated fills) + `LiveBroker` (real Fyers) — one interface |
| `fyers_client.py` | Fyers REST wrapper (quotes / orders / positions) |
| `auth.py` | daily Fyers login → `token.json` |
| `notify.py` | Telegram alerts |
| `demo.py` | read-only connectivity check |
| `test_live_order.py` | SAFE live-order check (limit 5% below market → verify → cancel) |
| `watchlist.csv` | `symbol,rim,trigger,qty` |

## Setup
```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install --no-deps fyers-apiv3==3.1.15
cp .env.example .env      # then fill FYERS_APP_ID / FYERS_SECRET (+ Telegram, optional)
```

## Daily run
```bash
python auth.py     # Fyers tokens are daily — log in, saves token.json
python run.py      # MODE from .env (paper by default)
```
`python demo.py` verifies connectivity (read-only). `python bot.py` runs the scripted lifecycle test (no market needed).

## Modes (in `.env`)
- `MODE=paper` — simulated fills against **live** quotes. Safe. Default.
- `MODE=live` — **real** Fyers orders. Also requires `CONFIRM_LIVE=YES` (guard).
  Live order placement needs **algo/API order permission enabled on your Fyers app** (enable at myapi.fyers.in / Fyers support).

## Docker
```bash
docker compose up -d --build
```
Mounts `.env`, `token.json`, `watchlist.csv` and state from the host (secrets never baked into the image).
**Daily token:** run `python auth.py` on the host each morning — the container picks up the new `token.json`.

## Security
`.env` and `token.json` hold secrets and are **git-ignored** — never commit them. Rotate the Fyers secret if it ever leaks.
