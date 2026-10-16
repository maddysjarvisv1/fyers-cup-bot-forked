"""Deterministic gap handling tests (Case A: gap-down exit; Case B: gap-up missed entry).

Covers:
  - Case A (open position gapped down below SL):
      * immediate market SELL of full remaining qty at current LTP
      * NO sell-stop re-armed before the market sell
      * works for both full position and post-partial qty_remaining, and across day-roll
  - Case B (entry gapped up at/above trigger):
      * durable row in missed_entries (id, day, symbol, trigger, rim, qty, ltp,
        gap_pct, reason, created_at)
      * immediate alert (MISSED ENTRY, symbol, trigger, price seen)
      * strictly one row and one alert per symbol per day (dedupe across cycles & restarts)
      * does NOT re-arm and does NOT chase (even if price later dips into near band)
      * exposed in dashboard.py ("Missed entries" section + API payload)
  - Regression: normal in-band entry arms and fills without triggering missed_entry
  - Determinism guard: AST import inspection confirms zero AI/LLM/JEV/Laya imports
    anywhere in the trading path.
"""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import CupBot, ScriptedFeed
from broker import PaperBroker
from ledger import Ledger
from tracking import Tracker
import dashboard


WATCH = [{"symbol": "WINNER", "rim": 100.0, "trigger": 104.0, "qty": 100}]


def _state(entry=100.0, qty_total=100, qty_remaining=None, phase="ACTIVE",
           sl=94.0, highest=100.0):
    if qty_remaining is None:
        qty_remaining = qty_total
    return {
        "entry": entry,
        "qty_total": qty_total,
        "qty_remaining": qty_remaining,
        "phase": phase,
        "sl": sl,
        "highest": highest,
    }


# ---------------------------------------------------------------------------
# CASE A — OPEN POSITION GAPPED DOWN BELOW STOP -> immediate market sell, no re-arm
# ---------------------------------------------------------------------------
def test_gap_down_open_position_market_sells_full_remaining_qty_without_rearming_stop(
    double, alert_log, tmp_path
):
    """Position with recorded SL=94.0, LTP gaps down to 85.0 at open, no resting stop:
    must market-SELL full remaining qty (60) at 85.0 and NEVER re-arm a stop first."""
    double.positions_map["WINNER"] = {"qty": 60, "avg": 100.0}
    bot = CupBot(
        double,
        lambda syms: {"WINNER": 85.0},
        WATCH,
        state_path=str(tmp_path / "st.json"),
        mode="LIVE",
        alerts=True,
        verbose=False,
    )
    # Recorded SL is 94.0; overnight DAY stop expired so no stop is resting
    bot.pos_state["WINNER"] = {
        "state": _state(entry=100.0, qty_total=100, qty_remaining=60, sl=94.0),
        "sell_stop_id": None,
    }

    bot.cycle()

    # 1. No stop was re-armed before (or after) the market exit
    assert "place_sell_stop" not in double.calls, (
        f"must NOT re-arm a sell-stop when LTP is already below SL; calls={double.calls}"
    )
    assert all(c[0] != "SELL" for c in double.placed)
    # 2. Immediate market sell of the full remaining qty at current LTP
    assert double.calls == ["market_sell"]
    assert double.sold == [("WINNER", 60, 85.0)]
    # 3. Position state cleaned up and exit alert emitted with realized gap-down P&L
    assert "WINNER" not in bot.pos_state
    assert len(alert_log["exit_"]) == 1
    mode, sym, exit_px, qty, pnl = alert_log["exit_"][0]
    assert (mode, sym, exit_px, qty, pnl) == ("LIVE", "WINNER", 85.0, 60, (85.0 - 100.0) * 60)


def test_gap_down_with_stale_stop_id_clears_and_market_sells_without_rearm(
    double, alert_log, tmp_path
):
    """Even if pos_state still holds yesterday's expired sell_stop_id (not in open_orders),
    _exit_pass detects the missing live stop, skips self-heal because ltp <= sl, and
    market-sells the full remaining qty immediately."""
    double.positions_map["WINNER"] = {"qty": 100, "avg": 100.0}
    bot = CupBot(
        double,
        lambda syms: {"WINNER": 88.5},
        WATCH,
        state_path=str(tmp_path / "st.json"),
        mode="LIVE",
        alerts=True,
        verbose=False,
    )
    bot.pos_state["WINNER"] = {
        "state": _state(entry=100.0, qty_total=100, qty_remaining=100, sl=94.0),
        "sell_stop_id": "EXPIRED_YESTERDAY",
    }

    bot.cycle()

    assert double.calls == ["market_sell"]
    assert double.sold == [("WINNER", 100, 88.5)]
    assert "WINNER" not in bot.pos_state


# ---------------------------------------------------------------------------
# CASE B — ENTRY GAPPED UP PAST TRIGGER -> record in missed_entries + immediate alert
# ---------------------------------------------------------------------------
def test_gap_up_records_missed_entry_and_alerts_once(double, alert_log, tmp_path):
    """When price is already at/above trigger with no open order/position, record
    one row in missed_entries with exact trigger/rim/qty/ltp/gap_pct/reason, fire
    an immediate missed_entry alert, and place NO buy order."""
    db = Ledger(str(tmp_path / "fyers_ledger.db"))
    tk = Tracker(ledger=db)
    # Trigger is 104.0; price opens gapped up at 109.20 (+5.0% above trigger)
    bot = CupBot(
        double,
        lambda syms: {"WINNER": 109.20},
        WATCH,
        state_path=str(tmp_path / "st.json"),
        mode="LIVE",
        alerts=True,
        verbose=False,
        tracker=tk,
    )

    bot.cycle()

    # Do NOT arm or chase
    assert double.placed == []
    assert double.calls == []
    assert "WINNER" not in bot._in_flight
    assert "WINNER" not in bot.pos_state

    # Durable ledger row in missed_entries
    rows = db.missed_entries()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == 1
    assert row["day"] == bot.day
    assert row["symbol"] == "WINNER"
    assert row["trigger"] == pytest.approx(104.0)
    assert row["rim"] == pytest.approx(100.0)
    assert row["qty"] == 100
    assert row["ltp"] == pytest.approx(109.20)
    assert row["gap_pct"] == pytest.approx((109.20 - 104.0) / 104.0 * 100.0, rel=1e-4)
    assert row["reason"] == "gapped_up_past_trigger"
    assert row["created_at"]

    # Immediate alert fired
    assert len(alert_log["missed_entry"]) == 1
    ev = alert_log["missed_entry"][0]
    assert ev[0] == "LIVE"
    assert ev[1] == "WINNER"
    assert ev[2] == pytest.approx(104.0)
    assert ev[3] == pytest.approx(109.20)
    assert ev[4] == pytest.approx(5.0)


def test_gap_up_dedupes_across_cycles_and_does_not_chase_on_pullback(
    double, alert_log, tmp_path
):
    """A symbol sitting above its trigger across multiple cycles must NOT alert
    again or insert duplicate rows, and must NOT arm if it later dips into the
    near-rim band on the same day ('the entry is a miss.. do not chase, do not re-arm')."""
    db = Ledger(str(tmp_path / "fyers_ledger.db"))
    tk = Tracker(ledger=db)
    prices = {"WINNER": 108.0}
    bot = CupBot(
        double,
        lambda syms: dict(prices),
        WATCH,
        state_path=str(tmp_path / "st.json"),
        mode="LIVE",
        alerts=True,
        verbose=False,
        tracker=tk,
    )

    # Cycle 1: gap up at 108.0 -> 1 row, 1 alert
    bot.cycle()
    assert len(db.missed_entries()) == 1
    assert len(alert_log["missed_entry"]) == 1

    # Cycle 2 & 3: still above trigger (110.0, then exact trigger 104.0) -> no new alert, no new row
    prices["WINNER"] = 110.0
    bot.cycle()
    prices["WINNER"] = 104.0
    bot.cycle()
    assert len(db.missed_entries()) == 1
    assert len(alert_log["missed_entry"]) == 1
    assert double.placed == []

    # Cycle 4: pulls back into near band (99.5) on the same day -> still a miss, do NOT re-arm
    prices["WINNER"] = 99.5
    bot.cycle()
    assert double.placed == [], "must NOT re-arm or chase a symbol that already missed today"
    assert alert_log["armed"] == []

    # Restart bot on the same day against same state/ledger -> still deduped
    prices["WINNER"] = 112.0
    bot2 = CupBot(
        double,
        lambda syms: dict(prices),
        WATCH,
        state_path=str(tmp_path / "st.json"),
        mode="LIVE",
        alerts=True,
        verbose=False,
        tracker=tk,
    )
    bot2.cycle()
    assert len(db.missed_entries()) == 1
    assert len(alert_log["missed_entry"]) == 1


def test_notify_missed_entry_message_states_symbol_trigger_and_price(monkeypatch):
    """Verify the real notify.missed_entry text clearly states MISSED ENTRY,
    symbol, trigger, and price seen."""
    spec = importlib.util.spec_from_file_location(
        "_real_notify", Path(__file__).resolve().parent.parent / "notify.py"
    )
    real_notify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_notify)

    sent = []
    monkeypatch.setattr(real_notify, "send", lambda text, silent=False: sent.append((text, silent)))
    real_notify.missed_entry("LIVE", "NAZARA", 436.30, 452.10, gap_pct=3.6214)

    assert len(sent) == 1
    text, silent = sent[0]
    assert silent is False
    assert "MISSED ENTRY" in text
    assert "NAZARA" in text
    assert "436.3" in text
    assert "452.1" in text


# ---------------------------------------------------------------------------
# REGRESSION — normal in-band entry is completely unaffected
# ---------------------------------------------------------------------------
def test_normal_in_band_entry_arms_and_fills_without_missed_entry( alert_log, tmp_path):
    """A symbol approaching from below (in the 1% near band) arms its buy-stop,
    fills on breakout at trigger, and never records a missed_entries row."""
    db = Ledger(str(tmp_path / "fyers_ledger.db"))
    tk = Tracker(ledger=db)
    broker = PaperBroker(state_path=str(tmp_path / "paper.json"), reset=True)
    feed = ScriptedFeed({"WINNER": [99.5, 104.0, 106.0]})
    bot = CupBot(
        broker,
        feed,
        WATCH,
        state_path=str(tmp_path / "st.json"),
        mode="PAPER",
        alerts=True,
        verbose=False,
        tracker=tk,
    )

    # Cycle 1: 99.5 is within 1% of rim 100.0 -> arms buy-stop @ 104.0
    bot.cycle()
    assert len(broker.open_orders("WINNER")) == 1
    assert len(alert_log["armed"]) == 1
    assert db.missed_entries() == []
    assert alert_log["missed_entry"] == []

    # Cycle 2: 104.0 fills the armed buy-stop -> opens position + places initial SL stop
    bot.cycle()
    assert "WINNER" in bot.pos_state
    assert len(alert_log["entry"]) == 1
    assert db.missed_entries() == []
    assert alert_log["missed_entry"] == []

    # Cycle 3: 106.0 above trigger while in position -> managed normally, not a missed entry
    bot.cycle()
    assert db.missed_entries() == []
    assert alert_log["missed_entry"] == []


# ---------------------------------------------------------------------------
# DASHBOARD — exposes "Missed entries" in payload and HTML
# ---------------------------------------------------------------------------
def test_dashboard_exposes_missed_entries_in_payload_and_html(tmp_path):
    db_path = tmp_path / "fyers_ledger.db"
    wl_path = tmp_path / "watchlist.csv"
    wl_path.write_text("symbol,rim,trigger,qty\nWINNER,100.0,104.0,100\n", encoding="utf-8")

    db = Ledger(str(db_path))
    db.missed_entry(
        symbol="WINNER",
        trigger=104.0,
        ltp=109.2,
        rim=100.0,
        qty=100,
        reason="gapped_up_past_trigger",
        day="2026-10-16",
    )

    app = dashboard.App(str(db_path), str(wl_path))

    class _DummyHandler(dashboard.Handler):
        def __init__(self):
            self.server = SimpleNamespace(app=app)
            self.headers_out = []
            self.status_code = None
            self.body = b""
            self.wfile = SimpleNamespace(write=self._write)

        def _write(self, b):
            self.body += b

        def send_response(self, code):
            self.status_code = code

        def send_header(self, k, v):
            self.headers_out.append((k, v))

        def end_headers(self):
            pass

    h = _DummyHandler()
    payload = h._payload()
    assert payload["missed_count"] == 1
    assert len(payload["missed_entries"]) == 1
    assert payload["missed_entries"][0]["symbol"] == "WINNER"
    assert payload["missed_entries"][0]["ltp"] == pytest.approx(109.2)

    h._html()
    html_out = h.body.decode("utf-8")
    assert "Missed entries" in html_out
    assert "WINNER" in html_out
    assert "109.2" in html_out
    assert "gapped_up_past_trigger" in html_out


# ---------------------------------------------------------------------------
# DETERMINISM — no model/AI/JEV/Laya imports anywhere in the trading path
# ---------------------------------------------------------------------------
FORBIDDEN_AI_PREFIXES = {
    "openai",
    "anthropic",
    "google",
    "genai",
    "vertexai",
    "langchain",
    "llama_index",
    "llamaindex",
    "litellm",
    "ollama",
    "transformers",
    "torch",
    "tensorflow",
    "keras",
    "vllm",
    "jev",
    "laya",
}

TRADING_PATH_FILES = (
    "bot.py",
    "broker.py",
    "cup_sl_engine.py",
    "fyers_client.py",
    "ledger.py",
    "tracking.py",
    "notify.py",
    "run.py",
    "dashboard.py",
)


def test_no_ai_or_model_imports_in_trading_path():
    root = Path(__file__).resolve().parent.parent
    for rel in TRADING_PATH_FILES:
        src = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0].lower())
                    imported.add(alias.name.lower())
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0].lower())
                imported.add(node.module.lower())
        bad = imported & FORBIDDEN_AI_PREFIXES
        assert not bad, f"{rel} imports forbidden AI/model module(s): {sorted(bad)}"
