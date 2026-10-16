"""SQLite trade-outcome ledger + cup-lifecycle tracking for the cup-bot.

Stores every order / entry / stop-exit / partial / error as durable rows and
maintains a per-cup lifecycle status (the state shown on the dashboard):
    trade_ready   validated watchlist candidate, armed-line, no position
    getting_ready price within ``near_pct`` of its trigger (approaching), not yet in
    order        buy-stop placed, awaiting fill
    in_trade      position open (entry booked, protective stop live)
    stop_loss     stop-exit booked (with realised pnl)
    error         a lifecycle/order error was recorded for this cup

Flat JSON (paper_state/bot_state) remains the bot's *session* scratch; this
ledger is the *durable outcome* store the dashboard reads. DB file is
gitignored (``*.db``). Standard-library sqlite3 only.
"""
import os
import sqlite3
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    return datetime.now(IST).strftime("%Y-%m-%d")


STATUSES = ("trade_ready", "getting_ready", "order", "in_trade", "stop_loss", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cup_status (
    symbol      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    trigger     REAL,
    qty         INTEGER,
    rim         REAL,
    entry_price REAL,
    last_price  REAL,
    pnl         REAL DEFAULT 0,
    error       TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT, side TEXT, type TEXT,
    trigger     REAL, qty INTEGER, status TEXT,
    placed_at   TEXT, filled_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL, side TEXT, qty INTEGER,
    price REAL, reason TEXT, pnl REAL, ts TEXT
);
CREATE TABLE IF NOT EXISTS audit_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL, event TEXT NOT NULL,
    symbol  TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS missed_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    trigger     REAL,
    rim         REAL,
    qty         INTEGER,
    ltp         REAL,
    gap_pct     REAL,
    reason      TEXT,
    created_at  TEXT,
    UNIQUE(day, symbol)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_missed_entries_day_symbol
    ON missed_entries(day, symbol);
"""


class Ledger:
    """Durable trade-outcome store. Thread-safe writes via per-op connection."""

    def __init__(self, path="fyers_ledger.db"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._conn()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        return c

    def _exec(self, sql, params=()):
        c = self._conn()
        try:
            cur = c.execute(sql, params)
            c.commit()
            return cur.lastrowid
        finally:
            c.close()

    def _row(self, symbol):
        c = self._conn()
        try:
            r = c.execute("SELECT * FROM cup_status WHERE symbol=?", (symbol,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def _rows(self, sql, params=()):
        c = self._conn()
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        finally:
            c.close()

    # -- cup lifecycle -------------------------------------------------------
    def set_status(self, symbol, status, **fields):
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; want one of {STATUSES}")
        cur = self._row(symbol) or {}
        merged = {"symbol": symbol, **cur, **fields, "status": status, "updated_at": _now()
                  if "updated_at" not in fields else fields["updated_at"]}
        cols = ["symbol", "status", "trigger", "qty", "rim", "entry_price",
                "last_price", "pnl", "error", "updated_at"]
        row = tuple(merged.get(c) for c in cols)
        placeholders = ",".join("?" * len(cols))
        update_cols = ",".join(f"{c}=excluded.{c}" for c in cols if c != "symbol")
        self._exec(
            f"INSERT INTO cup_status ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {update_cols}", row)
        self.audit("status_changed", symbol, f"{status}")
        return self._row(symbol)

    def cup(self, symbol):
        return self._row(symbol)

    # -- events --------------------------------------------------------------
    def order_event(self, symbol, side, type_, trigger, qty, status, error=None):
        self._exec(
            "INSERT INTO orders (symbol, side, type, trigger, qty, status, placed_at, error) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (symbol, side, type_, trigger, qty, status, _now(), error))

    def trade_event(self, symbol, side, qty, price, reason, pnl=None):
        self._exec(
            "INSERT INTO trades (symbol, side, qty, price, reason, pnl, ts) VALUES (?,?,?,?,?,?,?)",
            (symbol, side, qty, price, reason, pnl, _now()))
        return pnl

    def audit(self, event, symbol=None, detail=None):
        self._exec(
            "INSERT INTO audit_events (ts, event, symbol, detail) VALUES (?,?,?,?)",
            (_now(), event, symbol, detail))

    def missed_entry(self, symbol, trigger, ltp, rim=None, qty=None,
                     gap_pct=None, reason="gapped_up_past_trigger", day=None):
        """Record a missed entry (gap-up past trigger). Idempotent per (day, symbol).

        Returns True when a new row was inserted, False if already recorded today.
        """
        symbol = str(symbol).upper()
        day = day or _today()
        trigger = float(trigger)
        ltp = float(ltp)
        if gap_pct is None:
            gap_pct = round((ltp - trigger) / trigger * 100.0, 4) if trigger else 0.0
        c = self._conn()
        try:
            cur = c.execute(
                "INSERT OR IGNORE INTO missed_entries "
                "(day, symbol, trigger, rim, qty, ltp, gap_pct, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (day, symbol, trigger, rim, qty, ltp, gap_pct, reason, _now()),
            )
            c.commit()
            inserted = cur.rowcount > 0
        finally:
            c.close()
        if inserted:
            self.audit("missed_entry", symbol,
                       f"{reason} ltp={ltp} trigger={trigger} gap_pct={gap_pct:.2f}%")
        return inserted

    # -- reads ---------------------------------------------------------------
    def trades_for(self, symbol, limit=50):
        return self._rows(
            "SELECT * FROM trades WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))

    def orders_for(self, symbol, limit=20):
        return self._rows(
            "SELECT * FROM orders WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))

    def missed_entries(self, day=None, symbol=None, limit=100):
        clauses, params = [], []
        if day:
            clauses.append("day=?")
            params.append(day)
        if symbol:
            clauses.append("symbol=?")
            params.append(str(symbol).upper())
        where = ("WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        params.append(limit)
        return self._rows(
            f"SELECT * FROM missed_entries {where}ORDER BY id DESC LIMIT ?",
            tuple(params),
        )

    def missed_for(self, symbol, limit=50):
        return self.missed_entries(symbol=symbol, limit=limit)

    def has_missed_entry(self, symbol, day=None):
        day = day or _today()
        rows = self._rows(
            "SELECT 1 FROM missed_entries WHERE day=? AND symbol=? LIMIT 1",
            (day, str(symbol).upper()),
        )
        return bool(rows)

    def audit_for(self, symbol=None, limit=50):
        if symbol:
            return self._rows("SELECT * FROM audit_events WHERE symbol=? "
                              "ORDER BY id DESC LIMIT ?", (symbol, limit))
        return self._rows("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))

    def status_counts(self):
        rows = self._rows("SELECT status, COUNT(*) n FROM cup_status GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def report(self, watchlist=None, prices=None, near_pct=0.02, max_positions=None):
        """Dashboard view: lifecycle group counts + per-cup rows.

        ``watchlist`` - [{symbol, trigger, qty, rim}] => pre-trade candidates.
        ``prices``    - {SYM: ltp} => lets pre-trade cups split ready / getting_ready.
        ``max_positions`` - position cap (if set) => drives the "caps full" hint.

        Each cup gets a ``fire_status``:
            in_trade   position open
            ready      price reached its trigger but NO open position (
                       breakout waiting to fire / held by the cap)
            near       within ``near_pct`` of its trigger (getting ready)
            cold       nowhere near
        ``awaiting`` lists the fire-ready cups not currently in a position.
        """
        counts = {s: 0 for s in STATUSES}
        cups = []
        if watchlist:
            for w in watchlist:
                sym = str(w["symbol"]).upper()
                stat = self._pre_state(sym, w, prices, near_pct)
                cup = self._row(sym) or {
                    "symbol": sym, "status": stat, "trigger": w.get("trigger"),
                    "rim": w.get("rim"), "qty": w.get("qty"),
                    "entry_price": None, "last_price": (prices or {}).get(sym),
                    "pnl": 0, "error": None, "updated_at": None}
                cup["fire_status"] = self._fire_state(cup, prices, near_pct)
                cups.append(cup)
        else:
            cups = self._rows("SELECT * FROM cup_status ORDER BY updated_at DESC")
            for c in cups:
                c["fire_status"] = self._fire_state(c, prices, near_pct)
        if cups:
            for c in cups:
                counts[c["status"]] = counts.get(c["status"], 0) + 1
        counts = {s: counts.get(s, 0) for s in STATUSES}
        awaiting = [c for c in cups
                    if c["fire_status"] == "ready"
                    and c["status"] not in ("in_trade", "stop_loss", "error")]
        missed = self.missed_entries()
        return {
            "counts": counts,
            "cups": cups,
            "awaiting": awaiting,
            "awaiting_count": len(awaiting),
            "cap_full": bool(max_positions and counts.get("in_trade", 0) >= max_positions),
            "max_positions": max_positions,
            "in_trade": counts.get("in_trade", 0),
            "missed_entries": missed,
            "missed_count": len(missed),
        }

    def _fire_state(self, cup, prices, near_pct):
        p = (prices or {}).get(cup.get("symbol")) if prices else None
        if p is None:
            p = cup.get("last_price")
        trig = cup.get("trigger")
        if cup.get("status") == "in_trade":
            return "in_trade"
        if p is not None and trig:
            if p >= trig:
                return "ready"
            if p >= trig * (1 - near_pct):
                return "near"
        return "cold"

    def _pre_state(self, sym, w, prices, near_pct):
        p = (prices or {}).get(sym)
        trig = w.get("trigger")
        if p is not None and trig and p >= trig * (1 - near_pct):
            return "getting_ready"
        return "trade_ready"