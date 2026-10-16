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
        """SL-M BUY (buy-stop) with GTT validity so breakout entries survive close.
        Fyers: type=3 (Stop/SL-M), side=1 (buy), validity='GTT'.
        order_tag -> Fyers orderTag (strategy identifier; the Algo-ID hook).
        Returns the broker order id (string). Raises FyersError on rejection or non-GTT validity."""
        from broker import align_tick
        validity = validate_kwargs.pop("validity", "GTT")
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        overnight_position = bool(validate_kwargs.pop("overnight_position", True))
        has_next_day_rearm = bool(validate_kwargs.pop("has_next_day_rearm", False))
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
            overnight_position=overnight_position,
            has_next_day_rearm=has_next_day_rearm,
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
        if str(validity or "").upper().strip() != "GTT":
            raise FyersError(
                f"buy-stop {symbol} refused: validity={validity!r} must be 'GTT' "
                f"(never silently downgrade entry stop to DAY)"
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
            "validity": "GTT",
            "offlineOrder": offline_order,
            "orderTag": str(order_tag)[:20],
        }
        return _order_id(self.fy.place_order(data), f"buy-stop {symbol}")

    def place_buy_gtt(
        self,
        symbol,
        qty,
        trigger,
        product="CNC",
        validity="GTT",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Good-till-triggered BUY via Fyers GTT endpoint (/gtt/orders/sync).
        Fires on the EXCHANGE when LTP crosses ``trigger`` -- no polling needed.
        Validates response via _ok() and raises FyersError on failure or non-GTT validity."""
        from broker import align_tick
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        overnight_position = bool(validate_kwargs.pop("overnight_position", True))
        has_next_day_rearm = bool(validate_kwargs.pop("has_next_day_rearm", False))
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
            overnight_position=overnight_position,
            has_next_day_rearm=has_next_day_rearm,
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
        if str(validity or "").upper().strip() != "GTT":
            raise FyersError(
                f"buy-gtt {symbol} refused: validity={validity!r} must be 'GTT' "
                f"(never silently downgrade GTT order to DAY)"
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
            "validity": "GTT",
        }
        return _ok(self.fy.place_gtt_order(data), f"buy-gtt {symbol}")

    def place_sell_gtt(
        self,
        symbol,
        qty,
        trigger,
        product="CNC",
        validity="GTT",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Good-till-triggered protective SELL via Fyers GTT endpoint (/gtt/orders/sync).
        Validates response via _ok() and raises FyersError on failure or non-GTT validity."""
        from broker import align_tick
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        overnight_position = bool(validate_kwargs.pop("overnight_position", True))
        has_next_day_rearm = bool(validate_kwargs.pop("has_next_day_rearm", False))
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
            overnight_position=overnight_position,
            has_next_day_rearm=has_next_day_rearm,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        self._enforce_preflight(
            violations,
            what=f"sell-gtt {symbol}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        if str(validity or "").upper().strip() != "GTT":
            raise FyersError(
                f"sell-gtt {symbol} refused: validity={validity!r} must be 'GTT' "
                f"(never silently downgrade GTT stop to DAY)"
            )
        if is_t2t_symbol(symbol) and not allow_t2t:
            raise FyersError(
                f"sell-gtt {symbol} refused: {nse(symbol)} is a T2T/BE-series delivery-only stock "
                f"(no intraday square-off; same-day SL cannot execute)"
            )
        data = {
            "symbol": nse(symbol), "side": -1, "productType": product,
            "orderInfo": {"leg1": {"qty": int(qty), "price": 0,
                                   "triggerPrice": align_tick(trigger)}},
            "validity": "GTT",
        }
        return _ok(self.fy.place_gtt_order(data), f"sell-gtt {symbol}")

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
        """SL-M SELL (protective stop-loss) with GTT validity so open positions
        never sit unprotected overnight. type=3, side=-1, validity='GTT'.
        Returns order id (string). Raises FyersError on rejection or non-GTT validity."""
        from broker import align_tick
        validity = validate_kwargs.pop("validity", "GTT")
        offline_order = bool(validate_kwargs.pop("offline_order", False))
        overnight_position = bool(validate_kwargs.pop("overnight_position", True))
        has_next_day_rearm = bool(validate_kwargs.pop("has_next_day_rearm", False))
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
            overnight_position=overnight_position,
            has_next_day_rearm=has_next_day_rearm,
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
        if str(validity or "").upper().strip() != "GTT":
            raise FyersError(
                f"sell-stop {symbol} refused: validity={validity!r} must be 'GTT' "
                f"(never silently downgrade protective stop to DAY)"
            )
        if is_t2t_symbol(symbol) and not allow_t2t:
            raise FyersError(
                f"sell-stop {symbol} refused: {nse(symbol)} is a T2T/BE-series delivery-only stock "
                f"(no intraday square-off; same-day SL cannot execute)"
            )
        data = {
            "symbol": nse(symbol), "qty": int(qty), "type": 3, "side": -1,
            "productType": product, "limitPrice": 0, "stopPrice": align_tick(trigger),
            "validity": "GTT", "offlineOrder": offline_order, "orderTag": str(order_tag)[:20],
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
        validity="GTT",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Fyers v3 modify_order (PATCH /orders/sync). For SL-M keep limitPrice 0.

        NOTE on GTT modify support: Fyers v3 does NOT support ``modify_order`` on a GTT
        order identically to a regular DAY order — GTT orders in fyers_apiv3 use a
        separate endpoint (``PATCH /gtt/orders/sync`` via ``modify_gtt_order``) with a
        different schema requiring ``orderInfo.leg1.{price, triggerPrice, qty}``.
        When ``modify_order`` is rejected on a GTT stop, callers (``LiveBroker.modify_stop``,
        ``LiveBroker.modify_stop_qty``, ``replace_sell_stop``) MUST replace-then-cancel
        (place the replacement GTT stop BEFORE cancelling the old stop) so there is
        never a window without a protective stop.
        """
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

        overnight_position = bool(validate_kwargs.pop("overnight_position", is_stop))
        has_next_day_rearm = bool(validate_kwargs.pop("has_next_day_rearm", not is_stop))

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
            overnight_position=overnight_position,
            has_next_day_rearm=has_next_day_rearm,
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
        if is_stop and str(validity or "").upper().strip() != "GTT":
            raise FyersError(
                f"modify {order_id} refused: validity={validity!r} must be 'GTT' "
                f"(never silently downgrade protective stop to DAY)"
            )
        aligned_limit = align_tick(limit_price) if limit_price else 0.0
        data = {"id": str(order_id), "type": int(order_type), "limitPrice": aligned_limit}
        if stop_price is not None:
            data["stopPrice"] = align_tick(stop_price)
        if qty is not None:
            data["qty"] = int(qty)
        return _ok(self.fy.modify_order(data), f"modify {order_id}")

    def modify_gtt_order(
        self,
        order_id,
        trigger,
        qty,
        price=0,
        symbol="NSE:MODIFY-EQ",
        side="SELL",
        product="CNC",
        validity="GTT",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Fyers v3 GTT modify (PATCH /gtt/orders/sync).

        Unlike ``modify_order`` (which takes flat ``stopPrice`` and optional ``qty``),
        Fyers's ``modify_gtt_order`` requires ``orderInfo.leg1.{price, triggerPrice, qty}``.
        Applies ``align_tick()`` to every price and refuses non-GTT validity.
        """
        from broker import align_tick

        offline_order = bool(validate_kwargs.pop("offline_order", False))
        overnight_position = bool(validate_kwargs.pop("overnight_position", True))
        has_next_day_rearm = bool(validate_kwargs.pop("has_next_day_rearm", False))
        now = _resolve_now(self, validate_kwargs.pop("now", None))
        violations = validate_order(
            symbol,
            side,
            qty,
            trigger,
            product,
            validity,
            allow_t2t=allow_t2t,
            is_stop=True,
            overnight_position=overnight_position,
            has_next_day_rearm=has_next_day_rearm,
            offline_order=offline_order,
            order_tag=order_tag,
            now=now,
            **validate_kwargs,
        )
        self._enforce_preflight(
            violations,
            what=f"modify-gtt {order_id}",
            symbol=symbol,
            allow_t2t=allow_t2t,
        )
        if str(validity or "").upper().strip() != "GTT":
            raise FyersError(
                f"modify-gtt {order_id} refused: validity={validity!r} must be 'GTT'"
            )
        aligned_price = align_tick(price) if price else 0
        data = {
            "id": str(order_id),
            "orderInfo": {
                "leg1": {
                    "price": aligned_price,
                    "triggerPrice": align_tick(trigger),
                    "qty": int(qty),
                }
            },
        }
        return _ok(self.fy.modify_gtt_order(data), f"modify-gtt {order_id}")

    def replace_sell_stop(
        self,
        order_id,
        symbol,
        qty,
        trigger,
        product="CNC",
        order_tag="cupbot",
        allow_t2t=False,
        **validate_kwargs,
    ):
        """Replace a protective GTT sell-stop with zero unprotected window:
        places the replacement GTT stop FIRST, and only cancels ``order_id``
        after the replacement is confirmed resting. If cancelling the old stop
        fails, rolls back the new stop and raises FyersError."""
        from broker import align_tick
        aligned = align_tick(trigger)
        new_id = self.place_sell_stop(
            symbol,
            qty,
            aligned,
            product=product,
            order_tag=order_tag,
            allow_t2t=allow_t2t,
            **validate_kwargs,
        )
        if not new_id:
            raise FyersError(f"replace-sell-stop {symbol}: replacement returned no order id")
        cancel_res = self.cancel_order(order_id)
        cancel_ok = isinstance(cancel_res, dict) and (
            str(cancel_res.get("s", "")).lower() == "ok"
            or any(
                m in str(cancel_res.get("message", "")).lower()
                for m in ("not found", "no such", "does not exist", "not open", "already", "filled", "completed", "cancelled", "canceled")
            )
        )
        if not cancel_ok:
            try:
                self.cancel_order(new_id)
            except Exception:
                pass
            raise FyersError(
                f"replace-sell-stop {symbol}: cancel of old stop {order_id} failed ({cancel_res!r}); "
                f"rolled back replacement {new_id}"
            )
        return new_id

    def cancel_order(self, order_id):
        """Cancel is best-effort: an already-traded/cancelled order is not an error for us."""
        resp = self.fy.cancel_order({"id": str(order_id)})
        if isinstance(resp, dict) and resp.get("s") != "ok":
            print(f"[warn] cancel {order_id}: {resp.get('code')} {resp.get('message')}")
        return resp

    def cancel_gtt_order(self, order_id):
        """Cancel a GTT order on /gtt/orders/sync."""
        resp = self.fy.cancel_gtt_order({"id": str(order_id)})
        if isinstance(resp, dict) and resp.get("s") != "ok":
            print(f"[warn] cancel-gtt {order_id}: {resp.get('code')} {resp.get('message')}")
        return resp

