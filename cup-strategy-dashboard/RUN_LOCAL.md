# Running locally without Docker (Windows)

The README assumes Docker. This machine has no Docker, so the three services
run natively using the Python venv (`.venv`) and Node that are already set up.

## One-time setup (already done)
- Python venv created at `.venv` with 3.13-compatible dependencies
  (pinned versions in the `requirements.txt` files target Linux/Docker wheels
  and won't build on Python 3.13 without a C++ compiler).
- Frontend deps installed (`dashboard/frontend/node_modules`).
- SQLite DB + schema created at `database/cup.db`.

To redo setup from scratch:
```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install "pandas>=2.2.3" "numpy>=2.1" fastapi `
  "uvicorn[standard]" python-multipart openpyxl yfinance requests python-dotenv apscheduler pytz
.venv\Scripts\python.exe scanner\db.py        # creates database\cup.db
cd dashboard\frontend; npm install; cd ..\..
```

## Start the app (run each in its own terminal)
```powershell
.\run-backend.ps1     # FastAPI  -> http://localhost:8000   (docs at /docs)
.\run-frontend.ps1    # Dashboard -> http://localhost:5173
.\run-scanner.ps1     # Optional. Full NSE scan on startup (~10+ min, hits yfinance hard)
```

- Dashboard:  http://localhost:5173  (Vite dev server; proxies `/api` -> :8000)
- API docs:   http://localhost:8000/docs

## Notes / differences from the Docker setup
- Docker exposed the dashboard on :3000 via nginx. In dev it's Vite on :5173.
- `DB_PATH` is overridden to the local `database\cup.db` (Docker used
  `/app/database/cup.db`). The run scripts handle this.
- The apps don't call `load_dotenv`; the run scripts read `.env` and export it.
- Telegram alerts are disabled until you set real `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` in `.env` (until then alerts just print to the console).
- You don't need the scanner running to use the dashboard. You can add stocks
  manually (Queue tab -> Add Stock) or via CSV import; those hit yfinance live.
