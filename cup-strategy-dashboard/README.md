# Cup Strategy — Malkansview ATH Breakout System

## Setup in 3 steps

### 1. Configure
Edit `.env` with your settings:
```
CAPITAL_PER_TRADE=50000
FIXED_LOSS=750
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

To get Telegram credentials:
- Message @BotFather on Telegram → /newbot → copy token
- Message @userinfobot → copy your chat ID

### 2. Start
```bash
docker-compose up -d
```

### 3. Access
- Dashboard: http://your-server-ip:3000
- API docs:  http://your-server-ip:8000/docs

---

## How it works

**Scanner (Microservice 1)**
- On startup: scans all ~2000 NSE stocks for ATH setups (3yr rule)
- Nightly at 8PM IST: re-scans full universe
- Every 15min (market hours): checks queue stocks for buy triggers, checks open positions for SL/target hits

**Dashboard (Microservice 2)**
- Queue tab: all stocks near ATH waiting for breakout
- Positions tab: active trades with live SL tracking
- Trade Log: full history
- Config tab: change all parameters + sizing calculator

**Telegram Alerts**
- Queue: stock approaching ATH
- BUY: breakout triggered with full details
- SL HIT: loss amount
- +10%: risk-free alert
- +20%: partial booking confirmation
- Trail updates: silent (no notification sound)
- Errors: scanner failures

---

## Bulk Import
Go to Queue tab → Import CSV/Excel
CSV must have column: `symbol` (ATH column optional)

Example:
```
symbol,ath
FEDERALBNK,352.95
IRCTC,
CDSL,
```

---

## Phase 2 (Fyers API integration)
When ready, add to `.env`:
```
FYERS_APP_ID=your_app_id
FYERS_ACCESS_TOKEN=your_token
AUTO_TRADE=true
```
Scanner will then place GTT orders and modify SLs automatically.

---

## Update config without restart
All parameters can be changed from the Config tab in the dashboard.
Scanner picks up changes on next scan cycle (no restart needed).
