"""
SONACOMS momentum entry through this repo's FyersClient only.

  python -m scripts.sonacoms_order plan      # compute + run every pre-send check, send nothing
  python -m scripts.sonacoms_order place     # checks -> stop-limit BUY (type 4, CNC) -> order-book read-back
  python -m scripts.sonacoms_order protect   # after the fill: SELL stop at the planned stop (re-run EVERY session)

Run from the repo root (token.json, .env, watchlist.csv, asm_symbols.txt live there).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time

import run
from broker import LiveBroker, align_tick
from fyers_client import (IST, FyersClient, FyersError, _ok, is_t2t_symbol, nse, plain,
                          stop_limit_buy_payload)
from momentum_screener import (
    ATR_PERIOD,
    ENTRY_BUFFER,
    LOSS_BAND_MAX,
    LOSS_BAND_MIN,
    SL_PCT,
    TICK,
    Throttle,
    all_time_prior_high,
    atr,
    parse_candles,
    previous_ath_from_bars,
    size_qty_in_band,
)
from scripts.exchange_constraints import check_tick_size
from watchlist_sync import resolve_capital_budget

SYMBOL = "SONACOMS"
PRODUCT = "CNC"
VALIDITY = "DAY"
ORDER_TAG = "momo"
WINDOW_OPEN, WINDOW_CLOSE = dt.time(9, 30), dt.time(15, 0)
ASM_FILE = "asm_symbols.txt"
STATE_FILE = "sonacoms_order.json"
_int = LiveBroker._int


class PreflightError(RuntimeError):
    pass


class Checks:
    """Collect every failed check, then raise once. Never `assert`: python -O strips it."""

    def __init__(self):
        self.passed, self.failed = [], []

    def check(self, ok, msg):
        (self.passed if ok else self.failed).append(msg)
        return bool(ok)

    def raise_if_failed(self, what):
        for m in self.passed:
            print(f"  ok    {m}")
        for m in self.failed:
            print(f"  FAIL  {m}")
        if self.failed:
            raise PreflightError(f"{what}: {len(self.failed)} check(s) failed: " + "; ".join(self.failed))


def now_ist():
    return dt.datetime.now(IST)


def token_is_today(path="token.json"):
    """Same freshness rule as run._ensure_fresh_token (run.py:72-77)."""
    try:
        return json.load(open(path)).get("date") == now_ist().strftime("%Y-%m-%d")
    except Exception:
        return False


def available_funds(resp):
    """Same parse as run._funds_preflight (run.py:113-116)."""
    _ok(resp, "funds")
    for row in resp.get("fund_limit", []):
        if "available" in str(row.get("title", "")).lower():
            return float(row.get("equityAmount", row.get("value")) or 0)
    raise FyersError(f"funds: no 'Available Balance' row in {resp}")


def ltp_quote(client, sym):
    resp = _ok(client.quotes([sym]), f"quotes {sym}")
    for d in resp.get("d") or []:
        if d.get("n") == nse(sym) and d.get("s", "ok") == "ok":
            lp = (d.get("v") or {}).get("lp")
            if lp is not None and float(lp) > 0:
                return float(lp)
    raise FyersError(f"quotes {sym}: no usable lp in {resp}")


def circuit_band(client, sym):
    """Extract (lower_ckt, upper_ckt) from Fyers depth response.
    NOTE: 'lower_ckt' and 'upper_ckt' come from Fyers API docs and are unverified
    against captured repo fixtures; fail closed with FyersError if missing."""
    resp = _ok(client.depth(sym), f"depth {sym}")
    row = (resp.get("d") or {}).get(nse(sym)) or {}
    lo, hi = row.get("lower_ckt"), row.get("upper_ckt")
    if not lo or not hi:
        raise FyersError(f"depth {sym}: no lower_ckt/upper_ckt in {resp}")
    return float(lo), float(hi)


def prior_session(client, sym, today):
    """Completed sessions only (today's partial candle dropped) -> (session_date, prev_ath, atr14).
    Uses the PREVIOUS all-time high strictly before the current breakout leg, never the latest high."""
    resp = client.history(sym, (today - dt.timedelta(days=365)).isoformat(), today.isoformat())
    bars = [b for b in parse_candles(resp, f"history {sym}") if b.day < today]
    if len(bars) < ATR_PERIOD + 1:
        raise FyersError(f"history {sym}: {len(bars)} completed sessions < {ATR_PERIOD + 1}")
    older = all_time_prior_high(client, sym, bars[0].day, Throttle(0))
    prev_ath = previous_ath_from_bars(bars, older_high=older)
    return bars[-1].day, prev_ath, atr(bars, ATR_PERIOD)


def _validate_holdings_rows(hold):
    """Fail-closed validation for holdings response rows.
    NOTE: 'quantity' and 'costPrice' come from Fyers API docs and are unverified
    against captured repo fixtures; fail closed if any holding row lacks them."""
    if not isinstance(hold, dict) or "holdings" not in hold or hold["holdings"] is None:
        raise FyersError(f"holdings: missing 'holdings' list in {hold}")
    rows = hold["holdings"]
    for idx, h in enumerate(rows):
        if not isinstance(h, dict) or "quantity" not in h or "costPrice" not in h:
            raise FyersError(
                f"holdings[{idx}]: missing required 'quantity'/'costPrice' fields in {h!r}"
            )
    return rows


def held_qty(client, sym):
    """CNC qty actually held = settled holdings + today's net position."""
    pos = _ok(client.positions(), "positions")
    hold = _ok(client.holdings(), "holdings")
    hold_rows = _validate_holdings_rows(hold)
    net = sum(int(p.get("netQty") or 0) for p in pos.get("netPositions") or []
              if plain(str(p.get("symbol", ""))) == sym)
    settled = sum(int(h["quantity"] or 0) for h in hold_rows
                  if plain(str(h.get("symbol", ""))) == sym)
    return net + settled, pos, hold


def bot_scope():
    """Symbols charged to CAPITAL_BUDGET — same scope as CupBot._deployed_notional
    (bot.py:436-445): the bot's watchlists plus this order."""
    scope = {w["symbol"] for w in run.load_watchlist("watchlist.csv")}
    mom = run.env("MOMENTUM_WATCHLIST", "")
    if mom and os.path.exists(mom):
        scope |= {w["symbol"] for w in run.load_watchlist(mom)}
    return scope | {SYMBOL}


def deployed_notional(pos, hold, ob_rows, scope):
    hold_rows = _validate_holdings_rows(hold)
    d = sum(float(p.get("avgPrice") or p.get("netAvg") or 0) * int(p.get("netQty") or 0)
            for p in pos.get("netPositions") or []
            if plain(str(p.get("symbol", ""))) in scope and int(p.get("netQty") or 0) > 0)
    d += sum(float(h["costPrice"] or 0) * int(h["quantity"] or 0)
             for h in hold_rows if plain(str(h.get("symbol", ""))) in scope)
    d += sum(int(o.get("qty") or 0) * float(o.get("limitPrice") or o.get("stopPrice") or 0)
             for o in ob_rows
             if _int(o.get("status")) in LiveBroker.OPEN_STATUS and _int(o.get("side")) == 1
             and plain(str(o.get("symbol", ""))) in scope)
    return d


def load_asm(today):
    """No Fyers endpoint exposes NSE ASM membership. Fallback: the operator saves the
    day's NSE ASM list (one symbol per line) to asm_symbols.txt. Missing or not
    refreshed today -> None -> the pre-send check FAILS (fail closed)."""
    if not os.path.exists(ASM_FILE):
        return None
    if dt.datetime.fromtimestamp(os.path.getmtime(ASM_FILE), IST).date() != today:
        return None
    return {ln.strip().upper() for ln in open(ASM_FILE) if ln.strip() and not ln.startswith("#")}


def compute_plan(prior_high, atr14, capital_budget, budget_left, funds_available):
    """Owner-confirmed SONACOMS plan:
      H              = align_tick(prior_high)            (previous ATH, e.g. 839.0)
      trigger/entry  = align_tick(H * 1.04)              (872.55)
      limit          = trigger                           (872.55)
      stop           = align_tick(trigger * (1 - 0.06))  (820.20, 6% below entry)
      risk_per_share = round(trigger - stop, 2)          (52.35)
      qty            = nearest valid integer in [Rs 750, Rs 900] loss band within budget (16 -> risk 837.60)
    """
    H = align_tick(prior_high)
    trigger = align_tick(H * (1.0 + ENTRY_BUFFER))
    limit = trigger
    stop = align_tick(trigger * (1.0 - SL_PCT))
    risk_per_share = round(limit - stop, 2)
    effective_budget = max(0.0, min(float(capital_budget), float(budget_left), float(funds_available)))
    qty_band, band_reason = size_qty_in_band(
        limit, stop, capital_budget=effective_budget, loss_min=LOSS_BAND_MIN, loss_max=LOSS_BAND_MAX
    )
    qty_risk = math.floor(LOSS_BAND_MAX / risk_per_share) if risk_per_share > 0 else 0
    qty_funds = math.floor(effective_budget / limit) if limit > 0 else 0
    qty = int(qty_band) if qty_band is not None else 0
    risk_cash = round(qty * risk_per_share, 2)
    return {
        "H": H, "trigger": trigger, "limit": limit, "stop": stop, "atr14": atr14,
        "risk_per_share": risk_per_share, "risk_cash": risk_cash,
        "qty_risk": qty_risk, "qty_funds": qty_funds, "qty": qty,
        "band_reason": band_reason,
    }


def build_and_check(client, now):
    c = Checks()
    today, t = now.date(), now.time()
    c.check(now.weekday() < 5 and WINDOW_OPEN <= t <= WINDOW_CLOSE,
            f"time {t:%H:%M:%S} IST inside 09:30-15:00 on a weekday")
    c.check(token_is_today(), "token.json issued today")
    prof = client.profile()
    c.check(isinstance(prof, dict) and prof.get("s") == "ok" and prof.get("code") == 200,
            f"profile() s=ok code=200 (got s={prof.get('s') if isinstance(prof, dict) else prof!r}, "
            f"code={prof.get('code') if isinstance(prof, dict) else None})")
    c.check(PRODUCT == "CNC", f"productType {PRODUCT} == CNC")
    c.check(not is_t2t_symbol(SYMBOL), f"{nse(SYMBOL)} is not T2T/BE series")
    asm = load_asm(today)
    c.check(asm is not None, f"{ASM_FILE} present and refreshed today")
    c.check(asm is not None and SYMBOL not in asm, f"{SYMBOL} not on the ASM list")

    ltp = ltp_quote(client, SYMBOL)                      # quote from Fyers, raises if unusable
    session, H, a14 = prior_session(client, SYMBOL, today)
    c.check((today - session).days <= 5, f"prior session {session} is recent")
    lo, hi = circuit_band(client, SYMBOL)
    funds = available_funds(client.funds())
    held, pos, hold = held_qty(client, SYMBOL)
    ob = _ok(client.orderbook(), "orderbook").get("orderBook") or []
    open_same = [o for o in ob if plain(str(o.get("symbol", ""))) == SYMBOL
                 and _int(o.get("status")) in LiveBroker.OPEN_STATUS]
    budget = resolve_capital_budget()
    deployed = deployed_notional(pos, hold, ob, bot_scope())
    p = compute_plan(H, a14, budget, budget - deployed, funds)
    notional = p["qty"] * p["limit"]

    c.check(p["trigger"] > ltp, f"stopPrice {p['trigger']} > ltp {ltp} (not chasing)")
    c.check(p["limit"] >= p["trigger"], f"limitPrice {p['limit']} >= stopPrice {p['trigger']}")
    c.check(0 < p["stop"] < p["trigger"], f"protective stop {p['stop']} in (0, {p['trigger']})")
    for name in ("trigger", "limit", "stop"):
        ok, why = check_tick_size(p[name])
        c.check(ok, f"{name}: {why}")
    c.check(lo <= p["stop"] and p["limit"] <= hi,
            f"[stop {p['stop']}, limit {p['limit']}] inside circuit band [{lo}, {hi}]")
    c.check(p["band_reason"] is None and LOSS_BAND_MIN <= p["risk_cash"] <= LOSS_BAND_MAX,
            f"risk Rs {p['risk_cash']:.2f} in loss band [{LOSS_BAND_MIN:.2f}, {LOSS_BAND_MAX:.2f}]"
            + (f" ({p['band_reason']})" if p["band_reason"] else ""))
    c.check(p["qty"] >= 1, f"qty {p['qty']} >= 1")
    c.check(p["qty"] <= p["qty_risk"] and p["qty"] <= p["qty_funds"],
            f"qty {p['qty']} <= computed size min(risk {p['qty_risk']}, funds {p['qty_funds']})")
    c.check(notional <= funds, f"notional Rs {notional:,.2f} <= available funds Rs {funds:,.2f}")
    c.check(deployed + notional <= budget,
            f"deployed Rs {deployed:,.2f} + notional Rs {notional:,.2f} <= CAPITAL_BUDGET Rs {budget:,.2f}")
    c.check(held == 0, f"no existing {SYMBOL} position/holding (held {held})")
    c.check(not open_same, f"no open {SYMBOL} order (found {[o.get('id') for o in open_same]})")
    ctx = {"ltp": ltp, "lo": lo, "hi": hi, "ob": ob, "budget": budget, "session": session.isoformat(),
           "funds": funds, "deployed": deployed}
    return c, p, ctx


def read_back(client, oid, p, tries=10, delay=1.0):
    """Order-book read-back: the order must be PENDING (6) or TRADED (2) with our exact fields."""
    last = None
    for _ in range(tries):
        rows = _ok(client.orderbook(), "orderbook").get("orderBook") or []
        row = next((o for o in rows if str(o.get("id")) == str(oid)), None)
        if row is not None:
            last = _int(row.get("status"))
            if last in (1, 5):
                raise FyersError(f"order {oid} {'CANCELLED' if last == 1 else 'REJECTED'}: "
                                 f"{row.get('message')!r}")
            if last in (6, LiveBroker.FILLED_STATUS):
                c = Checks()
                c.check(_int(row.get("type")) == 4, f"type {row.get('type')} == 4")
                c.check(_int(row.get("side")) == 1, f"side {row.get('side')} == 1")
                c.check(_int(row.get("qty")) == p["qty"], f"qty {row.get('qty')} == {p['qty']}")
                c.check(abs(float(row.get("stopPrice") or 0) - p["trigger"]) < 1e-6,
                        f"stopPrice {row.get('stopPrice')} == {p['trigger']}")
                c.check(abs(float(row.get("limitPrice") or 0) - p["limit"]) < 1e-6,
                        f"limitPrice {row.get('limitPrice')} == {p['limit']}")
                c.check(str(row.get("productType")) == PRODUCT, f"productType {row.get('productType')} == CNC")
                c.raise_if_failed(f"read-back {oid}")
                return row
        time.sleep(delay)
    raise FyersError(f"order {oid} not live after {tries} order-book reads (last status {last})")


def place(client, send=True):
    now = now_ist()
    c, p, ctx = build_and_check(client, now)
    payload = stop_limit_buy_payload(SYMBOL, p["qty"], p["trigger"], p["limit"], product=PRODUCT,
                                     validity=VALIDITY, offline_order=False, order_tag=ORDER_TAG)
    print(f"plan: prior session {ctx['session']} H={p['H']} ATR14={p['atr14']:.2f} ltp={ctx['ltp']} "
          f"band=[{ctx['lo']}, {ctx['hi']}] stop={p['stop']} risk/sh={p['risk_per_share']:.2f}")
    print("entry payload:", json.dumps(payload, indent=2))
    c.raise_if_failed("pre-send")
    if not send:
        return None
    oid = client.place_buy_stop_limit(
        SYMBOL, p["qty"], p["trigger"], p["limit"], product=PRODUCT, order_tag=ORDER_TAG,
        validity=VALIDITY, now=now, ltp=ctx["ltp"], lower_circuit=ctx["lo"], upper_circuit=ctx["hi"],
        open_orders=ctx["ob"], max_order_value=ctx["budget"],
    )
    row = read_back(client, oid, p)
    json.dump({"date": now.date().isoformat(), "order_id": oid, **p, "prior_session": ctx["session"]},
              open(STATE_FILE, "w"), indent=2)
    print(f"LIVE: {oid} status {row.get('status')} — run `protect` once it fills")
    return oid


def protect(client):
    """Place the protective SELL stop for the qty actually held, at the stop planned
    when the entry was sized. VALIDITY=DAY: the exchange cancels it at 15:30, so this
    MUST be re-run every session after 09:15 IST (cron 16 9 * * 1-5) until the
    position is closed — the same daily re-place CupBot does (bot.py:141-150,
    :573-603). Between 15:30 and the next re-place the position has no resting stop."""
    state = json.load(open(STATE_FILE))
    stop = float(state["stop"])
    held, _, _ = held_qty(client, SYMBOL)
    if held <= 0:
        print(f"no {SYMBOL} held — entry not filled yet (or already exited); nothing to protect")
        return None
    ob = _ok(client.orderbook(), "orderbook").get("orderBook") or []
    resting = [o for o in ob if plain(str(o.get("symbol", ""))) == SYMBOL and _int(o.get("side")) == -1
               and _int(o.get("status")) in LiveBroker.OPEN_STATUS]
    if resting:
        r = resting[0]
        if _int(r.get("qty")) != held:      # partial fill grew: resize, never stack a second stop
            client.modify_order(r["id"], stop_price=stop, qty=held, order_type=3, symbol=SYMBOL,
                                side="SELL", product=PRODUCT, order_tag=ORDER_TAG)
            print(f"resized SELL stop {r['id']} -> qty {held} @ {stop}")
        else:
            print(f"SELL stop {r['id']} already resting x{held} @ {r.get('stopPrice')}")
        return r["id"]
    ltp = ltp_quote(client, SYMBOL)
    if ltp <= stop:
        # a SELL stop at/above ltp is rejected — exit at market (bot.py:579-584, :621-623 rule)
        oid = client.place_market_sell(SYMBOL, held, product=PRODUCT, order_tag=ORDER_TAG, ltp=ltp)
        print(f"stop {stop} already breached (ltp {ltp}) — market SELL {held}: {oid}")
        return oid
    lo, hi = circuit_band(client, SYMBOL)
    oid = client.place_sell_stop(
        SYMBOL, held, stop, product=PRODUCT, order_tag=ORDER_TAG,
        validity=VALIDITY,                       # DAY: dies at 15:30 — re-run `protect` every session
        ltp=ltp, lower_circuit=lo, upper_circuit=hi, open_orders=ob, position_qty=held,
        overnight_position=True, has_next_day_rearm=True,
    )
    for _ in range(10):
        rows = _ok(client.orderbook(), "orderbook").get("orderBook") or []
        row = next((o for o in rows if str(o.get("id")) == str(oid)), None)
        if row is not None and _int(row.get("status")) == 6:
            print(f"LIVE: SELL stop {oid} x{held} @ {stop}")
            return oid
        if row is not None and _int(row.get("status")) in (1, 5):
            raise FyersError(f"SELL stop {oid} rejected/cancelled: {row.get('message')!r}")
        time.sleep(1.0)
    raise FyersError(f"SELL stop {oid} not confirmed pending in the order book — position may be UNPROTECTED")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "plan"
    if cmd not in ("plan", "place", "protect"):
        print(__doc__)
        return 2
    try:
        client = FyersClient.from_files()
        if cmd == "protect":
            protect(client)
        else:
            place(client, send=(cmd == "place"))
    except (PreflightError, FyersError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
