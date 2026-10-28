#!/usr/bin/env python3
"""
new_high_detector.py — Separate ALERT-ONLY 52-week / all-time high + volume breakout detector.

PURPOSE
-------
The cup screener (`nse_screener.py`) deliberately looks for 3+ year bases, so a stock
breaking out today on exceptional volume is invisible to it by design. This separate job
surfaces such names for human review — nothing else.

RISK SUB-LIMIT (HUMAN REVIEW ONLY — NEVER ENFORCED BY TRADING)
--------------------------------------------------------------
- Maximum capital allocation sub-limit: Rs 15,000 (RISK_SUB_LIMIT_RS = 15000.0)
- Maximum concurrent positions: at most ONE (1) position (MAX_POSITIONS_SUB_LIMIT = 1)
This script is strictly alert-only. It NEVER places, modifies, or cancels any order.

HARD BOUNDARIES
---------------
1. NEVER places, modifies, or cancels an order.
2. NEVER imports or touches the cup bot's order path (`bot.py`, `broker.py`,
   `cup_sl_engine.py`, `run.py`, `ledger.py`, `watchlist_sync.py`), `watchlist.csv`,
   or bot state.
3. NEVER writes rows in the cup watchlist format (`symbol,rim,trigger,qty` with
   `trigger = ATH * 1.04` is meaningless here). Writes its own schema to
   `new_high_alerts.csv` plus a machine-readable status artifact to
   `new_high_alerts_status.json` (and appends to `new_high_alerts_status.jsonl`).
4. NEVER uses `yfinance`. Uses the existing Fyers client path (`fyers_client.FyersClient`).
   Explicit Market-Data Capability Note:
   `FyersClient` (`fyers_client.py:L137`) exposes `.quotes(symbols)` (`fyers_client.py:L218`)
   and `.ltps(symbols)` (`fyers_client.py:L223`), which return single-point quote snapshots
   only and do NOT provide 50-day historical volume or 52-week/all-time daily OHLCV bars;
   nor does `FyersClient` define a `.history()` wrapper method. Daily historical candles
   require the underlying Fyers v3 SDK call `client.fy.history(...)` (`fyers_client.py:L142`,
   `resolution="1D"`, capped at 366 days per request by Fyers v3). If `.history` /
   `client.fy.history` is not available on the supplied client, this detector fails loudly
   and explicitly rather than silently substituting `yfinance` or another source.

DETECTION RULE (all four clauses must hold on settled daily data)
-----------------------------------------------------------------
1. `close` is at a 52-week high or an all-time high (records which one fired: `ATH` or `52W`),
2. `volume >= 2.0x` the 50-day average volume,
3. `close` is in the top third of the day's range (`(close - low) / (high - low) >= 2/3`),
4. 50-day average volume `>= 1e5` (100,000).
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

from fyers_client import FyersClient, FyersError, nse, plain

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))

ALERTS_CSV = os.path.join(HERE, "new_high_alerts.csv")
STATUS_JSON = os.path.join(HERE, "new_high_alerts_status.json")
STATUS_JSONL = os.path.join(HERE, "new_high_alerts_status.jsonl")

# Documentary risk sub-limit constants (for human review only; NEVER used to trade)
RISK_SUB_LIMIT_RS = 15000.0
MAX_POSITIONS_SUB_LIMIT = 1

# Detection thresholds (do not loosen)
MIN_BARS_50D = 50
TRADING_DAYS_52W = 252
MIN_AVG_VOLUME_50D = 100_000.0   # 1e5
MIN_VOLUME_RATIO = 2.0           # >= 2.0x 50-day average volume
TOP_THIRD_THRESHOLD = 2.0 / 3.0  # close in top third of day's range

# Verdict thresholds (matches Fix 1)
MIN_SYMBOLS_OK = 2000
DEGRADED_ERROR_RATE = 0.02
MAX_ERROR_RATE = 0.05

ALERT_FIELDS = [
    "symbol",
    "date",
    "close",
    "day_high",
    "day_low",
    "range_pos",
    "volume",
    "avg_vol_50d",
    "vol_ratio",
    "high_type",
    "prior_52w_high",
    "prior_ath",
]

ERROR_TYPES = ("auth", "http", "parse", "empty")
FILTER_REASONS = (
    "insufficient_history",
    "low_average_volume",
    "not_new_high",
    "low_volume",
    "close_not_in_top_third",
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

_FORBIDDEN_ORDER_METHODS = (
    "place_buy_stop",
    "place_buy_gtt",
    "place_sell_stop",
    "place_market_sell",
    "modify_order",
    "cancel_order",
    "place_order",
    "place_gtt_order",
    "place_basket_orders",
    "place_multileg_order",
    "exit_positions",
)


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(IST)


def fetch_nse_symbols(max_n: int | None = None) -> list[str]:
    req = urllib.request.Request(NSE_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("latin-1", "ignore")
    reader = csv.DictReader(io.StringIO(text))
    syms: list[str] = []
    for row in reader:
        norm = {k.strip().upper().replace(" ", "_"): (v or "").strip() for k, v in row.items() if k}
        if "SERIES" in norm and norm["SERIES"] != "EQ":
            continue
        sym = norm.get("SYMBOL", "")
        if sym:
            syms.append(sym)
    if max_n:
        syms = syms[:max_n]
    return syms


class ReadOnlyFyersDataAdapter:
    """Read-only market-data facade over FyersClient.

    Explicitly blocks all order-placement, modification, and cancellation methods.
    Uses FyersClient.quotes() + underlying FyersModel.history() for settled daily candles.
    If historical candle retrieval is unavailable on the client, raises an explicit error
    explaining that FyersClient.quotes() alone only provides single-day snapshots and
    cannot supply 50-day average volume or 52-week/ATH history.
    """

    def __init__(self, client, years_of_history: int = 5):
        self._client = client
        self._years_of_history = max(1, int(years_of_history))

    def __getattr__(self, name: str):
        if name in _FORBIDDEN_ORDER_METHODS:
            raise PermissionError(
                f"new_high_detector is strictly alert-only; order method {name!r} is forbidden"
            )
        raise AttributeError(f"{type(self).__name__!r} has no attribute {name!r}")

    def quotes(self, symbols: list[str]) -> dict:
        if not hasattr(self._client, "quotes"):
            raise FyersError("Supplied Fyers client does not implement read-only .quotes()")
        return self._client.quotes(symbols)

    def fetch_settled_daily_bars(
        self,
        symbol: str,
        as_of_date: datetime.date | None = None,
    ) -> list[dict]:
        """Fetch settled daily OHLCV bars for `symbol` via Fyers client path.

        Checks:
        1. If the client exposes `.history(symbol=...)` or `.history(data)` directly, uses it.
        2. Else if the client exposes `.fy.history(data)` (standard `FyersClient` wrapping
           `fyersModel.FyersModel`), queries settled `1D` candles in <=365-day chunks.
        3. Otherwise raises an explicit `FyersError` stating that `FyersClient` only exposes
           `.quotes()`/`.ltps()` (single-day quote snapshot) and daily historical candles are
           not available on this client instance — never falls back to `yfinance`.
        """
        fyers_sym = nse(symbol)
        end_date = as_of_date or now_ist().date()

        if hasattr(self._client, "history") and callable(getattr(self._client, "history")):
            resp = self._client.history(symbol=fyers_sym, resolution="1D", as_of_date=end_date)
            return _normalize_history_response(resp)

        fy = getattr(self._client, "fy", None)
        if fy is not None and hasattr(fy, "history") and callable(getattr(fy, "history")):
            all_bars_by_date: dict[str, dict] = {}
            chunk_end = end_date
            for _ in range(self._years_of_history):
                chunk_start = chunk_end - datetime.timedelta(days=364)
                payload = {
                    "symbol": fyers_sym,
                    "resolution": "1D",
                    "date_format": "1",
                    "range_from": chunk_start.isoformat(),
                    "range_to": chunk_end.isoformat(),
                    "cont_flag": "1",
                }
                resp = fy.history(payload)
                chunk_bars = _normalize_history_response(resp)
                for b in chunk_bars:
                    all_bars_by_date[b["date"]] = b
                chunk_end = chunk_start - datetime.timedelta(days=1)
            return [all_bars_by_date[d] for d in sorted(all_bars_by_date.keys())]

        raise FyersError(
            "Explicit Fyers capability error: FyersClient in fyers_client.py only exposes "
            ".quotes()/.ltps() (single-day quote snapshots) and this client has no .history() "
            "or .fy.history() method for 50-day volume / 52-week / ATH daily candles. "
            "Refusing to substitute yfinance."
        )


def _normalize_history_response(resp) -> list[dict]:
    """Normalize a Fyers history response or list of bar dicts/tuples into sorted daily bars."""
    if resp is None:
        return []
    if isinstance(resp, list):
        raw_candles = resp
    elif isinstance(resp, dict):
        status = resp.get("s")
        if status == "no_data":
            return []
        if status is not None and status != "ok":
            code = resp.get("code")
            msg = resp.get("message") or "history request failed"
            if code in (-8, -15, -16, -17, 401, 403) or "auth" in str(msg).lower() or "token" in str(msg).lower():
                raise PermissionError(f"Fyers auth failure (code={code}): {msg}")
            raise FyersError(f"Fyers history failed: code={code} msg={msg} raw={resp}")
        if "candles" not in resp:
            raise ValueError(f"Malformed Fyers history response (missing 'candles'): {resp!r}")
        raw_candles = resp["candles"]
    else:
        raise TypeError(f"Unexpected history response type: {type(resp).__name__}")

    if raw_candles is None:
        return []

    bars: list[dict] = []
    for item in raw_candles:
        if isinstance(item, dict):
            dt_raw = item.get("date") or item.get("timestamp") or item.get("ts")
            o = float(item["open"])
            h = float(item["high"])
            l = float(item["low"])
            c = float(item["close"])
            v = float(item["volume"])
        elif isinstance(item, (list, tuple)) and len(item) >= 6:
            dt_raw, o, h, l, c, v = item[0], float(item[1]), float(item[2]), float(item[3]), float(item[4]), float(item[5])
        else:
            raise ValueError(f"Malformed candle entry: {item!r}")

        if any(math.isnan(x) or math.isinf(x) for x in (o, h, l, c, v)):
            raise ValueError(f"NaN/Inf in candle entry: {item!r}")
        if h < l:
            raise ValueError(f"Invalid candle with high ({h}) < low ({l}): {item!r}")

        if isinstance(dt_raw, (int, float)):
            dt_str = datetime.datetime.fromtimestamp(float(dt_raw), tz=IST).date().isoformat()
        elif isinstance(dt_raw, datetime.datetime):
            dt_str = dt_raw.date().isoformat()
        elif isinstance(dt_raw, datetime.date):
            dt_str = dt_raw.isoformat()
        elif isinstance(dt_raw, str) and dt_raw.strip():
            dt_str = dt_raw.strip()[:10]
        else:
            raise ValueError(f"Missing or invalid date in candle entry: {item!r}")

        bars.append({
            "date": dt_str,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })

    return bars


def classify_error(exc: BaseException) -> str:
    """Classify per-symbol failure into auth / http / parse / empty."""
    if isinstance(exc, PermissionError):
        return "auth"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "auth"
        return "http"
    msg = f"{type(exc).__name__} {exc}".lower()
    if any(tok in msg for tok in (
        "auth", "unauthorized", "forbidden", "401", "403",
        "token", "credential", "code=-8", "code=-15", "code=-16", "code=-17",
    )):
        return "auth"
    if any(tok in msg for tok in (
        "empty", "no data", "no_data", "0 bars",
    )):
        return "empty"
    if isinstance(exc, (KeyError, ValueError, TypeError, IndexError, json.JSONDecodeError)):
        return "parse"
    if any(tok in msg for tok in ("parse", "malformed", "nan", "column", "schema")):
        return "parse"
    return "http"


def evaluate_bars(
    symbol: str,
    bars: list[dict] | list[list],
    prior_ath_override: float | None = None,
) -> dict:
    """Evaluate settled daily bars for `symbol` against the 4 new-high + volume clauses.

    Clauses (all must hold on settled daily data):
      1. average volume (50-day) >= 1e5 (100,000)
      2. close is at a 52-week high or an all-time high (state which one fired: 'ATH' or '52W')
      3. volume >= 2.0x the 50-day average volume
      4. close is in the top third of the day's range: (close - low) / (high - low) >= 2/3
    """
    try:
        norm_bars = _normalize_history_response(bars)
    except Exception as exc:
        return {
            "symbol": symbol,
            "outcome": "error",
            "candidate": None,
            "error_type": classify_error(exc),
            "filter_reason": None,
            "error_detail": f"{type(exc).__name__}: {exc}",
        }

    if not norm_bars:
        return {
            "symbol": symbol,
            "outcome": "error",
            "candidate": None,
            "error_type": "empty",
            "filter_reason": None,
            "error_detail": "0 daily bars returned",
        }

    if len(norm_bars) < MIN_BARS_50D:
        return {
            "symbol": symbol,
            "outcome": "filtered",
            "candidate": None,
            "error_type": None,
            "filter_reason": "insufficient_history",
            "error_detail": None,
        }

    latest = norm_bars[-1]
    prior_bars = norm_bars[:-1]

    # 50-day average volume: use the 50 settled sessions preceding today when >=51 bars
    # exist (or trailing 50 bars when exactly 50 bars exist).
    vol_window = prior_bars[-MIN_BARS_50D:] if len(prior_bars) >= MIN_BARS_50D else norm_bars[-MIN_BARS_50D:]
    avg_vol_50d = sum(b["volume"] for b in vol_window) / len(vol_window)

    # Clause 4: 50-day average volume >= 1e5
    if avg_vol_50d < MIN_AVG_VOLUME_50D:
        return {
            "symbol": symbol,
            "outcome": "filtered",
            "candidate": None,
            "error_type": None,
            "filter_reason": "low_average_volume",
            "error_detail": None,
        }

    # Clause 1: close is at a 52-week high or an all-time high (state which one fired)
    prior_52w_bars = prior_bars[-TRADING_DAYS_52W:]
    prior_52w_high = max(b["high"] for b in prior_52w_bars)
    prior_ath = max(b["high"] for b in prior_bars)
    if prior_ath_override is not None:
        prior_ath = max(prior_ath, float(prior_ath_override))

    has_multi_year_history = (
        len(prior_bars) > TRADING_DAYS_52W or prior_ath_override is not None
    )
    close_price = latest["close"]
    is_52w_high = close_price >= prior_52w_high
    is_ath = close_price >= prior_ath

    if not is_52w_high and not is_ath:
        return {
            "symbol": symbol,
            "outcome": "filtered",
            "candidate": None,
            "error_type": None,
            "filter_reason": "not_new_high",
            "error_detail": None,
        }

    if is_ath and has_multi_year_history:
        high_type = "ATH"
    else:
        high_type = "52W"

    # Clause 2: volume >= 2.0x the 50-day average volume
    vol_ratio = latest["volume"] / avg_vol_50d if avg_vol_50d > 0 else 0.0
    if vol_ratio + 1e-9 < MIN_VOLUME_RATIO:
        return {
            "symbol": symbol,
            "outcome": "filtered",
            "candidate": None,
            "error_type": None,
            "filter_reason": "low_volume",
            "error_detail": None,
        }

    # Clause 3: close is in the top third of the day's range
    day_range = latest["high"] - latest["low"]
    if day_range <= 0:
        return {
            "symbol": symbol,
            "outcome": "filtered",
            "candidate": None,
            "error_type": None,
            "filter_reason": "close_not_in_top_third",
            "error_detail": None,
        }
    range_pos = (close_price - latest["low"]) / day_range
    if range_pos + 1e-9 < TOP_THIRD_THRESHOLD:
        return {
            "symbol": symbol,
            "outcome": "filtered",
            "candidate": None,
            "error_type": None,
            "filter_reason": "close_not_in_top_third",
            "error_detail": None,
        }

    alert_row = {
        "symbol": plain(symbol),
        "date": latest["date"],
        "close": round(close_price, 2),
        "day_high": round(latest["high"], 2),
        "day_low": round(latest["low"], 2),
        "range_pos": round(range_pos, 4),
        "volume": int(round(latest["volume"])),
        "avg_vol_50d": round(avg_vol_50d, 2),
        "vol_ratio": round(vol_ratio, 2),
        "high_type": high_type,
        "prior_52w_high": round(prior_52w_high, 2),
        "prior_ath": round(prior_ath, 2),
    }
    return {
        "symbol": plain(symbol),
        "outcome": "candidate",
        "candidate": alert_row,
        "error_type": None,
        "filter_reason": None,
        "error_detail": None,
    }


def compute_verdict(
    symbols_total: int,
    ok: int,
    error: int,
    min_symbols_ok: int = MIN_SYMBOLS_OK,
) -> str:
    if symbols_total <= 0 or ok < min_symbols_ok:
        return "FAILED"
    error_rate = error / symbols_total
    if error_rate > MAX_ERROR_RATE:
        return "FAILED"
    if error_rate > DEGRADED_ERROR_RATE:
        return "DEGRADED"
    return "OK"


def _write_status_artifacts(status_path: str, status_jsonl_path: str | None, payload: dict) -> None:
    target_dir = os.path.dirname(os.path.abspath(status_path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=target_dir,
            prefix=".nh_status_",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tf:
            tmp_path = tf.name
            json.dump(payload, tf, indent=2)
            tf.write("\n")
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, status_path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if status_jsonl_path:
        with open(status_jsonl_path, "a", encoding="utf-8") as jf:
            jf.write(json.dumps(payload) + "\n")


def run_detector(
    client=None,
    symbols: list[str] | None = None,
    bars_by_symbol: dict[str, list] | None = None,
    alerts_path: str | None = None,
    status_path: str | None = None,
    status_jsonl_path: str | None = None,
    max_n: int | None = None,
    sleep_s: float = 0.0,
    min_symbols_ok: int = MIN_SYMBOLS_OK,
) -> dict:
    """Run the alert-only new-high + volume detector across `symbols`.

    Writes alerts to `alerts_path` (`new_high_alerts.csv` by default) and writes/appends
    the JSON status artifact to `status_path` (`new_high_alerts_status.json`) and
    `status_jsonl_path` (`new_high_alerts_status.jsonl`).
    """
    started_at = now_ist().isoformat()
    out_alerts = alerts_path or ALERTS_CSV
    out_status = status_path or STATUS_JSON
    out_jsonl = status_jsonl_path if status_jsonl_path is not None else STATUS_JSONL

    by_error_type = {k: 0 for k in ERROR_TYPES}
    filtered_by_reason = {k: 0 for k in FILTER_REASONS}
    alerts: list[dict] = []
    ok_count = 0
    error_count = 0

    try:
        if symbols is not None:
            syms = list(symbols)
        elif bars_by_symbol is not None:
            syms = list(bars_by_symbol.keys())
        else:
            syms = fetch_nse_symbols(max_n=max_n)
    except Exception as exc:
        et = classify_error(exc)
        by_error_type[et] = by_error_type.get(et, 0) + 1
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
            "risk_sub_limit_rs": RISK_SUB_LIMIT_RS,
            "max_positions_sub_limit": MAX_POSITIONS_SUB_LIMIT,
            "universe_error": f"{type(exc).__name__}: {exc}",
        }
        _write_status_artifacts(out_status, out_jsonl, status)
        print(
            f"[NewHighDetector] SUMMARY: verdict=FAILED | "
            f"symbols_total=0 ok=0 error=1 (100.00%) | "
            f"by_error_type={by_error_type} | "
            f"filtered_by_reason={filtered_by_reason} | "
            f"candidates=0 | status={os.path.basename(out_status)}"
        )
        return status

    if max_n:
        syms = syms[:max_n]

    adapter = None
    if bars_by_symbol is None:
        if client is None:
            try:
                client = FyersClient.from_files()
            except Exception as exc:
                et = classify_error(exc)
                by_error_type[et] = len(syms) if syms else 1
                finished_at = now_ist().isoformat()
                total = len(syms)
                status = {
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "symbols_total": total,
                    "ok": 0,
                    "symbols_ok": 0,
                    "error": total if total else 1,
                    "error_rate": 1.0,
                    "by_error_type": by_error_type,
                    "filtered_by_reason": filtered_by_reason,
                    "candidates": 0,
                    "verdict": "FAILED",
                    "risk_sub_limit_rs": RISK_SUB_LIMIT_RS,
                    "max_positions_sub_limit": MAX_POSITIONS_SUB_LIMIT,
                    "client_error": f"{type(exc).__name__}: {exc}",
                }
                _write_status_artifacts(out_status, out_jsonl, status)
                print(
                    f"[NewHighDetector] SUMMARY: verdict=FAILED | "
                    f"symbols_total={total} ok=0 error={status['error']} (100.00%) | "
                    f"by_error_type={by_error_type} | "
                    f"filtered_by_reason={filtered_by_reason} | "
                    f"candidates=0 | status={os.path.basename(out_status)}"
                )
                return status
        adapter = ReadOnlyFyersDataAdapter(client)

    for sym in syms:
        if sleep_s and sleep_s > 0:
            time.sleep(sleep_s)
        try:
            if bars_by_symbol is not None:
                if sym not in bars_by_symbol:
                    res = {
                        "symbol": sym,
                        "outcome": "error",
                        "candidate": None,
                        "error_type": "empty",
                        "filter_reason": None,
                        "error_detail": "symbol missing from bars_by_symbol",
                    }
                else:
                    entry = bars_by_symbol[sym]
                    if isinstance(entry, BaseException):
                        raise entry
                    if callable(entry):
                        entry = entry(sym)
                    res = evaluate_bars(sym, entry)
            else:
                bars = adapter.fetch_settled_daily_bars(sym)
                res = evaluate_bars(sym, bars)
        except Exception as exc:
            et = classify_error(exc)
            res = {
                "symbol": sym,
                "outcome": "error",
                "candidate": None,
                "error_type": et,
                "filter_reason": None,
                "error_detail": f"{type(exc).__name__}: {exc}",
            }

        outcome = res["outcome"]
        if outcome == "error":
            error_count += 1
            et = res["error_type"] or "http"
            by_error_type[et] = by_error_type.get(et, 0) + 1
        elif outcome == "filtered":
            ok_count += 1
            fr = res["filter_reason"] or "not_new_high"
            filtered_by_reason[fr] = filtered_by_reason.get(fr, 0) + 1
        elif outcome == "candidate":
            ok_count += 1
            alerts.append(res["candidate"])

    symbols_total = len(syms)
    error_rate = round(error_count / symbols_total, 6) if symbols_total > 0 else 1.0
    verdict = compute_verdict(
        symbols_total=symbols_total,
        ok=ok_count,
        error=error_count,
        min_symbols_ok=min_symbols_ok,
    )
    finished_at = now_ist().isoformat()

    with open(out_alerts, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ALERT_FIELDS)
        writer.writeheader()
        writer.writerows(alerts)

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
        "candidates": len(alerts),
        "verdict": verdict,
        "risk_sub_limit_rs": RISK_SUB_LIMIT_RS,
        "max_positions_sub_limit": MAX_POSITIONS_SUB_LIMIT,
    }
    _write_status_artifacts(out_status, out_jsonl, status)

    print(
        f"[NewHighDetector] Alert-only scan (risk sub-limit Rs {RISK_SUB_LIMIT_RS:,.0f}, "
        f"max {MAX_POSITIONS_SUB_LIMIT} position — human review only, NO orders placed)"
    )
    for a in alerts[:15]:
        print(
            f"   ALERT {a['symbol']:<12} {a['date']} close={a['close']:>9.2f} "
            f"[{a['high_type']}] vol={a['volume']} ({a['vol_ratio']:.2f}x 50d avg {a['avg_vol_50d']:.0f}) "
            f"range_pos={a['range_pos']:.2f}"
        )
    pct_err = (error_count / symbols_total * 100.0) if symbols_total > 0 else 100.0
    print(
        f"[NewHighDetector] SUMMARY: verdict={verdict} | "
        f"symbols_total={symbols_total} ok={ok_count} error={error_count} ({pct_err:.2f}%) | "
        f"by_error_type={by_error_type} | "
        f"filtered_by_reason={filtered_by_reason} | "
        f"candidates={len(alerts)} | alerts={os.path.basename(out_alerts)} | "
        f"status={os.path.basename(out_status)}"
    )
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Alert-only 52-week / all-time high + volume detector (Fyers market data only; "
            "risk sub-limit Rs 15,000, max 1 position — never places orders)."
        )
    )
    parser.add_argument("--max-symbols", type=int, default=None, help="Cap number of NSE symbols scanned.")
    parser.add_argument("--alerts-out", default=ALERTS_CSV, help="Output CSV path for alerts.")
    parser.add_argument("--status-out", default=STATUS_JSON, help="Output JSON path for status artifact.")
    parser.add_argument("--min-symbols-ok", type=int, default=MIN_SYMBOLS_OK, help="Min symbols_ok for OK/DEGRADED verdict.")
    args = parser.parse_args(argv)

    status = run_detector(
        alerts_path=args.alerts_out,
        status_path=args.status_out,
        max_n=args.max_symbols,
        min_symbols_ok=args.min_symbols_ok,
    )
    return 0 if status["verdict"] in ("OK", "DEGRADED") else 1


if __name__ == "__main__":
    sys.exit(main())
