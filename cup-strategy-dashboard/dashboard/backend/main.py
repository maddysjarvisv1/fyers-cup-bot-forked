import os, sys, math, io, csv
import sqlite3
from typing import Optional
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import yfinance as yf
import requests


def telegram_send(text: str):
    """Send a Telegram message using env creds. No-op (prints) if not configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat or token == "your_bot_token_here":
        print(f"[TELEGRAM disabled] {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"[TELEGRAM error] {e}")

# Share DB module with scanner
sys.path.append("/app/database")
DB_PATH = os.getenv("DB_PATH", "/app/database/cup.db")

app = FastAPI(title="Cup Strategy API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── DB HELPERS ────────────────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ── CONFIG ────────────────────────────────────────────────────────────────────
CONFIG_DEFAULTS = {
    "CAPITAL_PER_TRADE": os.getenv("CAPITAL_PER_TRADE", "50000"),
    "FIXED_LOSS":         os.getenv("FIXED_LOSS", "750"),
    "SL_PCT":             os.getenv("SL_PCT", "6"),
    "BREAKEVEN_TRIGGER_PCT": os.getenv("BREAKEVEN_TRIGGER_PCT", "10"),
    "PARTIAL_BOOK_TRIGGER_PCT": os.getenv("PARTIAL_BOOK_TRIGGER_PCT", "20"),
    "TRAIL_PCT":          os.getenv("TRAIL_PCT", "10"),
    "QUEUE_THRESHOLD_PCT": os.getenv("QUEUE_THRESHOLD_PCT", "5"),
    "INTRADAY_SCAN_INTERVAL_MIN": os.getenv("INTRADAY_SCAN_INTERVAL_MIN", "15"),
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID", ""),
}


def get_config():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    conn.close()
    cfg = dict(CONFIG_DEFAULTS)
    for r in rows:
        cfg[r["key"]] = r["value"]
    return cfg


@app.get("/config")
def read_config():
    return get_config()


class ConfigUpdate(BaseModel):
    updates: dict


@app.post("/config")
def update_config(body: ConfigUpdate):
    conn = get_conn()
    for k, v in body.updates.items():
        conn.execute("""
            INSERT INTO config(key, value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
        """, (k, str(v)))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ── QUEUE ─────────────────────────────────────────────────────────────────────
@app.get("/queue")
def get_queue():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM queue ORDER BY pct_from_ath DESC"
    ).fetchall()
    conn.close()
    return rows_to_list(rows)


@app.delete("/queue/{symbol}")
def remove_from_queue(symbol: str):
    conn = get_conn()
    conn.execute("DELETE FROM queue WHERE symbol=?", (symbol.upper(),))
    conn.commit()
    conn.close()
    return {"status": "removed"}


class AddStockBody(BaseModel):
    symbol: str
    ath: Optional[float] = None


@app.post("/queue/add")
def add_stock(body: AddStockBody):
    """Add single stock manually. ATH optional — fetched if not provided."""
    symbol = body.symbol.upper().strip()
    sl_pct = float(get_config()["SL_PCT"]) / 100
    fixed_loss = float(get_config()["FIXED_LOSS"])

    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        hist   = ticker.history(period="max", auto_adjust=True)
        if hist.empty:
            raise HTTPException(400, f"No data for {symbol}")

        ath      = body.ath if body.ath else float(hist["High"].max())
        ath_idx  = (hist["High"] - ath).abs().idxmin()
        ath_date = str(ath_idx.date())
        cur      = float(hist["Close"].iloc[-1])
        buy_price = round(ath * 1.04, 2)
        sl_price  = round(buy_price * (1 - sl_pct), 2)
        qty       = max(1, math.floor(fixed_loss / (buy_price * sl_pct)))
        pct       = round(((cur - ath) / ath) * 100, 2)

        conn = get_conn()
        conn.execute("""
            INSERT INTO queue (symbol, ath, ath_date, buy_price, sl_price,
                               qty, capital, current_price, pct_from_ath)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                ath=excluded.ath, ath_date=excluded.ath_date,
                buy_price=excluded.buy_price, sl_price=excluded.sl_price,
                qty=excluded.qty, capital=excluded.capital,
                current_price=excluded.current_price,
                pct_from_ath=excluded.pct_from_ath,
                updated_at=datetime('now')
        """, (symbol, round(ath,2), ath_date, buy_price, sl_price,
              qty, round(buy_price*qty,2), round(cur,2), pct))
        conn.commit()
        conn.close()
        return {"status": "added", "symbol": symbol, "buy_price": buy_price,
                "sl_price": sl_price, "qty": qty, "ath": ath}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/queue/import")
async def import_csv(file: UploadFile = File(...)):
    """
    Bulk import from CSV or Excel.
    Expected columns: symbol, ath (optional)
    """
    content = await file.read()
    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(400, f"Cannot read file: {e}")

    df.columns = [c.strip().lower() for c in df.columns]
    if "symbol" not in df.columns:
        raise HTTPException(400, "File must have a 'symbol' column")

    results = []
    for _, row in df.iterrows():
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym:
            continue
        ath_val = float(row["ath"]) if "ath" in df.columns and pd.notna(row.get("ath")) else None
        body = AddStockBody(symbol=sym, ath=ath_val)
        try:
            res = add_stock(body)
            results.append({"symbol": sym, "status": "ok", **res})
        except Exception as e:
            results.append({"symbol": sym, "status": "error", "detail": str(e)})

    return {"imported": len(results), "results": results}


# ── POSITIONS ─────────────────────────────────────────────────────────────────
@app.get("/positions")
def get_positions(status: Optional[str] = None):
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status=? ORDER BY entry_date DESC",
            (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status != 'CLOSED' ORDER BY entry_date DESC"
        ).fetchall()
    conn.close()
    return rows_to_list(rows)


class ManualPositionBody(BaseModel):
    symbol: str
    buy_price: float
    qty: int
    ath: float
    notes: Optional[str] = None


@app.post("/positions/add")
def add_position_manually(body: ManualPositionBody):
    """Manually open a position (for trades placed directly on Fyers)."""
    cfg = get_config()
    sl_pct     = float(cfg["SL_PCT"]) / 100
    fixed_loss = float(cfg["FIXED_LOSS"])
    sl_price   = round(body.buy_price * (1 - sl_pct), 2)
    capital    = round(body.buy_price * body.qty, 2)

    conn = get_conn()
    conn.execute("""
        INSERT INTO positions (symbol, buy_price, sl_price, initial_sl,
                               qty_total, qty_remaining, capital, fixed_loss,
                               ath, highest_price, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (body.symbol.upper(), body.buy_price, sl_price, sl_price,
          body.qty, body.qty, capital, fixed_loss,
          body.ath, body.buy_price, body.notes))
    conn.commit()
    pos_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Remove from queue if it was there (position now tracked)
    conn.execute("DELETE FROM queue WHERE symbol=?", (body.symbol.upper(),))
    conn.commit()
    conn.close()

    telegram_send(
        f"🟢 <b>BUY recorded: {body.symbol.upper()}</b>\n"
        f"Price: ₹{body.buy_price:,.2f}\n"
        f"Qty: {body.qty} | Capital: ₹{capital:,.0f}\n"
        f"SL: ₹{sl_price:,.2f} ({cfg['SL_PCT']}% below)\n"
        f"<i>Tracked in app — no real order placed.</i>"
    )
    return {"status": "opened", "id": pos_id, "sl_price": sl_price}


class UpdatePositionBody(BaseModel):
    status: Optional[str] = None
    sl_price: Optional[float] = None
    trail_sl: Optional[float] = None
    qty_remaining: Optional[int] = None
    notes: Optional[str] = None


@app.patch("/positions/{pos_id}")
def update_position(pos_id: int, body: UpdatePositionBody):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Nothing to update")
    set_clause = ", ".join(f"{k}=?" for k in updates)
    set_clause += ", updated_at=datetime('now')"
    conn = get_conn()
    conn.execute(
        f"UPDATE positions SET {set_clause} WHERE id=?",
        list(updates.values()) + [pos_id]
    )
    conn.commit()
    conn.close()
    return {"status": "updated"}


@app.delete("/positions/{pos_id}")
def close_position_manually(pos_id: int, exit_price: float = 0, reason: str = "MANUAL"):
    conn = get_conn()
    pos = conn.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
    if not pos:
        raise HTTPException(404, "Position not found")
    pnl = (exit_price - pos["buy_price"]) * pos["qty_remaining"] if exit_price else 0
    conn.execute(
        "UPDATE positions SET status='CLOSED', pnl=pnl+?, updated_at=datetime('now') WHERE id=?",
        (pnl, pos_id)
    )
    conn.execute("""
        INSERT INTO trade_log (symbol, buy_price, exit_price, qty, pnl, exit_reason, entry_date)
        VALUES (?,?,?,?,?,?,?)
    """, (pos["symbol"], pos["buy_price"], exit_price,
          pos["qty_remaining"], pnl, reason, pos["entry_date"]))
    conn.commit()
    conn.close()
    return {"status": "closed", "pnl": pnl}


# ── TRADE LOG ─────────────────────────────────────────────────────────────────
@app.get("/trades")
def get_trades():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trade_log ORDER BY exit_date DESC"
    ).fetchall()
    conn.close()
    return rows_to_list(rows)


# ── STATS ─────────────────────────────────────────────────────────────────────
@app.get("/stats")
def get_stats():
    conn = get_conn()
    trades = rows_to_list(conn.execute("SELECT * FROM trade_log").fetchall())
    positions = rows_to_list(conn.execute(
        "SELECT * FROM positions WHERE status != 'CLOSED'"
    ).fetchall())
    conn.close()

    total   = len(trades)
    wins    = [t for t in trades if t["pnl"] > 0]
    losses  = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    open_pnl  = sum(p.get("pnl", 0) or 0 for p in positions)

    return {
        "total_trades":   total,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins)/total*100, 1) if total else 0,
        "total_pnl":      round(total_pnl, 2),
        "open_pnl":       round(open_pnl, 2),
        "avg_win":        round(sum(t["pnl"] for t in wins)/len(wins), 2) if wins else 0,
        "avg_loss":       round(sum(t["pnl"] for t in losses)/len(losses), 2) if losses else 0,
        "open_positions": len(positions),
        "queue_count":    conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0] if False else
                          get_conn().execute("SELECT COUNT(*) FROM queue").fetchone()[0],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
