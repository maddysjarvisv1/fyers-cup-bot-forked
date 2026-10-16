# AGENTS.md — read this first: start with the graph, not grep

## Your first move in this repo

```bash
graphify explain 'CupBot' --graph graphify-out/graph.json   # a symbol + its connections (file:line)
graphify path run.py bot.py --undirected                     # how two things connect
graphify query 'watchlist' --graph graphify-out/graph.json   # free-text search
```

`graphify-out/graph.json` is a **queryable knowledge graph** of this repo (files, classes, functions,
imports as nodes; calls/imports/references as edges). Traversing it is **far cheaper and more accurate
than re-grepping the tree** — extraction is local tree-sitter AST, so it costs **0 tokens**.

- `graphify-out/GRAPH_REPORT.md` — highlights, hub nodes, stats
- `graphify-out/graph.html` — **clickable visual map** (open in a browser; not committed)
- **Freshness:** `graphify-out/.graphify_checked` holds the commit this graph was verified against.
  Compare it to `git rev-parse --short HEAD`. Do **not** compare `GRAPH_REPORT.md`'s "Built from commit"
  — graphify only rebuilds when code *topology* changes, so that stamp lags by design.
- Kept current by cron `graphify-sweep` (daily 04:00). Refresh by hand: `graphify update .` (seconds).

## Human-readable orientation

`WALKTHROUGH.md` — the strategy in one paragraph, why the daily SEBI login exists, the money guards, the
file map, `watchlist.csv` columns, troubleshooting, and the single-strategy limitation.

## Safety rules for this repo — LIVE MONEY

- `MODE=live` **and** `CONFIRM_LIVE=YES` together place **real orders**. Never arm both without the
  owner's explicit instruction for that session.
- **Never commit** `.env` or `token.json`. The TOTP secret and trading PIN are as sensitive as a password.
  Auth codes are written 0600, used, then **deleted**.
- **`watchlist.csv` is written ONLY by `watchlist_sync.py`** — never hand-edit or append to it. It uses an
  atomic rewrite (temp + `os.replace`), takes a timestamped backup, and validates through the bot's own
  `run.load_watchlist()`. Hand-appending has already corrupted it once (trailing-newline bug).
- **Never push to upstream.** This is a fork — push to `maddysjarvisv1` branches only.
- Running the venv: **clear `PYTHONPATH`** (`env -u PYTHONPATH .venv/bin/python …`). A leaked Python 3.14
  site-packages path breaks numpy from a 3.12 venv.
- One strategy per account. Thread tags must be exact; symbols must not be duplicated across watches.
