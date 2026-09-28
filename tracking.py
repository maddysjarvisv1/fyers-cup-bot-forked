"""Bridge from the bot's lifecycle events to the durable trade ledger.

The bot already emits ``_alert(event, sym, *payload)`` at every meaningful
point (armed / entry / exit_ / partial / breakeven / trail / error). A
:class:`Tracker` listens to those same calls and writes the corresponding
durable rows into a :class:`ledger.Ledger` — without touching the alert/
notification path. Tracking is therefore independent of whether Telegram
alerts are enabled.
"""
from ledger import Ledger


class Tracker:
    """Record bot lifecycle events for a symbol into a Ledger."""

    def __init__(self, ledger=None, path=None):
        self.ledger = ledger or Ledger(path or ":memory:")

    def on_alert(self, event, sym, *payload, **extra):
        if event == "send" or not sym:
            return
        if event == "error" and str(sym).upper() == "ALL":
            self.ledger.audit("error", None, " ".join(str(x) for x in payload))
            return
        sym = str(sym)
        fn = getattr(self, f"_on_{event}", None)
        if fn:
            try:
                fn(sym, *payload)
            except Exception as e:                      # never break the trade path
                self.ledger.audit("tracker_error", sym, f"{e}")

    # --- lifecycle mapping --------------------------------------------------
    def _on_armed(self, sym, ltp=None, trig=None, qty=None):
        fields = {"trigger": trig} if trig is not None else {}
        if qty is not None:
            fields["qty"] = int(qty)
        if ltp is not None:
            fields["last_price"] = ltp
        self.ledger.set_status(sym, "order", **fields)
        self.ledger.order_event(sym, "BUY", "SL-M", trig, qty, "placed")

    def _on_entry(self, sym, px, qty, sl=None):
        if px is None:
            return
        self.ledger.set_status(sym, "in_trade", entry_price=px, last_price=px)
        self.ledger.trade_event(sym, "BUY", qty, px, "ENTRY")

    def _on_exit_(self, sym, exit_px, qty, pnl):
        self.ledger.set_status(sym, "stop_loss", last_price=exit_px, pnl=pnl or 0)
        self.ledger.trade_event(sym, "SELL", qty, exit_px, "STOP_EXIT", pnl=pnl)

    def _on_partial(self, sym, q, px, sl=None, keep=None):
        self.ledger.set_status(sym, "in_trade", last_price=px)
        self.ledger.trade_event(sym, "SELL", q, px, "PARTIAL")

    def _on_breakeven(self, sym, *a):
        self.ledger.audit("breakeven", sym)

    def _on_trail(self, sym, *a):
        self.ledger.audit("trail", sym)

    def _on_error(self, sym, message):
        self.ledger.set_status(sym, "error", error=str(message)[:200])
        self.ledger.audit("error", sym, str(message)[:500])