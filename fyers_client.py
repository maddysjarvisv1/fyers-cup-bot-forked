"""
Lean Fyers client — a thin wrapper over fyers-apiv3 for our own strategy bots.
No OpenAlgo. Just: auth token load, quotes, orders, positions, funds.

Symbol format for NSE equity: "NSE:SYMBOL-EQ"  (e.g. "NSE:SOTL-EQ")
"""
import force_ipv4  # noqa: must be first — force IPv4 egress (Fyers IP-whitelist path)
import os
import json
from fyers_apiv3 import fyersModel


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
# NOTE: T2T stocks are delivery-only — no intraday square-off, so a same-day
# stop-loss cannot execute (you can only sell after T+1 delivery). Trade with care.
SYMBOL_OVERRIDES = {
    "BAFNAPH": "NSE:BAFNAPH-BE",
    "SANGINITA": "NSE:SANGINITA-BE",
    "SWANDEF": "NSE:SWANDEF-BE",
    "UNIVASTU": "NSE:UNIVASTU-BE",
    "VIJIFIN": "NSE:VIJIFIN-BE",
    "WELINV": "NSE:WELINV-BE",
}


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
        self.fy = fyersModel.FyersModel(
            client_id=app_id, token=access_token, log_path=log_path, is_async=False
        )

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
    def place_buy_stop(self, symbol, qty, trigger, product="CNC", order_tag="cupbot"):
        """SL-M BUY (buy-stop). Fyers: type=3 (Stop/SL-M), side=1 (buy).
        order_tag -> Fyers orderTag (strategy identifier; the Algo-ID hook).
        Returns the broker order id (string). Raises FyersError on rejection."""
        data = {
            "symbol": nse(symbol),
            "qty": int(qty),
            "type": 3,                # 3 = SL-M (Stop market)
            "side": 1,                # 1 = Buy
            "productType": product,   # CNC / INTRADAY / MARGIN
            "limitPrice": 0,
            "stopPrice": float(trigger),
            "validity": "DAY",
            "offlineOrder": False,
            "orderTag": str(order_tag)[:20],
        }
        return _order_id(self.fy.place_order(data), f"buy-stop {symbol}")

    def place_buy_gtt(self, symbol, qty, trigger, product="CNC", validity="DAY", order_tag="cupbot"):
        """Good-till-triggered BUY (bracket entry). Fires on the EXCHANGE when
        LTP crosses ``trigger`` -- no polling needed. Returns the RAW fyers
        response so a probe can inspect it; parse 's'/'id' from it.
        Not part of the bot's normal entry path (the bot uses place_buy_stop)."""
        data = {
            "symbol": nse(symbol), "side": 1, "productType": product,
            "orderInfo": {"leg1": {"qty": int(qty), "price": 0,
                                   "triggerPrice": float(trigger)}},
            "validity": validity,
        }
        return self.fy.place_gtt_order(data)

    def place_sell_stop(self, symbol, qty, trigger, product="CNC", order_tag="cupbot"):
        """SL-M SELL (stop-loss). type=3, side=-1. Returns order id (string)."""
        data = {
            "symbol": nse(symbol), "qty": int(qty), "type": 3, "side": -1,
            "productType": product, "limitPrice": 0, "stopPrice": float(trigger),
            "validity": "DAY", "offlineOrder": False, "orderTag": str(order_tag)[:20],
        }
        return _order_id(self.fy.place_order(data), f"sell-stop {symbol}")

    def place_market_sell(self, symbol, qty, product="CNC", order_tag="cupbot"):
        """Immediate market SELL (type=2). Used for partial booking and fallback exits."""
        data = {
            "symbol": nse(symbol), "qty": int(qty), "type": 2, "side": -1,
            "productType": product, "limitPrice": 0, "stopPrice": 0,
            "validity": "DAY", "offlineOrder": False, "orderTag": str(order_tag)[:20],
        }
        return _order_id(self.fy.place_order(data), f"market-sell {symbol}")

    def modify_order(self, order_id, stop_price=None, limit_price=0, qty=None, order_type=3):
        """Fyers v3 modify needs the order type alongside the id. For SL-M keep limitPrice 0."""
        data = {"id": str(order_id), "type": int(order_type), "limitPrice": float(limit_price)}
        if stop_price is not None:
            data["stopPrice"] = float(stop_price)
        if qty is not None:
            data["qty"] = int(qty)
        return _ok(self.fy.modify_order(data), f"modify {order_id}")

    def cancel_order(self, order_id):
        """Cancel is best-effort: an already-traded/cancelled order is not an error for us."""
        resp = self.fy.cancel_order({"id": str(order_id)})
        if isinstance(resp, dict) and resp.get("s") != "ok":
            print(f"[warn] cancel {order_id}: {resp.get('code')} {resp.get('message')}")
        return resp
