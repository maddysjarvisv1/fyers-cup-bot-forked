"""intraday_paper — PAPER-ONLY intraday tracker for the 'open==low green 5-min
bar' ATM index-option strategy (NIFTY + BANKNIFTY).

Read-only market data (quotes/ltps -> 5-min bars) + pure signal/exit logic +
paper trade tracking to a JSONL ledger. NO ORDERS, NO FUNDS MOVEMENT.
"""
__version__ = "0.1.0"