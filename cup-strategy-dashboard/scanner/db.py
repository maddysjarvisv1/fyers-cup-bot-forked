import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "/app/database/cup.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Queue: stocks near ATH waiting for breakout trigger
    c.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL UNIQUE,
            ath REAL NOT NULL,
            ath_date TEXT NOT NULL,
            buy_price REAL NOT NULL,       -- ATH * 1.04
            sl_price REAL NOT NULL,        -- buy_price * 0.94
            qty INTEGER NOT NULL,
            capital REAL NOT NULL,
            current_price REAL,
            pct_from_ath REAL,
            added_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Positions: active trades
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            buy_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            initial_sl REAL NOT NULL,
            qty_total INTEGER NOT NULL,
            qty_remaining INTEGER NOT NULL,
            capital REAL NOT NULL,
            fixed_loss REAL NOT NULL,
            ath REAL NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            -- ACTIVE | BE_SET | PARTIAL_BOOKED | TRAILING | CLOSED
            highest_price REAL,
            trail_sl REAL,
            pnl REAL DEFAULT 0,
            entry_date TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            notes TEXT
        )
    """)

    # Trade log: closed trades history
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            buy_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            qty INTEGER NOT NULL,
            pnl REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            -- SL_HIT | BE_EXIT | PARTIAL_BOOK | TRAIL_EXIT | MANUAL
            entry_date TEXT,
            exit_date TEXT DEFAULT (datetime('now'))
        )
    """)

    # Config: runtime overrides stored in DB (fallback to .env)
    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Initialised")


if __name__ == "__main__":
    init_db()
