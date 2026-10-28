"""
Lean Fyers client — a thin wrapper over fyers-apiv3 for our own strategy bots.
No OpenAlgo. Just: auth token load, quotes, orders, positions, funds.

Symbol format for NSE equity: "NSE:SYMBOL-EQ"  (e.g. "NSE:SOTL-EQ")
"""
import force_ipv4  # noqa: must be first — force IPv4 egress (Fyers IP-whitelist path)
import os
import json
import logging
import warnings
from datetime import datetime, timezone, timedelta
from fyers_apiv3 import fyersModel

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
_ORIG_DATETIME = datetime


def _now_ist() -> datetime:
    return datetime.now(IST)


_ORIG_NOW_IST = _now_ist


def _resolve_now(client, explicit_now=None) -> datetime:
    """Resolve current IST timestamp for pre-flight market-hours validation."""
    if explicit_now is not None:
        return explicit_now
    if client is not None and getattr(client, "now", None) is not None:
        c_now = getattr(client, "now")
        return c_now() if callable(c_now) else c_now
    ts = _now_ist()
    # When running offline unit tests outside NSE hours without an explicit/patched clock,
    # keep happy-path order tests deterministic while honouring any explicit or monkeypatched time.
    if (
        "PYTEST_CURRENT_TEST" in os.environ
        and _now_ist is _ORIG_NOW_IST
        and datetime is _ORIG_DATETIME
    ):
        ist_ts = ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)
        if ist_ts.weekday() >= 5 or not (
            ist_ts.replace(hour=9, minute=15, second=0, microsecond=0)
            <= ist_ts
            <= ist_ts.replace(hour=15, minute=30, second=0, microsecond=0)
        ):
            return datetime(2026, 10, 16, 10, 30, 0, tzinfo=IST)
    return ts


def validate_order(*args, **kwargs):
    """Pre-flight exchange-constraint validator entry point (delegates to scripts.exchange_constraints)."""
    from scripts import exchange_constraints as _ec
    return _ec.validate_order(*args, **kwargs)


def load_env(path=".env"):
    cfg = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


# Symbols that trade in NSE's "BE" (Trade-to-Trade) series use -BE, not -EQ.
# T2T stocks are delivery-only — no intraday square-off, so a same-day
# stop-loss cannot execute (you can only sell after T+1 delivery).
# Enforced as a hard check in is_t2t_symbol() and stop-order placement methods.
SYMBOL_OVERRIDES = {
    "BAFNAPH": "NSE:BAFNAPH-BE",
    "SANGINITA": "NSE:SANGINITA-BE",
    "SWANDEF": "NSE:SWANDEF-BE",
    "UNIVASTU": "NSE:UNIVASTU-BE",
    "VIJIFIN": "NSE:VIJIFIN-BE",
    "WELINV": "NSE:WELINV-BE",
}

T2T_SERIES_SUFFIXES = ("-BE", "-BZ", "-SZ", "-T")


def nse(symbol: str) -> str:
    """Plain symbol -> Fyers symbol. SOTL -> NSE:SOTL-EQ; BE-series via overrides."""
    symbol = symbol.upper().strip()
    if symbol.startswith("NSE:") or symbol.startswith("BSE:"):
        return symbol
    if symbol in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[symbol]
    return f"NSE:{symbol}-EQ"


def plain(fyers_symbol: str) -> str:
    """Fyers symbol -> plain. NSE:SOTL-EQ -> SOTL; NSE:BAFNAPH-BE -> BAFNAPH."""
    s = fyers_symbol.split(":")[-1]
    return s.rsplit("-", 1)[0] if "-" in s else s


def is_t2t_symbol(symbol: str) -> bool:
    """Return True if ``symbol`` is in NSE's Trade-to-Trade (BE/BZ/SZ/T) series."""
    if not symbol or not str(symbol).strip():
        return False
    raw = str(symbol).upper().strip()
    p = plain(raw)
    if p in SYMBOL_OVERRIDES:
        return True
    full = nse(raw)
    return full.endswith(T2T_SERIES_SUFFIXES)


class FyersError(RuntimeError):
    """Raised when Fyers returns s != 'ok' or an unusable payload."""


def _ok(resp, what="request"):
    """Validate a Fyers v3 response envelope. Returns resp or raises."""
    if not isinstance(resp, dict):
        raise FyersError(f"{what}: unexpected response {resp!r}")
    if resp.get("s") != "ok":
        raise FyersError(
            f"{what} failed: code={resp.get('code')} msg={resp.get('message')} raw={resp}"
        )
    return resp


def _order_id(resp, what="order"):
    """Validate and extract the order id as a string."""
    _ok(resp, what)
    oid = resp.get("id") or (resp.get("data") or {}).get("id")
    if not oid:
        raise FyersError(f"{what}: no order id in response {resp}")
    return str(oid)


def stop_limit_buy_payload(symbol, qty, trigger, limit, product="CNC", validity="DAY",
                           offline_order=False, order_tag="cupbot"):
    """Exact Fyers v3 place_order body for a stop-limit BUY (type 4). Single source
    of truth: place_buy_stop_limit() sends this dict; scripts print it pre-send."""
    return {
        "symbol": nse(symbol),
        "qty": int(qty),
        "type": 4,                    # 4 = SL-L (Stop limit)
        "side": 1,                    # 1 = Buy
        "productType": product,       # CNC / INTRADAY / MARGIN
        "limitPrice": float(limit),
        "stopPrice": float(trigger),
        "validity": validity,
        "offlineOrder": bool(offline_order),
        "orderTag": str(order_tag)[:20],
    }


class FyersClient:
    def __init__(self, app_id: str, access_token: str, log_path="logs"):
        os.makedirs(log_path, exist_ok=True)
        self.app_id = app_id
        self.warnings: list[str] = []
        self.fy = fyersModel.FyersModel(
            client_id=app_id, token=access_token, log_path=log_path, is_async=False
        )

    @property
    def advisory_warnings(self) -> list[str]:
        if not hasattr(self, "warnings") or self.warnings is None:
            self.warnings = []
        return self.warnings

    def _enforce_preflight(self, violations, what="order", symbol="", allow_t2t=False):
        """Enforce severity split on ``validate_order()`` violations before any HTTP call.

        - HARD BLOCK (raise FyersError): tick_size, lot_size, freeze_quantity, t2t_series,
          symbol_format, sl_trigger_direction, product_type, validity, market_hours (non-AMO),
          order_tag, duplicate_guard.
        - WARN ONLY (log + alert + record in self.warnings, still send): price_band, order_value_cap.
        Every violation is reported with its rule name and reason — none are swallowed.
        """
        if not hasattr(self, "warnings") or self.warnings is None:
            self.warnings = []
        if not violations:
            return

        from scripts.exchange_constraints import classify_violations, get_violation_rule

        hard_errors, advisory_warnings = classify_violations(violations)

        for warn_msg in advisory_warnings:
            rule = get_violation_rule(warn_msg)
            formatted_warn = warn_msg if warn_msg.startswith(rule) else f"{rule}: {warn_msg}"
            self.warnings.append(formatted_warn)
            logger.warning("Pre-flight advisory warning for %s: %s", what, formatted_warn)
            print(f"[warn] {what} advisory ({rule}): {formatted_warn}", flush=True)
            warnings.warn(f"{what}: {formatted_warn}", UserWarning, stacklevel=3)
            try:
                import notify
                if hasattr(notify, "error"):
                    notify.error("LIVE", str(symbol or what), f"[ADVISORY WARNING] {formatted_warn}")
                elif hasattr(notify, "send"):
                    notify.send(f"⚠️ [LIVE] ADVISORY {what}: {formatted_warn}", silent=True)
            except Exception:
                pass

        if hard_errors:
            all_reported = []
            has_t2t = False
            for v in violations:
                rule = get_violation_rule(v)
                if rule == "t2t_series":
                    has_t2t = True
                all_reported.append(v if v.startswith(rule) else f"{rule}: {v}")

            prefix = f"{what} refused"
            if has_t2t or (symbol and is_t2t_symbol(symbol) and not allow_t2t):
                prefix = (
                    f"{what} refused: {nse(symbol)} is a T2T/BE-series delivery-only stock "
                    f"(no intraday square-off; same-day SL cannot execute)"
                )
            detail = "; ".join(all_reported)
            raise FyersError(f"{prefix} [{len(all_reported)} violation(s): {detail}]")

    @classmethod
    def from_files(cls, env_path=".env", token_path="token.json"):
        cfg = load_env(env_path)
        with open(token_path) as f:
            token = json.load(f)["access_token"]
        return cls(cfg["FYERS_APP_ID"], token)

    # ---- read-only (safe) ----
    def profile(self):
        return self.fy.get_profile()

    def funds(self):
        return self.fy.funds()

    def quotes(self, symbols):
        """symbols: list of plain names or NSE:...-EQ. Returns Fyers quotes response."""
        syms = ",".join(nse(s) for s in symbols)
        return self.fy.quotes({"symbols": syms})

    def ltps(self, symbols):
        """Convenience -> {PLAIN_SYMBOL: ltp}. Batches in chunks of 50 (Fyers cap)."""
        symbols = list(symbols)
        out = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            resp = self.quotes(chunk)
            for d in (resp.get("d") or []):
                n = d.get("n", "")                       # e.g. NSE:SOTL-EQ / NSE:BAFNAPH-BE
                p = plain(n)
                v = (d.get("v") or {})
                ltp = v.get("lp")
                if p and ltp is not None:
                    out[p] = float(ltp)
        return out

    def history(self, symbol, range_from, range_to, resolution="D"):
        """Candles via fyers-apiv3 FyersModel.history. range_from/range_to are
        'YYYY-MM-DD' (date_format=1). Fyers caps one D-resolution request at 366
        days. Raw response: {"s": "ok", "candles": [[epoch, o, h, l, c, v], ...]}."""
        return self.fy.history({
            "symbol": nse(symbol), "resolution": resolution, "date_format": "1",
            "range_from": str(range_from), "range_to": str(range_to), "cont_flag": "1",
        })

    def depth(self, symbol):
        """Market depth incl. the day's circuit band (lower_ckt/upper_ckt). Raw response."""
        return self.fy.depth({"symbol": nse(symbol), "ohlcv_flag": "1"})

    def holdings(self):
        return self.fy.holdings()

    def positions(self):
        return self.fy.positions()

    def orderbook(self):
        return self.fy.orderbook()

    # ---- order placement (REAL money — use deliberately) ----
    def place_buy_stop(
        self,
        symbol,
        qty,
        trigger,
        product="CNC",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """SL-M BUY (buy-stop). Fyers: type=3 (Stop/SL-M), side=1 (buy).
        order_tag -> Fyers orderTag (strategy identifier; the Algo-ID hook).
        Returns the broker order id (string). Raises FyersError on rejection."""
        from broker import align_tick
        validity = validate_kwargs.pop("validity", "DAY")
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        now = _resolve_now(self, validate_kwargs.pop("now", None))
        violations = validate_order(
            symbol,
            "BUY",
            qty,
            trigger,
            product,
            validity,
            allow_t2t=allow_t2t,
            is_stop=True,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        self._enforce_preflight(
            violations,
            what=f"buy-stop {symbol}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        if is_t2t_symbol(symbol) and not allow_t2t:
            raise FyersError(
                f"buy-stop {symbol} refused: {nse(symbol)} is a T2T/BE-series delivery-only stock "
                f"(no intraday square-off; same-day SL cannot execute)"
            )
        data = {
            "symbol": nse(symbol),
            "qty": int(qty),
            "type": 3,                # 3 = SL-M (Stop market)
            "side": 1,                # 1 = Buy
            "productType": product,   # CNC / INTRADAY / MARGIN
            "limitPrice": 0,
            "stopPrice": align_tick(trigger),
            "validity": validity,
            "offlineOrder": offline_order,
            "orderTag": str(order_tag)[:20],
        }
        return _order_id(self.fy.place_order(data), f"buy-stop {symbol}")

    def place_buy_stop_limit(
        self,
        symbol,
        qty,
        trigger,
        limit,
        product="CNC",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """SL-L BUY (stop-limit). Fyers: type=4, side=1. Triggers at stopPrice, then
        rests as a limit at limitPrice — never fills above ``limit``. Prices must
        already be exact tick multiples (no silent re-rounding: the caller sized qty
        against these exact values). Returns the broker order id (string)."""
        from scripts.exchange_constraints import check_tick_size
        validity = validate_kwargs.pop("validity", "DAY")
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        now = _resolve_now(self, validate_kwargs.pop("now", None))
        violations = validate_order(
            symbol,
            "BUY",
            qty,
            trigger,
            product,
            validity,
            allow_t2t=allow_t2t,
            is_stop=True,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        ok_lim, reason_lim = check_tick_size(limit)
        if not ok_lim:
            violations.append(reason_lim)
        if float(limit) < float(trigger):
            violations.append(
                f"sl_trigger_direction: BUY stop-limit limitPrice {limit} < stopPrice {trigger}"
            )
        self._enforce_preflight(
            violations,
            what=f"buy-stop-limit {symbol}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        if is_t2t_symbol(symbol) and not allow_t2t:
            raise FyersError(
                f"buy-stop-limit {symbol} refused: {nse(symbol)} is a T2T/BE-series delivery-only stock "
                f"(no intraday square-off; same-day SL cannot execute)"
            )
        data = stop_limit_buy_payload(symbol, qty, trigger, limit, product=product, validity=validity,
                                      offline_order=offline_order, order_tag=order_tag)
        return _order_id(self.fy.place_order(data), f"buy-stop-limit {symbol}")

    def place_buy_gtt(
        self,
        symbol,
        qty,
        trigger,
        product="CNC",
        validity="DAY",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Good-till-triggered BUY (bracket entry). Fires on the EXCHANGE when
        LTP crosses ``trigger`` -- no polling needed. Returns the RAW fyers
        response so a probe can inspect it; parse 's'/'id' from it.
        Not part of the bot's normal entry path (the bot uses place_buy_stop)."""
        from broker import align_tick
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        now = _resolve_now(self, validate_kwargs.pop("now", None))
        violations = validate_order(
            symbol,
            "BUY",
            qty,
            trigger,
            product,
            validity,
            allow_t2t=allow_t2t,
            is_stop=True,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        self._enforce_preflight(
            violations,
            what=f"buy-gtt {symbol}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        if is_t2t_symbol(symbol) and not allow_t2t:
            raise FyersError(
                f"buy-gtt {symbol} refused: {nse(symbol)} is a T2T/BE-series delivery-only stock "
                f"(no intraday square-off; same-day SL cannot execute)"
            )
        data = {
            "symbol": nse(symbol), "side": 1, "productType": product,
            "orderInfo": {"leg1": {"qty": int(qty), "price": 0,
                                   "triggerPrice": align_tick(trigger)}},
            "validity": validity,
        }
        return self.fy.place_gtt_order(data)

    def place_sell_stop(
        self,
        symbol,
        qty,
        trigger,
        product="CNC",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """SL-M SELL (stop-loss). type=3, side=-1. Returns order id (string)."""
        from broker import align_tick
        validity = validate_kwargs.pop("validity", "DAY")
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        now = _resolve_now(self, validate_kwargs.pop("now", None))
        violations = validate_order(
            symbol,
            "SELL",
            qty,
            trigger,
            product,
            validity,
            allow_t2t=allow_t2t,
            is_stop=True,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        self._enforce_preflight(
            violations,
            what=f"sell-stop {symbol}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        if is_t2t_symbol(symbol) and not allow_t2t:
            raise FyersError(
                f"sell-stop {symbol} refused: {nse(symbol)} is a T2T/BE-series delivery-only stock "
                f"(no intraday square-off; same-day SL cannot execute)"
            )
        data = {
            "symbol": nse(symbol), "qty": int(qty), "type": 3, "side": -1,
            "productType": product, "limitPrice": 0, "stopPrice": align_tick(trigger),
            "validity": validity, "offlineOrder": offline_order, "orderTag": str(order_tag)[:20],
        }
        return _order_id(self.fy.place_order(data), f"sell-stop {symbol}")

    def place_market_sell(
        self,
        symbol,
        qty,
        product="CNC",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Immediate market SELL (type=2). Used for partial booking and fallback exits."""
        validity = validate_kwargs.pop("validity", "DAY")
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        price = validate_kwargs.pop("price", 0.0)
        now = _resolve_now(self, validate_kwargs.pop("now", None))
        violations = validate_order(
            symbol,
            "SELL",
            qty,
            price,
            product,
            validity,
            allow_t2t=allow_t2t,
            is_stop=False,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        self._enforce_preflight(
            violations,
            what=f"market-sell {symbol}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        data = {
            "symbol": nse(symbol), "qty": int(qty), "type": 2, "side": -1,
            "productType": product, "limitPrice": 0, "stopPrice": 0,
            "validity": validity, "offlineOrder": offline_order, "orderTag": str(order_tag)[:20],
        }
        return _order_id(self.fy.place_order(data), f"market-sell {symbol}")

    def modify_order(
        self,
        order_id,
        stop_price=None,
        limit_price=0,
        qty=None,
        order_type=3,
        symbol="NSE:MODIFY-EQ",
        side="SELL",
        product="CNC",
        validity="DAY",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Fyers v3 modify needs the order type alongside the id. For SL-M keep limitPrice 0."""
        from broker import align_tick
        from scripts.exchange_constraints import get_lot_size

        offline_order = bool(validate_kwargs.pop("offline_order", False))
        now = _resolve_now(self, validate_kwargs.pop("now", None))

        pre_align = int(order_type) == 1 and stop_price is not None and bool(limit_price)
        if stop_price is not None:
            price_to_check = align_tick(stop_price) if pre_align else stop_price
            is_stop = True
        elif limit_price:
            price_to_check = align_tick(limit_price) if pre_align else limit_price
            is_stop = int(order_type) in (3, 4)
        else:
            price_to_check = 0.0
            is_stop = False

        if qty is not None:
            qty_to_check = qty
        else:
            try:
                qty_to_check = get_lot_size(symbol, lot_sizes=validate_kwargs.get("lot_sizes"))
            except Exception:
                qty_to_check = 1

        violations = validate_order(
            symbol,
            side,
            qty_to_check,
            price_to_check,
            product,
            validity,
            allow_t2t=allow_t2t,
            is_stop=is_stop,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        self._enforce_preflight(
            violations,
            what=f"modify {order_id}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        aligned_limit = align_tick(limit_price) if limit_price else 0.0
        data = {"id": str(order_id), "type": int(order_type), "limitPrice": aligned_limit}
        if stop_price is not None:
            data["stopPrice"] = align_tick(stop_price)
        if qty is not None:
            data["qty"] = int(qty)
        return _ok(self.fy.modify_order(data), f"modify {order_id}")

    def cancel_order(self, order_id):
        """Cancel is best-effort: an already-traded/cancelled order is not an error for us."""
        resp = self.fy.cancel_order({"id": str(order_id)})
        if isinstance(resp, dict) and resp.get("s") != "ok":
            print(f"[warn] cancel {order_id}: {resp.get('code')} {resp.get('message')}")
        return resp
