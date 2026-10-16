#!/usr/bin/env python3
"""
scripts/exchange_constraints.py — Exhaustive Fyers v3 / NSE Exchange-Constraint Sweep.

Why this module exists:
  A single unaligned stop price (code=-50 'StopPrice not a multiple of tick size 0.0500')
  blocked a LIVE entry for an entire session because a percentage calculation was handed
  straight to the broker without pre-flight exchange validation. That was one instance
  of a broader class of exchange/broker rejections.

  This module enumerates the class and provides pure, deterministic, offline-testable
  validators ``check_<rule>(...) -> (ok: bool, reason: str)`` for every Fyers/NSE
  constraint that can reject or mis-handle an order, plus a single entry point:

      validate_order(symbol, side, qty, price, product, validity, **kwargs) -> list[str]

  which runs all checks and returns EVERY violation found (never stopping at the first).

Explicit disclosure of rules that CANNOT be enforced purely offline without live data:
  - Dynamic NSE circuit bands ( Rule 4: PRICE BANDS ):
      NSE revises stock-specific circuit bands (2%, 5%, 10%, 20%) and computes exact
      daily upper/lower circuit limits from the previous session's closing price.
      Without ``ref_price`` (previous close) or explicit ``(lower_circuit, upper_circuit)``
      from the live quote feed, exact daily circuit compliance CANNOT be enforced
      statically. ``check_price_band()`` refuses when reference bounds are omitted, and
      ``validate_order(..., strict_unverifiable=True)`` surfaces this explicitly.
  - Stop trigger vs live market direction ( Rule 12: SL TRIGGER DIRECTION ):
      NSE rejects a BUY SL-M trigger <= current LTP or a SELL SL-M trigger >= current LTP.
      Cannot be enforced without a live ``ltp`` quote.
  - Quarterly NSE F&O stock-specific freeze revisions & T+1 depository EDIS/DDPI state:
      Index F&O freeze quantities are tracked here, while individual stock F&O freeze
      quantities and demat CDSL T+1 PoA/DDPI authorization status require live broker/exchange
      metadata.
"""
from __future__ import annotations

import ast
import math
import os
import re
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from broker import align_tick, _DEFAULT_TICK_SIZE
from fyers_client import SYMBOL_OVERRIDES, T2T_SERIES_SUFFIXES, is_t2t_symbol, nse, plain

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Exchange & Strategy Constants
# ---------------------------------------------------------------------------
DEFAULT_EQUITY_TICK: float = _DEFAULT_TICK_SIZE  # 0.05 INR for regular NSE equity
DEFAULT_EQUITY_LOT_SIZE: int = 1

# Exposed hook for F&O lot sizes where lot_size != 1 (NSE index derivatives)
DEFAULT_FO_LOT_SIZES: dict[str, int] = {
    "NIFTY": 75,
    "BANKNIFTY": 30,
    "FINNIFTY": 65,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    "SENSEX": 20,
    "BANKEX": 30,
}

# NSE per-order freeze quantities (index derivatives + conservative cash equity cap)
DEFAULT_EQUITY_FREEZE_QTY: int = 100_000
DEFAULT_FO_FREEZE_QTY: dict[str, int] = {
    "NIFTY": 1800,
    "BANKNIFTY": 900,
    "FINNIFTY": 1800,
    "MIDCPNIFTY": 2800,
    "NIFTYNXT50": 600,
    "SENSEX": 1000,
    "BANKEX": 900,
}

# Standard NSE price circuit bands (%)
VALID_PRICE_BAND_PCTS: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0)
DEFAULT_EQ_BAND_PCT: float = 20.0
DEFAULT_T2T_BAND_PCT: float = 5.0

# Per-order value cap (defaults to strategy ₹50,000 capital budget unless overridden)
DEFAULT_MAX_ORDER_VALUE: float = 50_000.0
EXCHANGE_RETAIL_ORDER_VALUE_CAP: float = 10_000_000.0  # ₹1 Crore hard sanity ceiling

# Fyers v3 validities and product types
VALID_VALIDITIES: tuple[str, ...] = ("DAY", "IOC", "GTT")
VALID_PRODUCT_TYPES: tuple[str, ...] = ("CNC", "INTRADAY", "MARGIN", "MTF")

PRODUCT_CONSEQUENCES: dict[str, str] = {
    "CNC": (
        "CNC (Cash & Carry delivery): position can be held overnight across sessions; "
        "no intraday broker auto-square-off; requires 100% cash upfront; T+1 settlement."
    ),
    "INTRADAY": (
        "INTRADAY (MIS): broker auto-squares off open positions at ~15:15-15:20 IST "
        "before market close; CANNOT be held overnight for a multi-day swing strategy."
    ),
    "MARGIN": (
        "MARGIN (Carry-forward F&O): valid only for derivative contracts (-FUT / CE / PE), "
        "rejected on NSE cash equity (-EQ / -BE)."
    ),
    "MTF": (
        "MTF (Margin Trading Facility): leveraged delivery subject to broker MTF eligibility "
        "list, daily interest, and pledge confirmation."
    ),
}

# Regular NSE trading session boundaries (IST)
MARKET_OPEN_TIME = dtime(9, 15, 0)
MARKET_CLOSE_TIME = dtime(15, 30, 0)
PRE_OPEN_START_TIME = dtime(9, 0, 0)

# Explicit manifest of rules and their offline enforceability
RULE_ENFORCEABILITY: dict[str, dict[str, Any]] = {
    "tick_size": {
        "enforced_offline": True,
        "note": "Exact Decimal modulo check against instrument tick (default 0.05).",
    },
    "lot_size": {
        "enforced_offline": True,
        "note": "Equity lot=1 enforced; F&O lot sizes resolved via DEFAULT_FO_LOT_SIZES or caller hook.",
    },
    "freeze_quantity": {
        "enforced_offline": True,
        "note": "Enforced against index F&O freeze table and equity cap; split_freeze_quantity() slices large orders.",
    },
    "price_band": {
        "enforced_offline": "requires_ref_price_or_bounds",
        "note": (
            "CANNOT be enforced without ref_price (prev close) or explicit (lower_circuit, upper_circuit) "
            "from live quote feed; NSE circuit limits (2/5/10/20%) are dynamic daily values."
        ),
    },
    "order_value_cap": {
        "enforced_offline": True,
        "note": "Enforced against per-order value cap (default Rs 50,000 strategy budget).",
    },
    "t2t_series": {
        "enforced_offline": True,
        "note": "Hard check excluding -BE/-BZ/-SZ/-T series and SYMBOL_OVERRIDES from same-day SL strategies.",
    },
    "validity": {
        "enforced_offline": True,
        "note": "Enforces DAY/IOC/GTT rules; flags DAY overnight expiry and refuses unprotected overnight state.",
    },
    "product_type": {
        "enforced_offline": True,
        "note": "Enforces CNC vs INTRADAY/MARGIN consequences and blocks INTRADAY for overnight holding.",
    },
    "market_hours": {
        "enforced_offline": True,
        "note": "Enforces 09:15-15:30 IST Mon-Fri window and requires explicit AMO handling outside hours.",
    },
    "duplicate_guard": {
        "enforced_offline": True,
        "note": "Enforces at most one resting stop per (symbol, side) and SELL qty <= held position_qty.",
    },
}


# ---------------------------------------------------------------------------
# Internal Symbol / Derivative Classification Helpers
# ---------------------------------------------------------------------------
_FO_OPTION_RE = re.compile(r"^(?:NSE:|BSE:)?([A-Z0-9&_-]+?)(\d{2}[A-Z0-9]{3}\d+(?:CE|PE))$")
_FO_FUT_RE = re.compile(r"^(?:NSE:|BSE:)?([A-Z0-9&_-]+?)\d{2}[A-Z]{3}FUT$|-FUT$")


def _extract_fo_underlying(symbol: str) -> str | None:
    """If ``symbol`` looks like an F&O option or future, return its underlying name, else None."""
    s = str(symbol or "").upper().strip()
    core = s.split(":")[-1]
    for u in sorted(DEFAULT_FO_LOT_SIZES.keys(), key=len, reverse=True):
        if core.startswith(u) and len(core) > len(u) and core[len(u):len(u) + 2].isdigit():
            if core.endswith(("CE", "PE", "FUT")):
                return u
    m = _FO_OPTION_RE.match(s)
    if m:
        return m.group(1)
    if core.endswith("-FUT") or _FO_FUT_RE.search(core):
        base = core.replace("-FUT", "")
        base = re.sub(r"\d{2}[A-Z]{3}FUT$", "", base)
        return base or "FO"
    return None


def is_fo_symbol(symbol: str) -> bool:
    """Return True if ``symbol`` is an F&O derivative (option or future) rather than cash equity."""
    return _extract_fo_underlying(symbol) is not None


def _normalize_side(side: str | int) -> str | None:
    if isinstance(side, int) and not isinstance(side, bool):
        if side == 1:
            return "BUY"
        if side == -1:
            return "SELL"
        return None
    s = str(side or "").upper().strip()
    if s in ("BUY", "B", "1", "+1"):
        return "BUY"
    if s in ("SELL", "S", "-1"):
        return "SELL"
    return None


# ---------------------------------------------------------------------------
# 1. TICK SIZE
# ---------------------------------------------------------------------------
def check_tick_size(
    price: float | int,
    tick: float = DEFAULT_EQUITY_TICK,
    *,
    allow_zero_market: bool = False,
) -> tuple[bool, str]:
    """Rule 1: Validate that ``price`` is a positive, exact multiple of ``tick`` (default 0.05).

    Returns ``(ok, reason)``.
    """
    if isinstance(price, bool) or not isinstance(price, (int, float, Decimal)):
        return False, f"tick_size: invalid non-numeric price {price!r}"
    price_f = float(price)
    if not math.isfinite(price_f):
        return False, f"tick_size: non-finite price {price!r}"
    if tick is None or isinstance(tick, bool) or float(tick) <= 0 or not math.isfinite(float(tick)):
        return False, f"tick_size: invalid tick size {tick!r} (must be > 0)"
    if price_f == 0.0 and allow_zero_market:
        return True, "tick_size: price 0.0 allowed for market order"
    if price_f <= 0.0:
        return False, f"tick_size: price {price_f} must be > 0"

    try:
        d_price = Decimal(str(price))
        d_tick = Decimal(str(tick))
        remainder = d_price % d_tick
    except (InvalidOperation, ValueError) as exc:
        return False, f"tick_size: decimal conversion failed for price={price!r}, tick={tick!r}: {exc}"

    if remainder != Decimal("0"):
        aligned = align_tick(price_f, float(tick))
        return (
            False,
            f"tick_size: price {price} is not a multiple of tick size {tick:.4f} "
            f"(remainder={remainder}; nearest aligned={aligned:.2f})",
        )
    return True, f"tick_size: {price} is an exact multiple of tick {tick}"


check_tick = check_tick_size


def check_outgoing_price_paths(repo_root: str | None = None) -> tuple[bool, str]:
    """Verify via AST inspection that EVERY outgoing order/stop price path in the repo uses align_tick().

    Inspects:
      - fyers_client.py: place_buy_stop, place_buy_gtt, place_sell_stop, modify_order
      - broker.py: PaperBroker._place_stop, place_buy_stop, place_sell_stop, modify_stop,
                   modify_stop_qty; LiveBroker.place_buy_stop, place_sell_stop, modify_stop,
                   modify_stop_qty
      - bot.py: _place_sell, _place_buy
      - nse_screener.py: screen_symbol
      - watchlist_sync.py: _read_existing_watchlist, _read_screener_candidates, _build_watchlist_csv
    """
    root = repo_root or ROOT
    required_functions: dict[str, list[str]] = {
        "fyers_client.py": ["place_buy_stop", "place_buy_gtt", "place_sell_stop", "modify_order"],
        "broker.py": [
            "PaperBroker._place_stop",
            "PaperBroker.place_buy_stop",
            "PaperBroker.place_sell_stop",
            "PaperBroker.modify_stop",
            "PaperBroker.modify_stop_qty",
            "LiveBroker.place_buy_stop",
            "LiveBroker.place_sell_stop",
            "LiveBroker.modify_stop",
            "LiveBroker.modify_stop_qty",
        ],
        "bot.py": ["CupBot._place_sell", "CupBot._place_buy"],
        "nse_screener.py": ["screen_symbol"],
        "watchlist_sync.py": [
            "_read_existing_watchlist",
            "_read_screener_candidates",
            "_build_watchlist_csv",
        ],
    }

    missing: list[str] = []
    for rel_path, targets in required_functions.items():
        full_path = os.path.join(root, rel_path)
        if not os.path.exists(full_path):
            missing.append(f"{rel_path} (file missing)")
            continue
        with open(full_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel_path)

        found_funcs: dict[str, ast.FunctionDef] = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                found_funcs[node.name] = node
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        found_funcs[f"{node.name}.{item.name}"] = item
                        if item.name not in found_funcs:
                            found_funcs[item.name] = item

        for target in targets:
            fn_node = found_funcs.get(target)
            if fn_node is None:
                missing.append(f"{rel_path}:{target} (function not found)")
                continue
            calls_align = any(
                isinstance(sub, ast.Call)
                and (
                    (isinstance(sub.func, ast.Name) and sub.func.id in ("align_tick", "align_to_tick", "round_to_tick"))
                    or (isinstance(sub.func, ast.Attribute) and sub.func.attr in ("align_tick", "align_to_tick", "round_to_tick"))
                )
                for sub in ast.walk(fn_node)
            )
            if not calls_align:
                missing.append(f"{rel_path}:{target} (does not call align_tick)")

    if missing:
        return False, f"tick_size outgoing path audit failed: {', '.join(missing)}"
    return True, "tick_size: all 19 outgoing price paths across fyers_client, broker, bot, screener, and watchlist_sync call align_tick()"


# ---------------------------------------------------------------------------
# 2. LOT SIZE
# ---------------------------------------------------------------------------
def get_lot_size(symbol: str = "", lot_sizes: dict[str, int] | None = None) -> int:
    """Resolve instrument lot size.

    - NSE cash equity (`-EQ`, `-BE`, or plain equity symbol): 1
    - F&O derivative (`NIFTY`, `BANKNIFTY`, etc. or `-FUT`/`CE`/`PE`): looked up from
      ``lot_sizes`` hook or ``DEFAULT_FO_LOT_SIZES``.
    """
    raw = str(symbol or "").upper().strip()
    p = plain(raw) if raw else ""
    table = dict(DEFAULT_FO_LOT_SIZES)
    if lot_sizes:
        table.update({str(k).upper(): int(v) for k, v in lot_sizes.items()})

    if raw in table:
        return table[raw]
    if p in table and (is_fo_symbol(raw) or (lot_sizes and p in {k.upper() for k in lot_sizes})):
        return table[p]

    underlying = _extract_fo_underlying(raw)
    if underlying:
        if underlying in table:
            return table[underlying]
        raise ValueError(
            f"F&O symbol {symbol!r} (underlying={underlying}) has no configured lot size; "
            f"pass lot_size or lot_sizes[{underlying!r}] explicitly"
        )
    return DEFAULT_EQUITY_LOT_SIZE


def check_lot_size(
    qty: int | float,
    symbol: str = "",
    lot_size: int | None = None,
    lot_sizes: dict[str, int] | None = None,
) -> tuple[bool, str]:
    """Rule 2: Validate that ``qty`` is a positive integer multiple of the instrument lot size."""
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        return False, f"lot_size: invalid non-numeric quantity {qty!r}"
    if not math.isfinite(float(qty)) or int(qty) != float(qty):
        return False, f"lot_size: quantity {qty!r} must be a whole integer"
    qty_i = int(qty)
    if qty_i <= 0:
        return False, f"lot_size: quantity {qty_i} must be >= 1"

    try:
        resolved_lot = int(lot_size) if lot_size is not None else get_lot_size(symbol, lot_sizes=lot_sizes)
    except ValueError as exc:
        return False, f"lot_size: {exc}"

    if resolved_lot <= 0:
        return False, f"lot_size: invalid lot_size {resolved_lot} (must be >= 1)"
    if qty_i % resolved_lot != 0:
        return (
            False,
            f"lot_size: qty {qty_i} for {symbol or 'instrument'} is not a multiple of lot size {resolved_lot} "
            f"(remainder={qty_i % resolved_lot})",
        )
    return True, f"lot_size: qty {qty_i} satisfies lot size {resolved_lot} ({qty_i // resolved_lot} lot(s))"


check_lot = check_lot_size


# ---------------------------------------------------------------------------
# 3. FREEZE QUANTITY
# ---------------------------------------------------------------------------
def get_freeze_quantity(symbol: str = "", freeze_map: dict[str, int] | None = None) -> int:
    """Resolve NSE per-order freeze quantity for ``symbol``."""
    raw = str(symbol or "").upper().strip()
    p = plain(raw) if raw else ""
    table = dict(DEFAULT_FO_FREEZE_QTY)
    if freeze_map:
        table.update({str(k).upper(): int(v) for k, v in freeze_map.items()})

    if raw in table:
        return table[raw]
    if p in table and (is_fo_symbol(raw) or (freeze_map and p in {k.upper() for k in freeze_map})):
        return table[p]

    underlying = _extract_fo_underlying(raw)
    if underlying and underlying in table:
        return table[underlying]
    return DEFAULT_EQUITY_FREEZE_QTY


def split_freeze_quantity(
    qty: int,
    symbol: str = "",
    freeze_qty: int | None = None,
    lot_size: int | None = None,
) -> list[int]:
    """Split an order ``qty`` into exchange-compliant child order quantities ``<= freeze_qty``.

    Each slice is aligned to ``lot_size`` so every child slice is independently valid on NSE.
    """
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise ValueError(f"qty must be a positive integer, got {qty!r}")
    resolved_lot = int(lot_size) if lot_size is not None else get_lot_size(symbol)
    if resolved_lot <= 0:
        raise ValueError(f"lot_size must be >= 1, got {resolved_lot}")
    if qty % resolved_lot != 0:
        raise ValueError(f"qty {qty} is not a multiple of lot_size {resolved_lot}")

    max_freeze = int(freeze_qty) if freeze_qty is not None else get_freeze_quantity(symbol)
    if max_freeze < resolved_lot:
        raise ValueError(f"freeze_qty {max_freeze} is smaller than lot_size {resolved_lot}")

    # Align max slice down to the nearest multiple of lot_size
    max_slice = (max_freeze // resolved_lot) * resolved_lot
    slices: list[int] = []
    remaining = qty
    while remaining > 0:
        chunk = min(remaining, max_slice)
        slices.append(chunk)
        remaining -= chunk
    return slices


def check_freeze_quantity(
    qty: int | float,
    symbol: str = "",
    freeze_qty: int | None = None,
    freeze_map: dict[str, int] | None = None,
) -> tuple[bool, str]:
    """Rule 3: Detect orders exceeding the NSE per-order freeze quantity and refuse with split guidance."""
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(float(qty)):
        return False, f"freeze_quantity: invalid quantity {qty!r}"
    qty_i = int(qty)
    if qty_i <= 0 or qty_i != float(qty):
        return False, f"freeze_quantity: quantity {qty!r} must be a positive integer"

    max_freeze = int(freeze_qty) if freeze_qty is not None else get_freeze_quantity(symbol, freeze_map=freeze_map)
    if max_freeze <= 0:
        return False, f"freeze_quantity: invalid freeze limit {max_freeze}"

    if qty_i > max_freeze:
        try:
            slices = split_freeze_quantity(qty_i, symbol=symbol, freeze_qty=max_freeze)
            split_hint = f"must split into {len(slices)} child orders {slices}"
        except ValueError:
            num_slices = math.ceil(qty_i / max_freeze)
            split_hint = f"must split into {num_slices} child orders of <= {max_freeze}"
        return (
            False,
            f"freeze_quantity: qty {qty_i} for {symbol or 'instrument'} exceeds NSE freeze limit "
            f"{max_freeze} ({split_hint})",
        )
    return True, f"freeze_quantity: qty {qty_i} is within freeze limit {max_freeze}"


check_freeze_qty = check_freeze_quantity


# ---------------------------------------------------------------------------
# 4. PRICE BANDS (CIRCUIT LIMITS)
# ---------------------------------------------------------------------------
def compute_price_band(
    ref_price: float,
    band_pct: float = DEFAULT_EQ_BAND_PCT,
    tick: float = DEFAULT_EQUITY_TICK,
) -> tuple[float, float]:
    """Return ``(lower_circuit, upper_circuit)`` tick-aligned around ``ref_price``."""
    raw_low = float(ref_price) * (1.0 - float(band_pct) / 100.0)
    raw_high = float(ref_price) * (1.0 + float(band_pct) / 100.0)
    return align_tick(raw_low, tick), align_tick(raw_high, tick)


def check_price_band(
    price: float,
    ref_price: float | None = None,
    band_pct: float | None = None,
    *,
    lower_circuit: float | None = None,
    upper_circuit: float | None = None,
    symbol: str = "",
    tick: float = DEFAULT_EQUITY_TICK,
) -> tuple[bool, str]:
    """Rule 4: Validate that ``price`` sits inside the day's NSE circuit limits (2/5/10/20%).

    NOTE: If neither ``(lower_circuit, upper_circuit)`` nor ``ref_price`` is supplied,
    returns ``(False, reason)`` explicitly stating that circuit compliance CANNOT be
    enforced without live reference data.
    """
    if isinstance(price, bool) or not isinstance(price, (int, float, Decimal)) or not math.isfinite(float(price)):
        return False, f"price_band: invalid price {price!r}"
    price_f = float(price)
    if price_f <= 0:
        return False, f"price_band: price {price_f} must be > 0"

    if lower_circuit is not None and upper_circuit is not None:
        low_f, high_f = float(lower_circuit), float(upper_circuit)
        if low_f <= 0 or high_f <= 0 or low_f > high_f:
            return False, f"price_band: invalid circuit bounds [{low_f}, {high_f}]"
        if price_f < low_f or price_f > high_f:
            return (
                False,
                f"price_band: price {price_f:.2f} for {symbol or 'instrument'} is outside "
                f"daily circuit band [{low_f:.2f}, {high_f:.2f}] — order will be rejected by NSE",
            )
        return True, f"price_band: price {price_f:.2f} is within explicit circuit band [{low_f:.2f}, {high_f:.2f}]"

    if ref_price is None:
        return (
            False,
            "price_band: CANNOT ENFORCE offline without ref_price (previous close) or explicit "
            "(lower_circuit, upper_circuit) — NSE daily circuit bands require reference price feed",
        )

    if isinstance(ref_price, bool) or not isinstance(ref_price, (int, float, Decimal)) or float(ref_price) <= 0:
        return False, f"price_band: invalid ref_price {ref_price!r} (must be > 0)"

    if band_pct is None:
        band_pct = DEFAULT_T2T_BAND_PCT if is_t2t_symbol(symbol) else DEFAULT_EQ_BAND_PCT
    band_f = float(band_pct)
    if band_f not in VALID_PRICE_BAND_PCTS:
        return (
            False,
            f"price_band: invalid band_pct {band_f}% — NSE circuit bands are {VALID_PRICE_BAND_PCTS}%",
        )

    low_f, high_f = compute_price_band(float(ref_price), band_f, tick=tick)
    if price_f < low_f or price_f > high_f:
        return (
            False,
            f"price_band: price {price_f:.2f} for {symbol or 'instrument'} sits outside "
            f"±{band_f:.0f}% circuit band [{low_f:.2f}, {high_f:.2f}] around ref {float(ref_price):.2f}",
        )
    return (
        True,
        f"price_band: price {price_f:.2f} sits inside ±{band_f:.0f}% circuit band [{low_f:.2f}, {high_f:.2f}]",
    )


check_price_bands = check_price_band


# ---------------------------------------------------------------------------
# 5. ORDER VALUE CAP
# ---------------------------------------------------------------------------
def check_order_value_cap(
    qty_or_value: int | float,
    price: float | None = None,
    max_order_value: float | None = None,
    *,
    min_order_value: float = 1.0,
) -> tuple[bool, str]:
    """Rule 5: Validate per-order notional value against the maximum order value cap.

    Can be called as ``check_order_value_cap(qty, price, max_order_value=50000)``
    or ``check_order_value_cap(order_value, max_order_value=50000)``.
    """
    if isinstance(qty_or_value, bool) or not isinstance(qty_or_value, (int, float, Decimal)):
        return False, f"order_value_cap: invalid numeric input {qty_or_value!r}"
    if price is not None:
        if isinstance(price, bool) or not isinstance(price, (int, float, Decimal)):
            return False, f"order_value_cap: invalid price {price!r}"
        notional = float(qty_or_value) * float(price)
    else:
        notional = float(qty_or_value)

    if not math.isfinite(notional) or notional <= 0:
        return False, f"order_value_cap: order value Rs {notional} must be > 0"
    if notional < float(min_order_value):
        return False, f"order_value_cap: order value Rs {notional:.2f} is below minimum Rs {min_order_value:.2f}"

    if max_order_value is None:
        env_cap = os.getenv("MAX_ORDER_VALUE") or os.getenv("CAPITAL_BUDGET")
        cap = float(env_cap) if env_cap else DEFAULT_MAX_ORDER_VALUE
    else:
        cap = float(max_order_value)

    if cap <= 0:
        return False, f"order_value_cap: invalid max_order_value {cap}"
    if notional > EXCHANGE_RETAIL_ORDER_VALUE_CAP:
        return (
            False,
            f"order_value_cap: order value Rs {notional:,.2f} exceeds retail sanity cap "
            f"Rs {EXCHANGE_RETAIL_ORDER_VALUE_CAP:,.2f} (and strategy cap Rs {cap:,.2f})",
        )
    if round(notional, 2) > round(cap, 2):
        return (
            False,
            f"order_value_cap: order notional Rs {notional:,.2f} exceeds per-order cap Rs {cap:,.2f}",
        )
    return True, f"order_value_cap: notional Rs {notional:,.2f} is within cap Rs {cap:,.2f}"


check_order_value = check_order_value_cap


# ---------------------------------------------------------------------------
# 6. T2T / -BE SERIES
# ---------------------------------------------------------------------------
def check_t2t_series(
    symbol: str,
    *,
    allow_t2t: bool = False,
    same_day_stop_strategy: bool = True,
) -> tuple[bool, str]:
    """Rule 6: Hard check excluding Trade-to-Trade (-BE/-BZ/-SZ/-T) stocks from same-day SL strategies.

    T2T stocks are delivery-only (no intraday netting/square-off), so a same-day SL-M
    protective stop cannot execute on the day of entry.
    """
    if not symbol or not str(symbol).strip():
        return False, "t2t_series: empty symbol"
    full = nse(str(symbol))
    if is_t2t_symbol(symbol):
        if allow_t2t or not same_day_stop_strategy:
            return (
                True,
                f"t2t_series: {full} is T2T/BE-series (explicitly permitted via allow_t2t=True; "
                f"WARNING: no same-day SL execution possible)",
            )
        return (
            False,
            f"t2t_series: {full} trades in NSE Trade-to-Trade (BE) series — delivery-only, "
            f"intraday square-off prohibited, so a same-day stop-loss CANNOT execute",
        )
    return True, f"t2t_series: {full} is regular non-T2T series (same-day SL supported)"


check_t2t = check_t2t_series


# ---------------------------------------------------------------------------
# 7. VALIDITY
# ---------------------------------------------------------------------------
def will_stop_survive_overnight(validity: str) -> bool:
    """Return True if an order with ``validity`` survives across trading days on the exchange.

    - ``DAY``: False (cancelled automatically by NSE at 15:30 IST close).
    - ``IOC``: False (cancelled immediately if not filled).
    - ``GTT``: True (Good-Till-Triggered persists across sessions).
    """
    return str(validity or "").upper().strip() == "GTT"


def check_validity(
    validity: str,
    *,
    is_stop: bool = True,
    overnight_position: bool = False,
    has_next_day_rearm: bool = True,
) -> tuple[bool, str]:
    """Rule 7: Validate order validity and expose whether a protective stop survives overnight."""
    val = str(validity or "").upper().strip()
    if val not in VALID_VALIDITIES:
        return (
            False,
            f"validity: {validity!r} is invalid; Fyers/NSE accepts only {VALID_VALIDITIES}",
        )
    if is_stop and val == "IOC":
        return (
            False,
            "validity: IOC (Immediate-or-Cancel) is invalid for a resting stop order — "
            "it would cancel immediately and leave the position naked",
        )
    survives = will_stop_survive_overnight(val)
    if val == "DAY":
        if overnight_position and not has_next_day_rearm:
            return (
                False,
                "validity: DAY stop expires at 15:30 IST market close (survives_overnight=False) "
                "and has_next_day_rearm=False — refusing to leave overnight position unprotected",
            )
        return (
            True,
            "validity: DAY accepted (survives_overnight=False: expires at 15:30 IST close; "
            "next-day self-heal/re-arm required to keep overnight positions protected)",
        )
    return True, f"validity: {val} accepted (survives_overnight={survives})"


# ---------------------------------------------------------------------------
# 8. PRODUCT TYPE
# ---------------------------------------------------------------------------
def describe_product_type(product: str) -> str:
    """Return an explicit human-readable explanation of a Fyers ``productType``."""
    p = str(product or "").upper().strip()
    return PRODUCT_CONSEQUENCES.get(p, f"Unknown productType {product!r}")


def check_product_type(
    product: str,
    *,
    symbol: str = "",
    hold_overnight: bool = True,
) -> tuple[bool, str]:
    """Rule 8: Validate Fyers ``productType`` and make its overnight/square-off consequence explicit."""
    p = str(product or "").upper().strip()
    if p not in VALID_PRODUCT_TYPES:
        return (
            False,
            f"product_type: {product!r} is not a valid Fyers productType (expected one of {VALID_PRODUCT_TYPES})",
        )
    consequence = describe_product_type(p)
    fo = is_fo_symbol(symbol) if symbol else False

    if fo and p == "CNC":
        return (
            False,
            f"product_type: CNC is for cash equity delivery only and is rejected on F&O symbol {symbol!r} "
            f"(use MARGIN or INTRADAY)",
        )
    if not fo and p == "MARGIN":
        return (
            False,
            f"product_type: {consequence}",
        )
    if symbol and is_t2t_symbol(symbol) and p != "CNC":
        return (
            False,
            f"product_type: T2T/BE-series symbol {nse(symbol)} requires CNC delivery productType, got {p}",
        )
    if hold_overnight and p == "INTRADAY":
        return (
            False,
            f"product_type: {consequence}",
        )
    return True, f"product_type: {consequence}"


check_product = check_product_type


# ---------------------------------------------------------------------------
# 9. MARKET / AMO HOURS
# ---------------------------------------------------------------------------
def _to_ist(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(IST)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def check_market_hours(
    now: datetime | None = None,
    *,
    offline_order: bool = False,
    allow_amo: bool = False,
    is_stop: bool = True,
    holidays: set[str] | None = None,
) -> tuple[bool, str]:
    """Rule 9: Validate order timing against NSE regular session (09:15-15:30 IST Mon-Fri) and AMO rules."""
    ts = _to_ist(now)
    date_str = ts.strftime("%Y-%m-%d")
    t = ts.time()

    is_weekend = ts.weekday() >= 5
    is_holiday = bool(holidays and date_str in holidays)
    in_pre_open = (not is_weekend and not is_holiday) and (PRE_OPEN_START_TIME <= t < MARKET_OPEN_TIME)
    in_regular = (not is_weekend and not is_holiday) and (MARKET_OPEN_TIME <= t <= MARKET_CLOSE_TIME)

    if in_regular:
        if offline_order:
            return (
                False,
                f"market_hours: offlineOrder=True (AMO) cannot be sent during live NSE market hours "
                f"({ts.strftime('%H:%M:%S')} IST) — set offlineOrder=False",
            )
        return True, f"market_hours: {ts.strftime('%Y-%m-%d %H:%M:%S')} IST is within regular NSE hours (09:15-15:30 IST)"

    if in_pre_open and is_stop:
        return (
            False,
            f"market_hours: {ts.strftime('%H:%M:%S')} IST is inside NSE pre-open session (09:00-09:15 IST); "
            f"SL-M stop orders are rejected during pre-open",
        )

    session_desc = (
        f"weekend ({ts.strftime('%A')})"
        if is_weekend
        else f"NSE holiday ({date_str})"
        if is_holiday
        else f"outside 09:15-15:30 IST ({ts.strftime('%H:%M:%S')} IST)"
    )
    if offline_order and allow_amo:
        return (
            True,
            f"market_hours: {session_desc} — AMO (offlineOrder=True) explicitly enabled",
        )
    return (
        False,
        f"market_hours: order at {ts.strftime('%Y-%m-%d %H:%M:%S')} IST is {session_desc}; "
        f"regular order (offlineOrder=False) will be rejected by NSE — refuse or use AMO (offlineOrder=True)",
    )


check_amo_hours = check_market_hours


# ---------------------------------------------------------------------------
# 10. DUPLICATE GUARD
# ---------------------------------------------------------------------------
def check_duplicate_guard(
    symbol: str,
    side: str | int,
    qty: int,
    open_orders: list[dict[str, Any]] | None = None,
    *,
    position_qty: int | None = None,
) -> tuple[bool, str]:
    """Rule 10: Never rest two stops for the same (symbol, side) or exceed held position_qty on SELL."""
    if not symbol or not str(symbol).strip():
        return False, "duplicate_guard: empty symbol"
    norm_side = _normalize_side(side)
    if norm_side is None:
        return False, f"duplicate_guard: invalid order side {side!r} (expected BUY/SELL or 1/-1)"
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or int(qty) <= 0:
        return False, f"duplicate_guard: invalid qty {qty!r}"
    qty_i = int(qty)
    target_sym = plain(str(symbol).upper().strip())

    existing_same_side: list[dict[str, Any]] = []
    existing_sell_qty = 0
    for o in open_orders or []:
        if not isinstance(o, dict):
            continue
        status = o.get("status")
        # In PaperBroker/LiveBroker open_orders(), rows are already filtered to open/pending,
        # but if raw orderbook rows are passed, check status too:
        if status is not None and status not in ("pending", "open", 4, 6, "4", "6"):
            continue
        o_sym = plain(str(o.get("symbol") or "").upper().strip())
        o_side = _normalize_side(o.get("side"))
        if o_sym != target_sym or o_side != norm_side:
            continue
        existing_same_side.append(o)
        if o_side == "SELL":
            try:
                existing_sell_qty += int(o.get("qty") or 0)
            except (TypeError, ValueError):
                pass

    if existing_same_side:
        ids = [str(o.get("id", "?")) for o in existing_same_side]
        risk_desc = "oversell/accidental short risk" if norm_side == "SELL" else "double-entry/budget overrun risk"
        return (
            False,
            f"duplicate_guard: {target_sym} already has {len(existing_same_side)} resting {norm_side} "
            f"order(s) (id={','.join(ids)}) — refusing duplicate stop ({risk_desc})",
        )

    if norm_side == "SELL" and position_qty is not None:
        pos_q = int(position_qty)
        if existing_sell_qty + qty_i > pos_q:
            return (
                False,
                f"duplicate_guard: SELL qty {qty_i} + resting SELL qty {existing_sell_qty} = "
                f"{existing_sell_qty + qty_i} exceeds held position_qty {pos_q} for {target_sym} "
                f"(oversell/short risk)",
            )

    return True, f"duplicate_guard: no conflicting {norm_side} stop resting for {target_sym}"


check_duplicate_stop = check_duplicate_guard


# ---------------------------------------------------------------------------
# Additional Fyers / NSE Order Rules in the Same Class (Rules 11-13)
# ---------------------------------------------------------------------------
_ORDER_TAG_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")


def check_order_tag(order_tag: str | None) -> tuple[bool, str]:
    """Rule 11: Fyers v3 orderTag must be alphanumeric and <= 20 characters."""
    if order_tag is None or order_tag == "":
        return True, "order_tag: empty/omitted tag allowed"
    if not isinstance(order_tag, str):
        return False, f"order_tag: must be str, got {type(order_tag).__name__}"
    if len(order_tag) > 20:
        return False, f"order_tag: {order_tag!r} length {len(order_tag)} exceeds Fyers 20-char limit"
    if not _ORDER_TAG_RE.match(order_tag):
        return (
            False,
            f"order_tag: {order_tag!r} contains non-alphanumeric characters (Fyers requires [A-Za-z0-9] only)",
        )
    return True, f"order_tag: {order_tag!r} is valid alphanumeric (<=20 chars)"


def check_sl_trigger_direction(
    side: str | int,
    trigger_price: float,
    ltp: float | None = None,
) -> tuple[bool, str]:
    """Rule 12: SL-M trigger must be ahead of current market LTP (BUY trigger > LTP; SELL trigger < LTP).

    If ``ltp is None``, explicitly states that trigger-vs-LTP direction cannot be enforced
    without a live market quote.
    """
    norm_side = _normalize_side(side)
    if norm_side is None:
        return False, f"sl_trigger_direction: invalid side {side!r}"
    if ltp is None:
        return (
            False,
            "sl_trigger_direction: CANNOT ENFORCE offline without live ltp quote — "
            "NSE rejects BUY stops <= LTP and SELL stops >= LTP",
        )
    trig_f = float(trigger_price)
    ltp_f = float(ltp)
    if ltp_f <= 0 or trig_f <= 0:
        return False, f"sl_trigger_direction: trigger ({trig_f}) and ltp ({ltp_f}) must be > 0"
    if norm_side == "BUY" and trig_f <= ltp_f:
        return (
            False,
            f"sl_trigger_direction: BUY stop trigger {trig_f:.2f} <= current LTP {ltp_f:.2f} "
            f"(stop is behind the market; NSE will reject or fill immediately at market)",
        )
    if norm_side == "SELL" and trig_f >= ltp_f:
        return (
            False,
            f"sl_trigger_direction: SELL stop trigger {trig_f:.2f} >= current LTP {ltp_f:.2f} "
            f"(stop is behind the market; use market_sell fallback instead of placing doomed stop)",
        )
    return True, f"sl_trigger_direction: {norm_side} trigger {trig_f:.2f} is valid vs LTP {ltp_f:.2f}"


_SYMBOL_RE = re.compile(r"^(?:(NSE|BSE):)?([A-Z0-9&_]+)(?:-([A-Z0-9]+))?$")


def check_symbol_format(symbol: str) -> tuple[bool, str]:
    """Rule 13: Symbol must be a valid non-empty NSE/BSE identifier."""
    if not isinstance(symbol, str) or not symbol.strip():
        return False, f"symbol_format: symbol {symbol!r} must be a non-empty string"
    s = symbol.upper().strip()
    if ":" in s and not s.startswith(("NSE:", "BSE:")):
        return False, f"symbol_format: unsupported exchange prefix in {symbol!r} (expected NSE: or BSE:)"
    if not _SYMBOL_RE.match(s):
        return False, f"symbol_format: malformed symbol {symbol!r}"
    return True, f"symbol_format: {nse(s)} is well-formed"


# ---------------------------------------------------------------------------
# Unified Multi-Rule Entry Point: validate_order(...)
# ---------------------------------------------------------------------------
def validate_order(
    symbol: str,
    side: str | int,
    qty: int | float,
    price: float,
    product: str,
    validity: str,
    *,
    tick: float = DEFAULT_EQUITY_TICK,
    lot_size: int | None = None,
    lot_sizes: dict[str, int] | None = None,
    freeze_qty: int | None = None,
    freeze_map: dict[str, int] | None = None,
    ref_price: float | None = None,
    band_pct: float | None = None,
    lower_circuit: float | None = None,
    upper_circuit: float | None = None,
    max_order_value: float | None = None,
    allow_t2t: bool = False,
    is_stop: bool = True,
    overnight_position: bool = False,
    has_next_day_rearm: bool = True,
    hold_overnight: bool = True,
    now: datetime | None = None,
    offline_order: bool = False,
    allow_amo: bool = False,
    open_orders: list[dict[str, Any]] | None = None,
    position_qty: int | None = None,
    order_tag: str | None = "cupbot",
    ltp: float | None = None,
    strict_unverifiable: bool = False,
) -> list[str]:
    """Run all exchange constraint checks on an order and return EVERY violation found.

    Returns an empty list ``[]`` when the order satisfies all applicable constraints.
    Never short-circuits on the first failure: a multi-fault order returns all faults.
    """
    violations: list[str] = []

    # 0. Symbol format & side check
    ok_sym, reason_sym = check_symbol_format(symbol)
    if not ok_sym:
        violations.append(reason_sym)

    norm_side = _normalize_side(side)
    if norm_side is None:
        violations.append(f"side: invalid side {side!r} (expected BUY/SELL or 1/-1)")

    # 1. Tick size
    ok_tick, reason_tick = check_tick_size(price, tick=tick, allow_zero_market=not is_stop)
    if not ok_tick:
        violations.append(reason_tick)

    # 2. Lot size
    ok_lot, reason_lot = check_lot_size(qty, symbol=symbol, lot_size=lot_size, lot_sizes=lot_sizes)
    if not ok_lot:
        violations.append(reason_lot)

    # 3. Freeze quantity
    ok_freeze, reason_freeze = check_freeze_quantity(
        qty, symbol=symbol, freeze_qty=freeze_qty, freeze_map=freeze_map
    )
    if not ok_freeze:
        violations.append(reason_freeze)

    # 4. Price bands (checked when reference price/circuit bounds are given, or when price <= 0,
    #    or when strict_unverifiable=True requests explicit refusal on missing live circuit data)
    if (
        ref_price is not None
        or (lower_circuit is not None and upper_circuit is not None)
        or strict_unverifiable
        or (isinstance(price, (int, float)) and float(price) <= 0 and is_stop)
    ):
        ok_band, reason_band = check_price_band(
            price,
            ref_price=ref_price,
            band_pct=band_pct,
            lower_circuit=lower_circuit,
            upper_circuit=upper_circuit,
            symbol=symbol,
            tick=tick,
        )
        if not ok_band:
            violations.append(reason_band)

    # 5. Order value cap
    ok_val_cap, reason_val_cap = check_order_value_cap(qty, price, max_order_value=max_order_value)
    if not ok_val_cap:
        violations.append(reason_val_cap)

    # 6. T2T / -BE series
    ok_t2t, reason_t2t = check_t2t_series(symbol, allow_t2t=allow_t2t, same_day_stop_strategy=is_stop)
    if not ok_t2t:
        violations.append(reason_t2t)

    # 7. Validity
    ok_validity, reason_validity = check_validity(
        validity,
        is_stop=is_stop,
        overnight_position=overnight_position,
        has_next_day_rearm=has_next_day_rearm,
    )
    if not ok_validity:
        violations.append(reason_validity)

    # 8. Product type
    ok_prod, reason_prod = check_product_type(
        product, symbol=symbol, hold_overnight=hold_overnight
    )
    if not ok_prod:
        violations.append(reason_prod)

    # 9. Market / AMO hours (checked when `now` is provided or `offline_order` is set)
    if now is not None or offline_order:
        ok_hrs, reason_hrs = check_market_hours(
            now=now,
            offline_order=offline_order,
            allow_amo=allow_amo,
            is_stop=is_stop,
        )
        if not ok_hrs:
            violations.append(reason_hrs)

    # 10. Duplicate guard
    if norm_side is not None and (open_orders is not None or position_qty is not None):
        ok_dup, reason_dup = check_duplicate_guard(
            symbol, norm_side, int(qty) if isinstance(qty, (int, float)) and qty > 0 else 1,
            open_orders=open_orders, position_qty=position_qty,
        )
        if not ok_dup:
            violations.append(reason_dup)

    # 11. Order tag format
    ok_tag, reason_tag = check_order_tag(order_tag)
    if not ok_tag:
        violations.append(reason_tag)

    # 12. Stop trigger direction vs live LTP (when ltp is provided or strict_unverifiable=True)
    if is_stop and norm_side is not None and (ltp is not None or strict_unverifiable):
        ok_dir, reason_dir = check_sl_trigger_direction(norm_side, price, ltp=ltp)
        if not ok_dir:
            violations.append(reason_dir)

    return violations


# ---------------------------------------------------------------------------
# CLI Self-Audit Report
# ---------------------------------------------------------------------------
def main() -> int:
    print("=== FYERS / NSE EXCHANGE-CONSTRAINT SWEEP ===")
    for idx, (rule_name, meta) in enumerate(RULE_ENFORCEABILITY.items(), start=1):
        status = (
            "ENFORCED OFFLINE"
            if meta["enforced_offline"] is True
            else f"REQUIRES LIVE INPUT ({meta['enforced_offline']})"
        )
        print(f"  {idx:2d}. {rule_name:<18} [{status}] — {meta['note']}")

    ok_ast, msg_ast = check_outgoing_price_paths()
    print(f"\nOutgoing price-path AST audit: {'PASS' if ok_ast else 'FAIL'} — {msg_ast}")
    return 0 if ok_ast else 1


if __name__ == "__main__":
    sys.exit(main())
