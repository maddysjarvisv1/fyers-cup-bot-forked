"""ATM option symbol resolution for NIFTY + BANKNIFTY, Fyers-index convention.

Pure helpers (atm_strike, weekly/monthly symbol builders) are unit-testable with
no network. The live resolver (`resolve_atm_symbols`) pulls spot via client.ltps,
computes the ATM strike, builds candidate weekly + monthly symbols for the
near-term expiries, and VERIFIES each against client.quotes() (ltp > 0) before
returning — so the tracker only ever works on symbols the broker actually quotes.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

# ---- Fyers index spot / option underlyings (verified against live quotes) ----
SPOT_SYMBOL = {
    "NIFTY": "NSE:NIFTY50-INDEX",        # spot NIFTY -> ATM strike 22400
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",  # spot BANKNIFTY -> ATM strike 54600
}
OPTION_UNDERLYING = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY"}
STRIKE_TICK = {"NIFTY": 100, "BANKNIFTY": 100}
# NIFTY has weekly (Tue) options; BANKNIFTY is monthly only (verified: no weekly
# Mdd symbol resolves for BANKNIFTY across Tue/Wed/Thu next 14 days).
HAS_WEEKLY = {"NIFTY": True, "BANKNIFTY": False}
WEEKLY_WEEKDAY = 1  # Tuesday

_MONTH_3 = ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


# ============================== pure helpers ==============================
def atm_strike(spot: float, round_to: int = 100) -> int:
    """ATM strike = spot rounded to nearest `round_to` (100 for NIFTY & BANKNIFTY)."""
    return int(round(spot / round_to) * round_to)


def month_code_3(dt: datetime) -> str:
    return _MONTH_3[dt.month]


def month_letter(dt: datetime) -> str:
    """First letter of the month (uppercase), used in Fyers weekly Mdd format."""
    return dt.strftime("%b")[0].upper()


def weekly_symbol(underlying: str, dt: datetime, strike: int, side: str) -> str:
    """Fyers weekly index-option symbol: NSE:NIFTY<YY><M><DD><strike><CE/PE>.
    e.g. NSE:NIFTY26O0622400CE (Oct 06 2026, strike 22400 CE)."""
    return f"NSE:{underlying}{dt.strftime('%y')}{month_letter(dt)}{dt.strftime('%d')}{strike}{side}"


def monthly_symbol(underlying: str, dt: datetime, strike: int, side: str) -> str:
    """Fyers monthly index-option symbol: NSE:NIFTY<YY><MMM><strike><CE/PE>.
    e.g. NSE:NIFTY26OCT22400CE."""
    return f"NSE:{underlying}{dt.strftime('%y')}{month_code_3(dt)}{strike}{side}"


def next_week_days(from_dt: datetime, weekday: int, count: int = 8) -> list:
    """Next `count` days on `weekday` strictly after `from_dt`."""
    out = []
    d = from_dt
    while len(out) < count:
        d = d + timedelta(days=1)
        if d.weekday() == weekday:
            out.append(d)
    return out


def candidate_symbols(underlying: str, spot: float, today: datetime,
                      side=None) -> list:
    """Candidate option symbols (weekly + monthly) for an underlying at its ATM
    strike, across the near-term expiries. `side` filters to CE/PE/None(both)."""
    strikes = [atm_strike(spot, STRIKE_TICK[underlying])]
    sides = [side] if side else ["CE", "PE"]
    cands = []
    tuesdays = next_week_days(today, WEEKLY_WEEKDAY, 6)
    monthly = [today + timedelta(days=1)]

    for st in strikes:
        for sd in sides:
            if HAS_WEEKLY.get(underlying):
                for ed in tuesdays:
                    cands.append(weekly_symbol(OPTION_UNDERLYING[underlying], ed, st, sd))
            for ed in monthly:
                cands.append(monthly_symbol(OPTION_UNDERLYING[underlying], ed, st, sd))
    # dedupe preserve order
    seen, uniq = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); uniq.append(c)
    return uniq


# ============================== live resolution ==============================
def resolve_spot(client) -> dict:
    """Spot levels via client.ltps for both indices: {underlying: spot}."""
    syms = {u: SPOT_SYMBOL[u] for u in ("NIFTY", "BANKNIFTY")}
    ltps = client.ltps(list(syms.values()))
    # ltps() keys are plain()d — map back via the plain segment
    out = {}
    for u, full in syms.items():
        seg = full.split(":")[-1]            # e.g. NIFTY50-INDEX
        plain = seg.rsplit("-", 1)[0] if "-" in seg else seg
        v = ltps.get(full) or ltps.get(plain) or ltps.get(seg)
        if v:
            out[u] = float(v)
    return out


def verify_symbols(client, symbols: list, min_ltp: float = 0.0) -> dict:
    """Query each symbol via client.quotes, return {symbol: ltp} for those with
    ltp > min_ltp (i.e. the broker actually quotes them)."""
    ok = {}
    resp = client.quotes(symbols)
    for d in (resp.get("d") or []):
        n = d.get("n")
        v = d.get("v") or {}
        ltp = v.get("lp") if v.get("lp") is not None else v.get("ltp")
        if n and ltp is not None and ltp > min_ltp:
            ok[n] = float(ltp)
    return ok


def resolve_atm_symbols(client, today: datetime | None = None, override=None) -> dict:
    """Return verified ATM CE/PE symbols for NIFTY + BANKNIFTY (or `override`
    filter of underlying names). Each returned symbol has been confirmed with
    client.quotes() ltp>0.

    Returns {underlying: {"spot":.., "strike":.., "ce": sym or None,
                          "pe": sym or None, "weeklies": {...}, "monthlies": {...}}}.
    """
    today = today or datetime.now()
    spot = resolve_spot(client)
    underlyings = list(spot.keys())
    if override:
        underlyings = [u for u in underlyings
                       if u in {o.upper() for o in override}]

    out = {}
    for u in underlyings:
        strike = atm_strike(spot[u], STRIKE_TICK[u])
        cands = candidate_symbols(u, spot[u], today)
        live = verify_symbols(client, cands)
        # classify weekly vs monthly by the option code after the underlying:
        #   weekly : <YY><M-letter><DD><strike><CE/PE>  -> 26O0622400CE
        #   monthly: <YY><MMM><strike><CE/PE>           -> 26OCT22400CE
        ce = pe = None
        weeklies = {}; monthlies = {}
        for s, v in live.items():
            core = s.split(":")[-1]
            if core.endswith("CE"): kind = "ce"
            elif core.endswith("PE"): kind = "pe"
            else: continue
            if HAS_WEEKLY.get(u) and _is_weekly(core, OPTION_UNDERLYING[u]):
                weeklies[s] = v
                if ce is None and kind == "ce": ce = s
                if pe is None and kind == "pe": pe = s
            else:
                monthlies[s] = v
                if ce is None and kind == "ce": ce = s
                if pe is None and kind == "pe": pe = s
        out[u] = {"spot": spot[u], "strike": strike, "ce": ce, "pe": pe,
                  "weeklies": weeklies, "monthlies": monthlies}
    return out


def _is_weekly(core: str, underlying: str) -> bool:
    """True if `core` is a weekly index-option symbol (Fyers weekday code).

    After stripping the underlying name and the 2-digit year, a weekly has a
    single month-letter followed by a 2-digit day before the strike
    (e.g. NIFTY26O0622400CE -> 'O0' after '26'), while a monthly has a 3-letter
    month (NIFTY26OCT22400CE -> 'OC')."""
    rest = core[len(underlying):]          # e.g. "26O0622400CE" / "26OCT22400CE"
    if len(rest) < 5:
        return False
    ymd = rest[2:4]                        # after the YY: "O0" (weekly) / "OC" (monthly)
    return ymd[0].isalpha() and ymd[1].isdigit()