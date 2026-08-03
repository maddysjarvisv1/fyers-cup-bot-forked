"""
Lean Fyers client — a thin wrapper over fyers-apiv3 for our own strategy bots.
No OpenAlgo. Just: auth token load, quotes, orders, positions, funds.

Symbol format for NSE equity: "NSE:SYMBOL-EQ"  (e.g. "NSE:SOTL-EQ")
"""
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


def nse(symbol: str) -> str:
    """Plain symbol -> Fyers NSE equity symbol. SOTL -> NSE:SOTL-EQ."""
    symbol = symbol.upper().strip()
    if symbol.startswith("NSE:"):
        return symbol
    return f"NSE:{symbol}-EQ"


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
                n = d.get("n", "")                       # e.g. NSE:SOTL-EQ
                plain = n.split(":")[-1].replace("-EQ", "")
                v = (d.get("v") or {})
                ltp = v.get("lp")
                if plain and ltp is not None:
                    out[plain] = float(ltp)
        return out

    def positions(self):
        return self.fy.positions()

    def orderbook(self):
        return self.fy.orderbook()

    # ---- order placement (REAL money — use deliberately) ----
    def place_buy_stop(self, symbol, qty, trigger, product="CNC"):
        """SL-M BUY (buy-stop). Fyers: type=3 (Stop/SL-M), side=1 (buy)."""
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
        }
        return self.fy.place_order(data)

    def place_sell_stop(self, symbol, qty, trigger, product="CNC"):
        """SL-M SELL (stop-loss). type=3, side=-1."""
        data = {
            "symbol": nse(symbol), "qty": int(qty), "type": 3, "side": -1,
            "productType": product, "limitPrice": 0, "stopPrice": float(trigger),
            "validity": "DAY", "offlineOrder": False,
        }
        return self.fy.place_order(data)

    def modify_order(self, order_id, **kwargs):
        data = {"id": order_id, **kwargs}
        return self.fy.modify_order(data)

    def cancel_order(self, order_id):
        return self.fy.cancel_order({"id": order_id})
