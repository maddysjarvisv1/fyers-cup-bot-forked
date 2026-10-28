#!/usr/bin/env python3
"""
NSE cup/breakout screener for the fork's watchlist.

Pulls the OFFICIAL NSE equity universe (nsearchives EQUITY_L.csv — proven
reachable), then screens each liquid EQ-series stock for the cup setup the
bot trades: a 3+yr-old all-time-high with current price within threshold
of that ATH (breakout candidate).

Jarvis-safe: writes to a CANDIDATES file (nse_screener_candidates.csv), NEVER
touches the protected watchlist.csv. Adoption into the live watchlist is a
separate, explicit step.

Usage:
  nse_screener.py scan [max_symbols]   # full scan (default all); cap for a quick test
  nse_screener.py list                  # show last candidates
"""
from __future__ import annotations
import os, sys, io, time, csv, json, tempfile
import urllib.request
import urllib.error
import datetime

import pandas as pd
import yfinance as yf

from broker import align_tick

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
CAND = os.path.join(HERE, "nse_screener_candidates.csv")
STATUS = os.path.join(HERE, "nse_screener_status.json")

CAND_FIELDS = [
    "symbol", "ath", "ath_date", "buy_price", "sl_price",
    "qty", "capital", "current", "pct_from_ath",
]

ATH_MIN_YEARS = 3
QUEUE_PCT = 5.0        # queue if current price within 5% below ATH
BREAK_PCT = 2.0        # straight-to-buy if within 2% above ATH
FIXED_LOSS = 750.0
SL_PCT = 6.0 / 100
MIN_AVG_VOLUME = 100000

# Verdict thresholds (Fix 1: fail loudly on broken/empty scans)
MIN_SYMBOLS_OK = 2000
DEGRADED_ERROR_RATE = 0.02   # > 2% and <= 5% error rate -> DEGRADED
MAX_ERROR_RATE = 0.05        # > 5% error rate (or ok < 2000) -> FAILED

ERROR_TYPES = ("auth", "http", "parse", "empty")
FILTER_REASONS = (
    "insufficient_history",
    "low_volume",
    "ath_too_recent",
    "below_queue_threshold",
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def now_ist():
    return datetime.datetime.now(IST)


def fetch_nse_symbols(max_n=None):
    req = urllib.request.Request(NSE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        df = pd.read_csv(io.StringIO(r.read().decode("latin-1", "ignore")))
    df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
    # keep liquid EQ series equities (exclude SME, ST, etc.)
    if "SERIES" in df.columns:
        df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
    syms = df["SYMBOL"].dropna().astype(str).str.strip().tolist()
    if max_n:
        syms = syms[:max_n]
    print(f"[Screener] NSE universe: {len(syms)} EQ-series symbols fetched @ {now_ist()}")
    return syms


def calc_qty(buy_price):
    risk = buy_price * SL_PCT
    return max(int(FIXED_LOSS // risk), 1)


def classify_error(exc: BaseException) -> str:
    """Classify a per-symbol fetch/evaluation failure into auth/http/parse/empty."""
    if isinstance(exc, PermissionError):
        return "auth"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "auth"
        return "http"
    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status_code in (401, 403):
        return "auth"
    if status_code is not None:
        return "http"

    if isinstance(exc, pd.errors.EmptyDataError):
        return "empty"

    msg = f"{type(exc).__name__} {exc}".lower()
    if any(tok in msg for tok in (
        "auth", "unauthorized", "forbidden", "401", "403",
        "crumb", "invalid token", "expired token", "credential",
    )):
        return "auth"
    if any(tok in msg for tok in (
        "http", "429", "rate limit", "rate_limit", "ratelimit",
        "too many requests", "timeout", "timed out", "connection",
        "network", "500", "502", "503", "504", "404", "urlerror",
    )):
        return "http"
    if any(tok in msg for tok in (
        "empty", "no data", "no price data", "no timezone", "delisted",
    )):
        return "empty"
    if isinstance(exc, (
        KeyError, ValueError, TypeError, IndexError, AttributeError,
        json.JSONDecodeError, pd.errors.ParserError,
    )):
        return "parse"
    if any(tok in msg for tok in ("parse", "json", "decode", "column", "schema", "nan")):
        return "parse"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return "http"
    return "http"


def _evaluate_symbol_core(sym: str) -> dict:
    """Evaluate a symbol and return its explicit outcome classification.

    Returns a dict with:
      - symbol: str
      - outcome: 'candidate' | 'filtered' | 'error'
      - row: dict | None (populated whenever ATH/volume/history checks pass,
             even if pct_from_ath < -QUEUE_PCT, so screen_symbol() stays
             backward-compatible)
      - candidate: dict | None (populated only when outcome == 'candidate')
      - error_type: 'auth' | 'http' | 'parse' | 'empty' | None
      - filter_reason: str | None
      - error_detail: str | None
    """
    try:
        h = yf.Ticker(f"{sym}.NS").history(period="max", auto_adjust=True)
    except Exception as exc:
        err_type = classify_error(exc)
        return {
            "symbol": sym,
            "outcome": "error",
            "row": None,
            "candidate": None,
            "error_type": err_type,
            "filter_reason": None,
            "error_detail": f"{type(exc).__name__}: {exc}",
        }

    try:
        if h is None:
            return {
                "symbol": sym,
                "outcome": "error",
                "row": None,
                "candidate": None,
                "error_type": "empty",
                "filter_reason": None,
                "error_detail": "history() returned None",
            }
        if not isinstance(h, pd.DataFrame) or not hasattr(h, "empty"):
            return {
                "symbol": sym,
                "outcome": "error",
                "row": None,
                "candidate": None,
                "error_type": "parse",
                "filter_reason": None,
                "error_detail": f"unexpected history type: {type(h).__name__}",
            }
        if h.empty or len(h) == 0:
            return {
                "symbol": sym,
                "outcome": "error",
                "row": None,
                "candidate": None,
                "error_type": "empty",
                "filter_reason": None,
                "error_detail": "empty history DataFrame (0 rows)",
            }
        for req_col in ("High", "Close", "Volume"):
            if req_col not in h.columns:
                return {
                    "symbol": sym,
                    "outcome": "error",
                    "row": None,
                    "candidate": None,
                    "error_type": "parse",
                    "filter_reason": None,
                    "error_detail": f"missing required column: {req_col}",
                }
        if h["High"].dropna().empty or h["Close"].dropna().empty:
            return {
                "symbol": sym,
                "outcome": "error",
                "row": None,
                "candidate": None,
                "error_type": "parse",
                "filter_reason": None,
                "error_detail": "High/Close series contains only NaN",
            }

        # Fetched and parsed cleanly (counts as ok). Apply legitimate strategy filters:
        if len(h) < 10:
            return {
                "symbol": sym,
                "outcome": "filtered",
                "row": None,
                "candidate": None,
                "error_type": None,
                "filter_reason": "insufficient_history",
                "error_detail": None,
            }

        avg_vol = float(h["Volume"].tail(60).mean())
        if pd.isna(avg_vol) or avg_vol < MIN_AVG_VOLUME:
            return {
                "symbol": sym,
                "outcome": "filtered",
                "row": None,
                "candidate": None,
                "error_type": None,
                "filter_reason": "low_volume",
                "error_detail": None,
            }

        ath_idx = h["High"].idxmax()
        ath = float(h["High"].max())
        if pd.isna(ath) or ath <= 0:
            return {
                "symbol": sym,
                "outcome": "error",
                "row": None,
                "candidate": None,
                "error_type": "parse",
                "filter_reason": None,
                "error_detail": f"invalid ATH value: {ath!r}",
            }
        adate = ath_idx.date() if hasattr(ath_idx, "date") else datetime.date.fromisoformat(str(ath_idx)[:10])
        cutoff = now_ist().date() - datetime.timedelta(days=ATH_MIN_YEARS * 365)
        if adate > cutoff:
            return {
                "symbol": sym,
                "outcome": "filtered",
                "row": None,
                "candidate": None,
                "error_type": None,
                "filter_reason": "ath_too_recent",
                "error_detail": None,
            }

        cur = float(h["Close"].iloc[-1])
        if pd.isna(cur):
            return {
                "symbol": sym,
                "outcome": "error",
                "row": None,
                "candidate": None,
                "error_type": "parse",
                "filter_reason": None,
                "error_detail": "latest Close is NaN",
            }
        pct = (cur - ath) / ath * 100
        buy = align_tick(ath * 1.04)
        sl = align_tick(buy * (1 - SL_PCT))
        row = {
            "symbol": sym, "ath": round(ath, 2), "ath_date": str(adate),
            "buy_price": buy, "sl_price": sl,
            "qty": calc_qty(buy), "capital": round(buy * calc_qty(buy), 2),
            "current": round(cur, 2), "pct_from_ath": round(pct, 2),
        }
        if row["pct_from_ath"] < -QUEUE_PCT:
            return {
                "symbol": sym,
                "outcome": "filtered",
                "row": row,
                "candidate": None,
                "error_type": None,
                "filter_reason": "below_queue_threshold",
                "error_detail": None,
            }
        return {
            "symbol": sym,
            "outcome": "candidate",
            "row": row,
            "candidate": row,
            "error_type": None,
            "filter_reason": None,
            "error_detail": None,
        }
    except Exception as exc:
        err_type = classify_error(exc)
        return {
            "symbol": sym,
            "outcome": "error",
            "row": None,
            "candidate": None,
            "error_type": err_type,
            "filter_reason": None,
            "error_detail": f"{type(exc).__name__}: {exc}",
        }


def evaluate_symbol(sym: str) -> dict:
    """Public wrapper returning the per-symbol outcome classification dict."""
    return _evaluate_symbol_core(sym)


def screen_symbol(sym):
    """Return candidate dict if it's a cup setup, else None."""
    res = _evaluate_symbol_core(sym)
    row = res["row"]
    if row is None:
        return None
    row["buy_price"] = align_tick(row["buy_price"])
    row["sl_price"] = align_tick(row["sl_price"])
    return row


def compute_verdict(
    symbols_total: int,
    ok: int,
    error: int,
    min_symbols_ok: int = MIN_SYMBOLS_OK,
) -> str:
    """Compute run verdict:
    - OK only when symbols_ok >= 2000 AND error/symbols_total <= 0.02
    - DEGRADED when symbols_ok >= 2000 AND 0.02 < error/symbols_total <= 0.05
    - FAILED otherwise (symbols_ok < 2000 or error/symbols_total > 0.05 or symbols_total <= 0)
    """
    if symbols_total <= 0 or ok < min_symbols_ok:
        return "FAILED"
    error_rate = error / symbols_total
    if error_rate > MAX_ERROR_RATE:
        return "FAILED"
    if error_rate > DEGRADED_ERROR_RATE:
        return "DEGRADED"
    return "OK"


def _write_json_atomic(path: str, payload: dict) -> None:
    target_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=target_dir,
            prefix=".status_",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tf:
            tmp_path = tf.name
            json.dump(payload, tf, indent=2)
            tf.write("\n")
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def scan(
    max_n=None,
    cap=None,
    symbols=None,
    cand_path=None,
    status_path=None,
    sleep_s=0.25,
    min_symbols_ok=MIN_SYMBOLS_OK,
):
    started_dt = now_ist()
    started_at = started_dt.isoformat()
    out_cand = cand_path or CAND
    out_status = status_path or STATUS

    by_error_type = {k: 0 for k in ERROR_TYPES}
    filtered_by_reason = {k: 0 for k in FILTER_REASONS}
    hits = []
    ok_count = 0
    error_count = 0

    try:
        syms = list(symbols) if symbols is not None else fetch_nse_symbols(max_n)
    except Exception as exc:
        err_type = classify_error(exc)
        by_error_type[err_type] = by_error_type.get(err_type, 0) + 1
        finished_at = now_ist().isoformat()
        status = {
            "started_at": started_at,
            "finished_at": finished_at,
            "symbols_total": 0,
            "ok": 0,
            "symbols_ok": 0,
            "error": 1,
            "error_rate": 1.0,
            "by_error_type": by_error_type,
            "filtered_by_reason": filtered_by_reason,
            "candidates": 0,
            "verdict": "FAILED",
            "universe_error": f"{type(exc).__name__}: {exc}",
        }
        _write_json_atomic(out_status, status)
        print(
            f"[Screener] SUMMARY: verdict={status['verdict']} | "
            f"symbols_total=0 ok=0 error=1 (100.00%) | "
            f"by_error_type={by_error_type} | "
            f"filtered_by_reason={filtered_by_reason} | "
            f"candidates=0 | status={os.path.basename(out_status)}"
        )
        return status

    if max_n and symbols is not None:
        syms = syms[:max_n]

    t0 = time.time()
    for i, s in enumerate(syms):
        if i % 100 == 0:
            print(
                f"  [{i}/{len(syms)}] {time.time()-t0:.0f}s  "
                f"ok={ok_count} err={error_count} hits={len(hits)}"
            )
        if cap and len(hits) >= cap:
            print("  (cap reached)")
            break
        if sleep_s and sleep_s > 0:
            time.sleep(sleep_s)  # respectful ~4 req/s to yfinance
        res = _evaluate_symbol_core(s)
        outcome = res["outcome"]
        if outcome == "error":
            error_count += 1
            et = res["error_type"] or "http"
            by_error_type[et] = by_error_type.get(et, 0) + 1
        elif outcome == "filtered":
            ok_count += 1
            fr = res["filter_reason"] or "below_queue_threshold"
            filtered_by_reason[fr] = filtered_by_reason.get(fr, 0) + 1
        elif outcome == "candidate":
            ok_count += 1
            hits.append(res["candidate"])

    symbols_total = len(syms)
    error_rate = round(error_count / symbols_total, 6) if symbols_total > 0 else 1.0
    verdict = compute_verdict(
        symbols_total=symbols_total,
        ok=ok_count,
        error=error_count,
        min_symbols_ok=min_symbols_ok,
    )
    finished_at = now_ist().isoformat()

    with open(out_cand, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(hits[0].keys()) if hits else CAND_FIELDS)
        wr.writeheader()
        wr.writerows(hits)

    status = {
        "started_at": started_at,
        "finished_at": finished_at,
        "symbols_total": symbols_total,
        "ok": ok_count,
        "symbols_ok": ok_count,
        "error": error_count,
        "error_rate": error_rate,
        "by_error_type": by_error_type,
        "filtered_by_reason": filtered_by_reason,
        "candidates": len(hits),
        "verdict": verdict,
    }
    _write_json_atomic(out_status, status)

    print(f"\n[!] {len(hits)} cup/breakout candidates -> {os.path.basename(out_cand)}")
    for h in hits[:10]:
        print(f"   {h['symbol']:<12} ATH {h['ath']:>9} {h['ath_date']}  cur {h['current']:>9}  {h['pct_from_ath']:>6}%  qty {h['qty']}")
    if len(hits) > 10:
        print(f"   ... and {len(hits)-10} more")

    pct_err = (error_count / symbols_total * 100.0) if symbols_total > 0 else 100.0
    print(
        f"[Screener] SUMMARY: verdict={verdict} | "
        f"symbols_total={symbols_total} ok={ok_count} error={error_count} ({pct_err:.2f}%) | "
        f"by_error_type={by_error_type} | "
        f"filtered_by_reason={filtered_by_reason} | "
        f"candidates={len(hits)} | status={os.path.basename(out_status)}"
    )
    return status


def list_cands():
    if not os.path.exists(CAND):
        print("no candidates yet — run: nse_screener.py scan"); return
    rows = list(csv.DictReader(open(CAND)))
    print(f"{len(rows)} candidates in {os.path.basename(CAND)}:")
    for r in rows[:15]:
        print(f"   {r['symbol']:<12} {r.get('ath',''):>9}  cur {r.get('current',''):>9}  {r.get('pct_from_ath',''):>6}s  qty {r.get('qty','')}")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == "scan":
        max_n = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        status = scan(max_n=max_n)
        return 0 if status["verdict"] in ("OK", "DEGRADED") else 1
    elif cmd == "list":
        list_cands()
        return 0
    else:
        print(__doc__)
        return 2


if __name__ == "__main__":
    sys.exit(main())