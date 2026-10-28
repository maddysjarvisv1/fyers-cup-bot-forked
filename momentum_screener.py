#!/usr/bin/env python3
"""
Momentum-breakout screener — Fyers data only (FyersClient.history); never yfinance.

Signal, all four on session `asof`:
  1. close >= highest High of the prior 52 weeks (excluding the current breakout leg)
     -> high_kind "52W", or "ATH" when the close also clears every High in the full
     Fyers history before the current breakout leg (walked back for hits only)
  2. volume >= 2 x the 50-session average volume (signal day excluded from the average)
  3. close in the top third of the day's range: (close - low) / (high - low) >= 2/3
  4. 50-session average volume >= 1e5

Owner-confirmed row semantics (override earlier draft's H*1.003 / 1xATR / fixed-750 rules):
  rim            = align_tick(prev_ath)                  (PREVIOUS ATH before current breakout leg, never latest high)
  trigger        = align_tick(rim * 1.04)                (entry = previous ATH x 1.04, tick-aligned to 0.05)
  stop           = align_tick(trigger * (1 - 0.06))      (6% below entry, tick-aligned to 0.05)
  risk_per_share = round(trigger - stop, 2)
  qty            = nearest valid integer in [Rs 750, Rs 900] defined-loss band within capital_budget
  time_stop_days = 5                                     (no breakeven within 5 sessions -> market exit)
  valid_until    = next weekday after asof               (a breakout level is good for one session)

Take-profit ladder (in cup_sl_engine.py, unchanged):
  +10% breakeven (SL -> entry), +20% book half, then 10% trailing stop below running high.

Usage:
  momentum_screener.py scan [max_symbols] [--asof YYYY-MM-DD] [--min-ok N] [--interval S] [--out-dir D] [--fixture PATH]
  momentum_screener.py list [--out-dir D]
Writes momentum_candidates.csv (watchlist rows; run.py loads it when MOMENTUM_WATCHLIST is set)
and momentum_status.json (machine-readable verdict). Exit: 0 = OK/DEGRADED, 2 = FAILED.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import errno
import io
import json
import math
import os
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from broker import align_tick
from fyers_client import FyersClient, FyersError, _ok, is_t2t_symbol
from watchlist_sync import resolve_capital_budget

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
CAND = os.path.join(HERE, "momentum_candidates.csv")
STATUS = os.path.join(HERE, "momentum_status.json")

# --- detection ---------------------------------------------------------------
LOOKBACK_DAYS = 365            # one Fyers D-resolution request (cap: 366 days)
HISTORY_COVERAGE_DAYS = 357    # first bar must be at least this old, else no real 52-week high exists
WEEKS52_DAYS = 364
VOL_AVG_SESSIONS = 50
VOL_SPIKE_MULT = 2.0
MIN_AVG_VOLUME = 100000        # same floor as nse_screener.MIN_AVG_VOLUME
TOP_RANGE_FRAC = 2.0 / 3.0
ATH_MAX_CHUNKS = 30            # ATH label: walk back <= 30 x 365d, hits only

# --- owner-confirmed row semantics -------------------------------------------
TICK = 0.05
ENTRY_BUFFER = 0.04            # entry = previous ATH x 1.04, tick-aligned
SL_PCT = 0.06                  # stop  = 6% below entry, tick-aligned
LOSS_BAND_MIN = 750.0          # minimum defined loss per trade (Rs)
LOSS_BAND_MAX = 900.0          # maximum defined loss per trade (Rs)
ATR_PERIOD = 14
TIME_STOP_SESSIONS = 5

# --- run health --------------------------------------------------------------
MIN_SYMBOLS_OK = 2000
OK_MAX_ERROR_RATE = 0.02
DEGRADED_MAX_ERROR_RATE = 0.05
STALE_MAX_FRAC = 0.5
EARLY_ABORT_ERRORS = 25        # first 25 symbols all failing = token/feed down; stop hammering Fyers
REQ_INTERVAL = 0.31            # ~190 req/min, under the Fyers per-minute cap
SESSION_CLOSE_GRACE = dt.time(15, 45)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"   # same universe as nse_screener.py

ROW_FIELDS = (
    "symbol", "rim", "trigger", "qty", "kind", "stop", "risk_per_share", "risk",
    "time_stop_days", "valid_until", "asof", "high_kind", "ath_check", "atr14",
    "close", "vol_mult", "range_pos",
)


def now_ist():
    return dt.datetime.now(IST)


def ceil_tick(price, tick=TICK):
    t = Decimal(str(tick))
    return float((Decimal(str(float(price))) / t).to_integral_value(rounding=ROUND_CEILING) * t)


def floor_tick(price, tick=TICK):
    t = Decimal(str(tick))
    return float((Decimal(str(float(price))) / t).to_integral_value(rounding=ROUND_FLOOR) * t)


def next_weekday(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def default_asof(now=None):
    """Today once the session has closed (15:45 IST grace), else the previous weekday."""
    now = now or now_ist()
    d = now.date()
    if d.weekday() < 5 and now.time() >= SESSION_CLOSE_GRACE:
        return d
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


# --- data ----------------------------------------------------------------------
@dataclass(frozen=True)
class Bar:
    day: dt.date
    o: float
    h: float
    l: float
    c: float
    v: float


class BadCandles(ValueError):
    """Fyers returned s=ok but candle rows that cannot be trusted."""


def parse_candles(resp, what="history"):
    """Fyers history envelope -> day-sorted, day-deduped Bars. s=no_data -> []."""
    if isinstance(resp, dict) and resp.get("s") == "no_data":
        return []
    _ok(resp, what)
    rows = resp.get("candles")
    if rows is None:
        raise BadCandles(f"{what}: s=ok but no 'candles' key")
    by_day = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise BadCandles(f"{what}: malformed candle {row!r}")
        ts, o, h, l, c, v = row[:6]
        b = Bar(dt.datetime.fromtimestamp(int(ts), IST).date(),
                float(o), float(h), float(l), float(c), float(v))
        if min(b.o, b.h, b.l, b.c) <= 0 or b.v < 0 or b.h < max(b.o, b.c, b.l) or b.l > min(b.o, b.c):
            raise BadCandles(f"{what}: inconsistent OHLCV {row!r}")
        by_day[b.day] = b
    return [by_day[d] for d in sorted(by_day)]


class Throttle:
    def __init__(self, interval):
        self.interval = float(interval)
        self._last = 0.0

    def wait(self):
        if self.interval <= 0:
            return
        gap = time.monotonic() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.monotonic()


def fetch_daily(client, sym, start, end, throttle):
    throttle.wait()
    return parse_candles(client.history(sym, start.isoformat(), end.isoformat(), resolution="D"),
                         f"history {sym}")


def fetch_universe(max_n=None):
    req = urllib.request.Request(NSE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("latin-1", "ignore")
    syms = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {str(k).strip().upper().replace(" ", "_"): (v or "").strip() for k, v in raw.items() if k}
        if row.get("SYMBOL") and row.get("SERIES", "EQ") == "EQ":
            syms.append(row["SYMBOL"])
    if not syms:
        raise ValueError(f"NSE universe empty from {NSE_URL}")
    return syms[:max_n] if max_n else syms


def all_time_prior_high(client, sym, before, throttle, max_chunks=ATH_MAX_CHUNKS):
    """Highest High strictly before `before`, walking back 365d chunks until Fyers has no data."""
    best = 0.0
    end = before - dt.timedelta(days=1)
    for _ in range(max_chunks):
        start = end - dt.timedelta(days=LOOKBACK_DAYS)
        bars = fetch_daily(client, sym, start, end, throttle)
        if not bars:
            break
        best = max(best, max(b.h for b in bars))
        end = start - dt.timedelta(days=1)
    return best


# --- detection (pure) ------------------------------------------------------------
def atr(bars, period=ATR_PERIOD):
    """Simple-average true range of the last `period` bars (needs period+1 bars)."""
    if len(bars) < period + 1:
        raise ValueError(f"atr: need {period + 1} bars, have {len(bars)}")
    trs = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c))
           for p, b in zip(bars[-period - 1:-1], bars[-period:])]
    return sum(trs) / period


# Rule: previous ATH is the max High strictly before the current breakout leg (asof plus any contiguous prior bars with strictly rising highs and closes).
def breakout_leg_start(bars):
    """Index where the current breakout leg starts: always excludes bars[-1] (asof) and any
    immediately preceding contiguous bars with strictly rising highs and closes."""
    if not bars:
        return 0
    leg_start = len(bars) - 1
    while (
        leg_start > 1
        and bars[leg_start].h >= bars[leg_start - 1].h
        and bars[leg_start - 1].h > bars[leg_start - 2].h
        and bars[leg_start - 1].c > bars[leg_start - 2].c
    ):
        leg_start -= 1
    return leg_start


def previous_ath_from_bars(bars, older_high=0.0):
    """Compute the previous all-time high strictly before the current breakout leg."""
    prior_leg = bars[:breakout_leg_start(bars)]
    if not prior_leg:
        return float(older_high)
    return max(float(older_high), max(b.h for b in prior_leg))


@dataclass
class Signal:
    symbol: str
    asof: str
    close: float
    high: float
    low: float
    volume: float
    avg_vol50: float
    vol_mult: float
    range_pos: float
    prior_52w_high: float
    atr14: float
    prev_ath: float = 0.0
    high_kind: str = "52W"
    ath_check: str = "not_run"

    def __post_init__(self):
        if self.prev_ath <= 0.0:
            self.prev_ath = float(self.prior_52w_high)


def detect(symbol, bars, asof):
    """Daily bars ending at `asof` -> (Signal, "hit") or (None, reject_reason)."""
    if not bars:
        return None, "no_data"
    today = bars[-1]
    if today.day != asof:
        return None, "stale_data"
    prior = bars[:-1]
    leg_start = breakout_leg_start(bars)
    prior_leg = bars[:leg_start]
    if (len(prior) < max(VOL_AVG_SESSIONS, ATR_PERIOD)
            or not prior_leg
            or prior[0].day > asof - dt.timedelta(days=HISTORY_COVERAGE_DAYS)):
        return None, "insufficient_history"
    avg_vol = sum(b.v for b in prior[-VOL_AVG_SESSIONS:]) / VOL_AVG_SESSIONS
    if avg_vol < MIN_AVG_VOLUME:
        return None, "illiquid"
    if today.v < VOL_SPIKE_MULT * avg_vol:
        return None, "no_volume_spike"
    rng = today.h - today.l
    if rng <= 0:
        return None, "no_range"
    range_pos = (today.c - today.l) / rng
    if range_pos < TOP_RANGE_FRAC:
        return None, "weak_close"
    since = asof - dt.timedelta(days=WEEKS52_DAYS)
    year = [b.h for b in prior_leg if b.day >= since]
    if not year:
        return None, "insufficient_history"
    prior_52w_high = max(year)
    prev_ath = max(b.h for b in prior_leg)
    if today.c < prior_52w_high:
        return None, "below_52w_high"
    return Signal(
        symbol=symbol, asof=asof.isoformat(), close=today.c, high=today.h, low=today.l,
        volume=today.v, avg_vol50=avg_vol, vol_mult=today.v / avg_vol, range_pos=range_pos,
        prior_52w_high=prior_52w_high, atr14=atr(bars), prev_ath=prev_ath,
    ), "hit"


def size_qty_in_band(entry, stop, capital_budget=None, loss_min=LOSS_BAND_MIN, loss_max=LOSS_BAND_MAX):
    """Size integer share count so defined loss `qty * (entry - stop)` lands inside
    [loss_min, loss_max] (Rs 750 to Rs 900) and notional `qty * entry <= capital_budget`.

    Selects the valid integer count in the band nearest to the band midpoint (Rs 825).
    Returns `(qty, None)` on success, or `(None, reason)` if the band cannot be satisfied.
    """
    entry = float(entry)
    stop = float(stop)
    risk_per_share = round(entry - stop, 2)
    if entry <= 0 or stop <= 0 or risk_per_share <= 0:
        return None, "invalid_risk"

    q_lo = max(1, math.ceil((loss_min - 1e-9) / risk_per_share))
    q_hi = math.floor((loss_max + 1e-9) / risk_per_share)
    band_qs = [
        q for q in range(q_lo, q_hi + 1)
        if loss_min - 1e-9 <= round(q * risk_per_share, 2) <= loss_max + 1e-9
    ]
    if not band_qs:
        return None, (
            f"loss_band_unreachable: no integer qty puts risk/share {risk_per_share:.2f} "
            f"inside Rs [{loss_min:.2f}, {loss_max:.2f}]"
        )

    if capital_budget is not None:
        budget = float(capital_budget)
        affordable_qs = [
            q for q in band_qs
            if round(q * entry, 2) <= round(budget, 2)
        ]
        if not affordable_qs:
            min_needed = round(band_qs[0] * entry, 2)
            return None, (
                f"insufficient_budget_for_loss_band: min band qty {band_qs[0]} at entry {entry:.2f} "
                f"requires Rs {min_needed:.2f} > available budget Rs {budget:.2f}"
            )
    else:
        affordable_qs = band_qs

    target_loss = (loss_min + loss_max) / 2.0
    best_q = min(affordable_qs, key=lambda q: (abs(round(q * risk_per_share, 2) - target_loss), q))
    return best_q, None


def compute_cup_levels(prev_ath, capital_budget=None, tick=TICK, loss_min=LOSS_BAND_MIN, loss_max=LOSS_BAND_MAX):
    """Owner-confirmed parameter math from previous ATH:
      rim            = align_tick(prev_ath)
      entry/trigger  = align_tick(rim * 1.04)
      stop           = align_tick(entry * (1 - 0.06))
      risk_per_share = round(entry - stop, 2)
      qty            = nearest valid integer count in [loss_min, loss_max] within capital_budget
      risk           = round(qty * risk_per_share, 2)
    """
    rim = align_tick(float(prev_ath), tick)
    entry = align_tick(rim * (1.0 + ENTRY_BUFFER), tick)
    stop = align_tick(entry * (1.0 - SL_PCT), tick)
    risk_per_share = round(entry - stop, 2)
    qty, reason = size_qty_in_band(
        entry, stop, capital_budget=capital_budget, loss_min=loss_min, loss_max=loss_max
    )
    risk = round(qty * risk_per_share, 2) if qty is not None else None
    return {
        "prev_ath": rim,
        "rim": rim,
        "entry": entry,
        "trigger": entry,
        "stop": stop,
        "risk_per_share": risk_per_share,
        "qty": qty,
        "risk": risk,
        "unsizable_reason": reason,
    }


def build_row_with_reason(sig, capital_budget, asof, tick=TICK):
    """Signal -> (momentum watchlist row, None) or (None, unsizable_reason)."""
    prev_ath = sig.prev_ath if getattr(sig, "prev_ath", 0.0) > 0 else sig.prior_52w_high
    levels = compute_cup_levels(prev_ath, capital_budget=capital_budget, tick=tick)
    if levels["qty"] is None:
        return None, levels["unsizable_reason"]
    return {
        "symbol": sig.symbol,
        "rim": levels["rim"],
        "trigger": levels["trigger"],
        "qty": levels["qty"],
        "kind": "momentum",
        "stop": levels["stop"],
        "risk_per_share": levels["risk_per_share"],
        "risk": levels["risk"],
        "time_stop_days": TIME_STOP_SESSIONS,
        "valid_until": next_weekday(asof).isoformat(),
        "asof": asof.isoformat(),
        "high_kind": sig.high_kind,
        "ath_check": sig.ath_check,
        "atr14": round(sig.atr14, 2),
        "close": sig.close,
        "vol_mult": round(sig.vol_mult, 2),
        "range_pos": round(sig.range_pos, 3),
    }, None


def build_row(sig, capital_budget, asof, tick=TICK):
    """Signal -> momentum watchlist row, or None when it cannot satisfy the loss band and budget."""
    row, _ = build_row_with_reason(sig, capital_budget, asof, tick=tick)
    return row


# --- outcome accounting ----------------------------------------------------------------
@dataclass
class ScanStats:
    outcomes: Counter = field(default_factory=Counter)      # "hit" / reject reason -> count
    errors: Counter = field(default_factory=Counter)        # exception type -> count
    error_samples: dict = field(default_factory=dict)       # exception type -> first 3 "SYM: msg"
    ath_errors: Counter = field(default_factory=Counter)

    def count(self, reason):
        self.outcomes[reason] += 1

    def error(self, sym, exc):
        kind = type(exc).__name__
        self.errors[kind] += 1
        samples = self.error_samples.setdefault(kind, [])
        if len(samples) < 3:
            samples.append(f"{sym}: {exc}"[:300])

    @property
    def symbols_ok(self):
        return sum(self.outcomes.values())

    @property
    def n_errors(self):
        return sum(self.errors.values())

    @property
    def attempted(self):
        return self.symbols_ok + self.n_errors

    @property
    def error_rate(self):
        return self.n_errors / self.attempted if self.attempted else 1.0


def screen_one(client, sym, asof, stats, throttle):
    """Evaluate one symbol. This is the counter wrapper that replaces
    nse_screener.screen_symbol's `except Exception: return None` (nse_screener.py:93-94):
    every outcome is counted — a clean reject by its reason, a failure by its
    exception type — so "no candidates" can never be confused with "nothing worked"."""
    if is_t2t_symbol(sym):
        stats.count("t2t_excluded")
        return None
    try:
        bars = fetch_daily(client, sym, asof - dt.timedelta(days=LOOKBACK_DAYS), asof, throttle)
        sig, reason = detect(sym, bars, asof)
    except Exception as exc:          # counted by type — never swallowed
        stats.error(sym, exc)
        return None
    stats.count(reason)
    if sig is not None:
        try:
            older = all_time_prior_high(client, sym, bars[0].day, throttle)
            full_prev_ath = previous_ath_from_bars(bars, older_high=older)
            if sig.close >= full_prev_ath:
                sig.high_kind = "ATH"
                sig.prev_ath = full_prev_ath
            else:
                sig.high_kind = "52W"
            sig.ath_check = "ok"
        except Exception as exc:      # 52W clause is proven; only the ATH label is unproven
            stats.ath_errors[type(exc).__name__] += 1
            sig.ath_check = f"error:{type(exc).__name__}"
    return sig


def judge(stats, *, min_ok=MIN_SYMBOLS_OK, aborted=False):
    """OK: symbols_ok >= min_ok and errors <= 2%. DEGRADED: errors in (2%, 5%]. Else FAILED."""
    reasons = []
    if aborted:
        reasons.append(f"aborted: first {stats.n_errors} symbols all failed")
    if stats.symbols_ok < min_ok:
        reasons.append(f"symbols_ok {stats.symbols_ok} < {min_ok}")
    if stats.error_rate > DEGRADED_MAX_ERROR_RATE:
        reasons.append(f"error_rate {stats.error_rate:.2%} > {DEGRADED_MAX_ERROR_RATE:.0%}")
    stale = stats.outcomes.get("stale_data", 0)
    if stats.symbols_ok and stale / stats.symbols_ok > STALE_MAX_FRAC:
        reasons.append(f"stale_data on {stale}/{stats.symbols_ok} symbols — asof is not the "
                       f"last session (holiday / feed lag)")
    if reasons:
        return "FAILED", reasons
    if stats.error_rate > OK_MAX_ERROR_RATE:
        return "DEGRADED", [f"error_rate {stats.error_rate:.2%} in "
                            f"({OK_MAX_ERROR_RATE:.0%}, {DEGRADED_MAX_ERROR_RATE:.0%}]"]
    return "OK", []


# --- artifacts -----------------------------------------------------------------------
def _atomic_write(path, text):
    target = os.path.abspath(path)
    d = os.path.dirname(target) or "."
    probe = d
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if probe and os.path.exists(probe):
        if not os.path.isdir(probe):
            raise NotADirectoryError(
                errno.ENOTDIR, f"Target directory path is not a directory: {d}", probe
            )
        st = os.stat(probe)
        mode_unwritable = (st.st_mode & 0o222) == 0 or (
            hasattr(os, "geteuid") and os.geteuid() == st.st_uid and (st.st_mode & 0o200) == 0
        )
        if not os.access(probe, os.W_OK | os.X_OK) or mode_unwritable:
            raise PermissionError(
                errno.EACCES, f"Target directory is not writable: {d}", probe
            )
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".momentum_", suffix=".tmp")
    except OSError as exc:
        raise type(exc)(
            exc.errno,
            f"Target directory is not writable ({d}): {exc.strerror or exc}",
            exc.filename or d,
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _rows_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=ROW_FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def run_scan(client, symbols, asof, *, capital_budget, min_ok=MIN_SYMBOLS_OK, interval=REQ_INTERVAL,
             cand_path=CAND, status_path=STATUS, log=print):
    started = now_ist()
    stats, throttle, signals, aborted = ScanStats(), Throttle(interval), [], False
    t0 = time.time()
    for i, sym in enumerate(symbols):
        if i % 100 == 0:
            log(f"  [{i}/{len(symbols)}] {time.time() - t0:.0f}s ok={stats.symbols_ok} "
                f"err={stats.n_errors} hits={len(signals)}")
        sig = screen_one(client, sym, asof, stats, throttle)
        if sig is not None:
            signals.append(sig)
        if stats.symbols_ok == 0 and stats.n_errors >= EARLY_ABORT_ERRORS:
            aborted = True
            log(f"  ABORT: first {stats.n_errors} symbols all failed — {dict(stats.errors)}")
            break

    rows, unsizable, unsizable_reasons = [], [], {}
    for sig in sorted(signals, key=lambda s: s.vol_mult, reverse=True):
        row, reason = build_row_with_reason(sig, capital_budget, asof)
        if row:
            rows.append(row)
        else:
            unsizable.append(sig.symbol)
            unsizable_reasons[sig.symbol] = reason

    verdict, reasons = judge(stats, min_ok=min_ok, aborted=aborted)
    wrote = verdict != "FAILED"     # a FAILED run never publishes rows
    status = {
        "tool": "momentum_screener",
        "asof": asof.isoformat(),
        "started_at": started.isoformat(),
        "finished_at": now_ist().isoformat(),
        "universe": len(symbols),
        "attempted": stats.attempted,
        "symbols_ok": stats.symbols_ok,
        "errors": stats.n_errors,
        "error_rate": round(stats.error_rate, 4),
        "outcomes": dict(stats.outcomes),
        "errors_by_type": dict(stats.errors),
        "error_samples": stats.error_samples,
        "ath_lookup_errors": dict(stats.ath_errors),
        "aborted": aborted,
        "candidates": len(rows),
        "unsizable": unsizable,
        "unsizable_reasons": unsizable_reasons,
        "capital_budget": capital_budget,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "exit_code": 2 if verdict == "FAILED" else 0,
        "candidates_path": cand_path if wrote else None,
        "thresholds": {"min_symbols_ok": min_ok, "ok_max_error_rate": OK_MAX_ERROR_RATE,
                       "degraded_max_error_rate": DEGRADED_MAX_ERROR_RATE},
    }
    if wrote:
        _atomic_write(cand_path, _rows_csv(rows))
    _atomic_write(status_path, json.dumps(status, indent=2, sort_keys=True) + "\n")
    return status, rows


def _fail_setup(status_path, asof, err_type, msg):
    key = f"setup:{err_type}"
    status = {
        "tool": "momentum_screener", "asof": asof.isoformat(), "finished_at": now_ist().isoformat(),
        "universe": 0, "attempted": 0, "symbols_ok": 0, "errors": 1, "error_rate": 1.0,
        "outcomes": {}, "errors_by_type": {key: 1}, "error_samples": {key: [msg[:300]]},
        "aborted": True, "candidates": 0, "verdict": "FAILED", "exit_code": 2,
        "verdict_reasons": [f"setup failed: {err_type}: {msg[:300]}"], "candidates_path": None,
    }
    _atomic_write(status_path, json.dumps(status, indent=2, sort_keys=True) + "\n")
    print(f"[Momentum] FAILED at setup: {err_type}: {msg}", file=sys.stderr)
    return 2


class _FixtureFyersClient:
    """Offline fixture client for running momentum_screener without network or Fyers token."""
    def __init__(self, payload):
        self.series = payload.get("series") or {}
        self.errors = payload.get("errors") or {}
        self.raw = payload.get("raw") or {}

    def history(self, symbol, range_from, range_to, resolution="D"):
        if symbol in self.errors:
            err = self.errors[symbol]
            kind = err.get("type", "FyersError") if isinstance(err, dict) else "FyersError"
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if kind == "ConnectionError":
                raise ConnectionError(msg)
            raise FyersError(msg)
        if symbol in self.raw:
            return self.raw[symbol]
        lo, hi = dt.date.fromisoformat(range_from), dt.date.fromisoformat(range_to)
        rows = [
            r for r in self.series.get(symbol, [])
            if lo <= dt.datetime.fromtimestamp(int(r[0]), IST).date() <= hi
        ]
        return {"s": "ok", "code": 200, "candles": rows} if rows else {"s": "no_data", "candles": []}


def list_cands(path=CAND):
    if not os.path.exists(path):
        print("no momentum candidates yet — run: momentum_screener.py scan")
        return
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"{len(rows)} momentum candidates in {os.path.basename(path)}:")
    for r in rows[:15]:
        print(f"   {r['symbol']:<12} {r['high_kind']:<3} rim {r['rim']:>9} trig {r['trigger']:>9} "
              f"stop {r['stop']:>9} qty {r['qty']:>5} vol x{r['vol_mult']}  valid {r['valid_until']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fyers-only momentum breakout screener")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("max_symbols", nargs="?", type=int, default=None)
    sc.add_argument("--asof", default=None, help="signal session YYYY-MM-DD (default: last closed session)")
    sc.add_argument("--min-ok", type=int, default=MIN_SYMBOLS_OK)
    sc.add_argument("--interval", type=float, default=REQ_INTERVAL)
    sc.add_argument("--out-dir", default=HERE)
    sc.add_argument("--fixture", default=None, help="offline fixture JSON path (no network/API calls)")
    lp = sub.add_parser("list")
    lp.add_argument("--out-dir", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "list":
        list_cands(os.path.join(a.out_dir, "momentum_candidates.csv") if a.out_dir else CAND)
        return 0

    cand_path = os.path.join(a.out_dir, "momentum_candidates.csv")
    status_path = os.path.join(a.out_dir, "momentum_status.json")
    now = now_ist()
    asof = dt.date.fromisoformat(a.asof) if a.asof else default_asof(now)
    if asof == now.date() and now.time() < SESSION_CLOSE_GRACE:
        return _fail_setup(status_path, asof, "SessionNotClosed",
                           f"asof {asof} is today and the session has not closed (now {now:%H:%M} IST)")
    try:
        if a.fixture:
            with open(a.fixture, encoding="utf-8") as f:
                payload = json.load(f)
            client = _FixtureFyersClient(payload)
            symbols = list(payload.get("symbols") or payload.get("series", {}).keys())
            if a.max_symbols:
                symbols = symbols[:a.max_symbols]
            budget = float(payload.get("capital_budget") or resolve_capital_budget())
        else:
            client = FyersClient.from_files()
            symbols = fetch_universe(a.max_symbols)
            budget = resolve_capital_budget()
    except Exception as exc:
        return _fail_setup(status_path, asof, type(exc).__name__, str(exc))

    print(f"[Momentum] asof {asof} | universe {len(symbols)} | CAPITAL_BUDGET Rs {budget:,.0f}")
    status, rows = run_scan(client, symbols, asof, capital_budget=budget, min_ok=a.min_ok,
                            interval=a.interval, cand_path=cand_path, status_path=status_path)
    print(f"\n[Momentum] verdict {status['verdict']} | ok {status['symbols_ok']} | "
          f"errors {status['errors']} ({status['error_rate']:.2%}) {status['errors_by_type']} | "
          f"candidates {status['candidates']}")
    for r in status["verdict_reasons"]:
        print(f"   reason: {r}")
    for r in rows[:10]:
        print(f"   {r['symbol']:<12} {r['high_kind']:<3} rim {r['rim']} trig {r['trigger']} "
              f"stop {r['stop']} qty {r['qty']} risk {r['risk']} vol x{r['vol_mult']}")
    return status["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
