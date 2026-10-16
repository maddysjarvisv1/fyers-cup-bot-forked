"""
Regression tests for exchange tick-size alignment (NSE equity tick = 0.05).

Covers:
  1. broker.align_tick helper:
     - exact tick multiples stay unchanged
     - NAZARA trigger 436.28 -> 436.30
     - LALPATHLAB trigger 2126.8 -> 2126.8 (unchanged)
     - 3-decimal inputs (round up and round down)
     - both round-down and round-up cases
     - custom tick override via explicit arg, module constant, and env var
  2. LiveBroker & PaperBroker order methods:
     - place_buy_stop, place_sell_stop, modify_stop, modify_stop_qty (atomic +
       cancel-then-replace fallback) all hand tick-aligned prices to the fake
       Fyers client / paper order store
  3. End-to-end CupBot (including trailing-stop path):
     - NAZARA (436.28) never reaches the broker or client unaligned on arming,
       initial SL, partial-book resize, or trailing stop modify
     - LALPATHLAB (2126.8) is left unchanged
  4. nse_screener.screen_symbol:
     - buy_price and sl_price are tick-aligned
  5. watchlist_sync.sync_watchlist:
     - rim and trigger are tick-aligned when written to watchlist.csv
"""
from __future__ import annotations

from decimal import Decimal
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import broker
from broker import LiveBroker, PaperBroker, align_tick
from bot import CupBot
import watchlist_sync


def _is_tick_multiple(price: float, tick: float = 0.05) -> bool:
    d = Decimal(str(price))
    t = Decimal(str(tick))
    return (d % t) == 0


class FakeFyersClient:
    """Fake FyersClient recording the exact stop_price / trigger handed to it."""

    def __init__(self, fail_modify_qty: bool = False):
        self.fail_modify_qty = fail_modify_qty
        self.buy_stops: list[tuple[str, int, float, str]] = []
        self.sell_stops: list[tuple[str, int, float, str]] = []
        self.modifies: list[tuple[str, dict]] = []
        self.cancels: list[str] = []
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"FY{self._seq:04d}"

    def orderbook(self):
        return {"s": "ok", "orderBook": []}

    def positions(self):
        return {"s": "ok", "netPositions": []}

    def place_buy_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        self.buy_stops.append((symbol, int(qty), trigger, order_tag))
        return self._next_id()

    def place_sell_stop(self, symbol, qty, trigger, order_tag="cupbot"):
        self.sell_stops.append((symbol, int(qty), trigger, order_tag))
        return self._next_id()

    def place_market_sell(self, symbol, qty, order_tag="cupbot"):
        return self._next_id()

    def modify_order(self, order_id, **kwargs):
        if self.fail_modify_qty and "qty" in kwargs:
            raise RuntimeError("combined qty+stopPrice modify rejected")
        self.modifies.append((str(order_id), dict(kwargs)))
        return {"s": "ok"}

    def cancel_order(self, order_id):
        self.cancels.append(str(order_id))
        return {"s": "ok"}


# ---------------------------------------------------------------------------
# 1. Tick-alignment helper unit tests
# ---------------------------------------------------------------------------
def test_align_tick_exact_multiples_unchanged():
    for exact in (0.05, 1.0, 94.0, 100.0, 100.05, 419.5, 436.3, 2045.0, 2126.8):
        aligned = align_tick(exact)
        assert aligned == exact
        assert _is_tick_multiple(aligned)


def test_align_tick_nazara_and_lalpathlab():
    # NAZARA: 419.5 * 1.04 = 436.28 -> 436.30
    assert align_tick(436.28) == 436.30
    assert _is_tick_multiple(align_tick(436.28))

    # LALPATHLAB: 2045.0 * 1.04 = 2126.8 -> unchanged
    assert align_tick(2126.8) == 2126.8
    assert _is_tick_multiple(align_tick(2126.8))


def test_align_tick_round_up_and_round_down():
    # Round-down cases (remainder < 0.025)
    assert align_tick(436.26) == 436.25
    assert align_tick(436.27) == 436.25
    assert align_tick(100.01) == 100.00
    assert align_tick(100.02) == 100.00

    # Round-up cases (remainder >= 0.025)
    assert align_tick(436.28) == 436.30
    assert align_tick(436.29) == 436.30
    assert align_tick(100.03) == 100.05
    assert align_tick(100.04) == 100.05


def test_align_tick_three_decimal_inputs():
    # 3-decimal round-down
    assert align_tick(436.262) == 436.25
    assert align_tick(1999.192) == 1999.20
    assert align_tick(410.122) == 410.10

    # 3-decimal round-up (including exact half-tick 0.025)
    assert align_tick(436.276) == 436.30
    assert align_tick(100.025) == 100.05
    assert align_tick(100.075) == 100.10


def test_align_tick_overridable_via_env_and_module_constant(monkeypatch):
    # Override via explicit argument
    assert align_tick(436.24, tick=0.10) == 436.20
    assert align_tick(436.28, tick=0.25) == 436.25

    # Override via environment variable
    monkeypatch.setenv("TICK_SIZE", "0.10")
    assert align_tick(436.24) == 436.20
    assert align_tick(436.26) == 436.30
    monkeypatch.delenv("TICK_SIZE")

    # Override via module constant
    monkeypatch.setattr(broker, "TICK_SIZE", 0.25)
    assert align_tick(436.28) == 436.25
    assert align_tick(436.40) == 436.50


# ---------------------------------------------------------------------------
# 2. LiveBroker / PaperBroker stop placement and modification
# ---------------------------------------------------------------------------
def test_live_broker_aligns_all_stop_methods(tmp_path):
    fake = FakeFyersClient()
    lb = LiveBroker(fake, tag="cup", state_path=str(tmp_path / "live.json"))

    # place_buy_stop: 436.28 -> 436.30; 2126.8 -> 2126.8
    lb.place_buy_stop("NAZARA", 28, 436.28)
    lb.place_buy_stop("LALPATHLAB", 5, 2126.8)
    assert fake.buy_stops[0] == ("NAZARA", 28, 436.30, "cupbot")
    assert fake.buy_stops[1] == ("LALPATHLAB", 5, 2126.8, "cupbot")

    # place_sell_stop: 410.1032 -> 410.10
    oid = lb.place_sell_stop("NAZARA", 28, 410.1032)
    assert fake.sell_stops[0] == ("NAZARA", 28, 410.10, "cupbot")

    # modify_stop (trailing stop path): 471.1824 -> 471.20
    lb.modify_stop(oid, 471.1824)
    assert fake.modifies[-1] == (oid, {"stop_price": 471.20})

    # modify_stop_qty (atomic path): 471.1824 -> 471.20
    lb.modify_stop_qty(oid, 14, 471.1824, symbol="NAZARA", order_tag="cup")
    assert fake.modifies[-1] == (oid, {"stop_price": 471.20, "qty": 14})


def test_live_broker_modify_stop_qty_fallback_aligns_stop(tmp_path):
    fake = FakeFyersClient(fail_modify_qty=True)
    lb = LiveBroker(fake, tag="cup", state_path=str(tmp_path / "live_fb.json"))

    res = lb.modify_stop_qty("OLD1", 14, 471.1824, symbol="NAZARA", order_tag="cup")
    assert res["s"] == "ok"
    assert fake.cancels == ["OLD1"]
    assert fake.sell_stops[-1] == ("NAZARA", 14, 471.20, "cup")


def test_paper_broker_aligns_all_stop_methods(tmp_path):
    pb = PaperBroker(state_path=str(tmp_path / "paper.json"), reset=True)

    bid = pb.place_buy_stop("NAZARA", 28, 436.28)
    sid = pb.place_sell_stop("NAZARA", 28, 410.1032)
    orders = {o["id"]: o for o in pb.open_orders()}
    assert orders[bid]["trigger"] == 436.30
    assert orders[sid]["trigger"] == 410.10

    pb.modify_stop(sid, 471.1824)
    assert {o["id"]: o for o in pb.open_orders()}[sid]["trigger"] == 471.20

    pb.modify_stop_qty(sid, 14, 480.03, symbol="NAZARA")
    updated = {o["id"]: o for o in pb.open_orders()}[sid]
    assert updated["trigger"] == 480.05
    assert updated["qty"] == 14


# ---------------------------------------------------------------------------
# 3. NAZARA & LALPATHLAB end-to-end through CupBot (including trailing path)
# ---------------------------------------------------------------------------
def test_nazara_never_reaches_broker_unaligned_and_lalpathlab_unchanged(double, tmp_path):
    wl = [
        {"symbol": "NAZARA", "rim": 419.5, "trigger": 436.28, "qty": 28},
        {"symbol": "LALPATHLAB", "rim": 2045.0, "trigger": 2126.8, "qty": 5},
    ]
    prices = {"NAZARA": 418.0, "LALPATHLAB": 2030.0}
    bot = CupBot(
        double,
        lambda syms: {s: prices[s] for s in syms if s in prices},
        wl,
        state_path=str(tmp_path / "st.json"),
        mode="LIVE",
        verbose=False,
    )

    # Cycle 1: arm both inside their 1% rim band
    bot.cycle()

    buys = {sym: trig for side, sym, _qty, trig in double.placed if side == "BUY"}
    assert buys["NAZARA"] == 436.30, "436.28 must never reach the broker unaligned"
    assert buys["LALPATHLAB"] == 2126.8, "2126.8 must be left unchanged"

    # Simulate NAZARA entry fill at 436.30 -> initial SL = 436.30 * 0.94 = 410.122 -> 410.10
    double.pending.clear()
    double.positions_map["NAZARA"] = {"qty": 28, "avg": 436.30}
    prices["NAZARA"] = 436.30
    bot.cycle()

    sells = [trig for side, sym, _qty, trig in double.placed if side == "SELL" and sym == "NAZARA"]
    assert sells == [410.10]
    assert _is_tick_multiple(sells[0])

    # Partial book at +20% (ltp = 523.57 -> 523.57 * 0.90 = 471.21 -> 471.20)
    prices["NAZARA"] = 523.57
    bot.cycle()  # ACTIVE -> BE + TRAILING (books half, modifies stop qty + trigger)
    assert double.modified[-1][1] == 471.20
    assert _is_tick_multiple(double.modified[-1][1])

    # Trailing stop update at a new high that produces a non-tick multiple:
    # ltp = 530.13 -> 530.13 * 0.90 = 477.117 -> 477.12 (round(2)) -> aligned to 477.10
    double.positions_map["NAZARA"]["qty"] = 14
    prices["NAZARA"] = 530.13
    bot.cycle()
    assert double.modified[-1][1] == 477.10
    assert _is_tick_multiple(double.modified[-1][1])


# ---------------------------------------------------------------------------
# 4. Screener output is tick-aligned
# ---------------------------------------------------------------------------
def test_screener_output_is_tick_aligned(monkeypatch):
    pd = pytest.importorskip("pandas")
    import nse_screener

    dates = pd.date_range("2020-01-01", periods=100, freq="D")

    def _run_screen(ath: float, cur: float):
        highs = [100.0] * len(dates)
        highs[0] = ath
        closes = [cur] * len(dates)
        vols = [250000.0] * len(dates)
        df = pd.DataFrame({"High": highs, "Close": closes, "Volume": vols}, index=dates)

        class _FakeTicker:
            def history(self, period="max", auto_adjust=True):
                return df

        monkeypatch.setattr(nse_screener.yf, "Ticker", lambda _s: _FakeTicker())
        return nse_screener.screen_symbol("TEST")

    # NAZARA case: ATH 419.5 -> 419.5 * 1.04 = 436.28 -> aligned to 436.30
    # sl_price = 436.30 * 0.94 = 410.122 -> aligned to 410.10
    nazara = _run_screen(419.5, 415.0)
    assert nazara is not None
    assert nazara["buy_price"] == 436.30
    assert nazara["sl_price"] == 410.10
    assert _is_tick_multiple(nazara["buy_price"])
    assert _is_tick_multiple(nazara["sl_price"])

    # LALPATHLAB case: ATH 2045.0 -> 2045.0 * 1.04 = 2126.8 (unchanged)
    # sl_price = 2126.8 * 0.94 = 1999.192 -> aligned to 1999.20
    lal = _run_screen(2045.0, 2000.0)
    assert lal is not None
    assert lal["buy_price"] == 2126.8
    assert lal["sl_price"] == 1999.20
    assert _is_tick_multiple(lal["buy_price"])
    assert _is_tick_multiple(lal["sl_price"])


# ---------------------------------------------------------------------------
# 5. watchlist_sync aligns rim and trigger when writing watchlist.csv
# ---------------------------------------------------------------------------
def test_watchlist_sync_aligns_trigger_and_rim(tmp_path):
    wl_path = tmp_path / "watchlist.csv"
    cand_path = tmp_path / "nse_screener_candidates.csv"

    # Existing row has unaligned trigger; candidate has unaligned ath and buy_price
    wl_path.write_text(
        "symbol,rim,trigger,qty\n"
        "LALPATHLAB,2045.0,2126.8,5\n"
        "NAZARA,419.5,436.28,28\n",
        encoding="utf-8",
    )
    cand_path.write_text(
        "symbol,ath,ath_date,buy_price,sl_price,qty,capital,current,pct_from_ath\n"
        "UNALIGNED,312.33,2021-05-10,324.82,305.33,20,6496.4,310.0,-0.75\n",
        encoding="utf-8",
    )

    res = watchlist_sync.sync_watchlist(
        watchlist_path=str(wl_path),
        candidates_path=str(cand_path),
        capital_budget=50000,
    )
    by_sym = {r["symbol"]: r for r in res["final"]}
    assert by_sym["LALPATHLAB"] == {"symbol": "LALPATHLAB", "rim": 2045.0, "trigger": 2126.8, "qty": 5}
    assert by_sym["NAZARA"] == {"symbol": "NAZARA", "rim": 419.5, "trigger": 436.30, "qty": 28}
    assert by_sym["UNALIGNED"] == {"symbol": "UNALIGNED", "rim": 312.35, "trigger": 324.80, "qty": 20}

    written = wl_path.read_text(encoding="utf-8")
    assert "436.28" not in written
    assert "NAZARA,419.5,436.3,28" in written
    assert "UNALIGNED,312.35,324.8,20" in written
