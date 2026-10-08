"""Self-contained cup-lifecycle dashboard (stdlib only, no build step).

Serves an HTML page grouping every watchlist cup by lifecycle state:

    trade_ready   ready to arm    getting_ready  approaching its trigger (lalpath)
    order         buy-stop placed in_trade        position open
    stop_loss     stop-exit booked  error          lifecycle/order error

plus realised pnl. Reads ``fyers_ledger.db`` (the durable outcome ledger) and
the protected ``watchlist.csv`` (read-only here — never mutated).

Run:  python3 dashboard.py                # http://localhost:8123
      python3 dashboard.py --port 9000 --db /path/fyers_ledger.db
API:  GET /api/cups  ->  {"counts": {...}, "cups": [...], "pnl": {total, wins, losses}}
"""
import argparse
import csv
import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ledger import Ledger
import urllib.parse as _up
import auth_ui as _auth

STATUS_LABEL = {
    "trade_ready": "Trade-ready",
    "getting_ready": "Getting ready",
    "order": "Order placed",
    "in_trade": "In trade",
    "stop_loss": "Stop-loss",
    "error": "Error",
}


def load_watchlist(path="watchlist.csv"):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"symbol": str(r["symbol"]).upper(), "rim": float(r["rim"]),
                             "trigger": float(r["trigger"]), "qty": int(r["qty"])})
            except (KeyError, ValueError):
                continue
    return rows


def pnl_stats(ledger, symbol=None):
    rows = ledger._rows(
        "SELECT pnl, reason FROM trades WHERE pnl IS NOT NULL "
        + ("AND symbol=?" if symbol else ""), (symbol,) if symbol else ())
    exits = [r for r in rows if r["reason"] not in ("ENTRY", "PARTIAL")]
    total = sum(r["pnl"] for r in exits)
    wins = len([r for r in exits if r["pnl"] > 0])
    losses = len([r for r in exits if r["pnl"] < 0])
    return {"total": total, "wins": wins, "losses": losses, "n": len(exits)}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/auth"):
            self._auth_page()
        elif self.path.startswith("/api"):
            self._api(self.path)
        else:
            self._html()

    def do_POST(self):
        # The ONLY write endpoint. Sits behind the Traefik + portal-gate ForwardAuth front
        # (https-fyers anchor), so it is never reachable unauthenticated from the internet.
        if not self.path.startswith("/auth"):
            self.send_error(404); return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(n, 65536)).decode("utf-8", "replace")
        except Exception:
            self._json({"ok": False, "error": "could not read body"}); return
        text = raw
        if "auth_code=" not in text and "=" in text:
            # allow a urlencoded form field: auth_url=... / url=... / code=...
            try:
                q = _up.parse_qs(raw)
                for k in ("auth_url", "url", "text", "code"):
                    if q.get(k):
                        text = q[k][0] if k != "code" else "https://x/?auth_code=" + q[k][0]
                        break
            except Exception:
                pass
        try:
            res = _auth.apply_from_text(text)
        except Exception as e:
            res = {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}
        self._json(res)   # result never contains the token or the code

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_page(self):
        st = _auth.status()
        badge = ("<span style='color:#0a0'>FRESH</span>" if st["fresh"]
                 else "<span style='color:#c00'>STALE — login needed</span>")
        bot = ("<span style='color:#0a0'>running</span>" if st["bot_running"]
               else "<span style='color:#c00'>down</span>")
        page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Fyers daily login</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem}}
input,textarea{{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box}}
button{{padding:.7rem 1.2rem;font-size:1rem;margin-top:.8rem}}
.card{{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}}
pre{{background:#f6f6f6;padding:.8rem;border-radius:6px;overflow:auto}}</style></head><body>
<h2>🔐 Fyers daily login</h2>
<div class="card">Token date: <b>{st['token_date'] or 'none'}</b> · status: {badge}<br>
Bot: {bot} {('pid ' + ', '.join(map(str, st['bot_pids']))) if st['bot_pids'] else ''}</div>
<div class="card"><b>Paste the redirected URL here.</b>
<p>Open the login link, sign in, then copy the <i>full</i> redirected URL (it contains
<code>auth_code=...</code>) and paste it below. No PIN required.</p>
<textarea id="u" rows="4" placeholder="https://trade.fyers.in/api-login/default.html?...&auth_code=...&state=jarvis_login"></textarea>
<button onclick="go()">Exchange &amp; restart bot</button>
<pre id="r">—</pre></div>
<script>
async function go(){{const r=document.getElementById('r');r.textContent='working…';
 try{{const res=await fetch('/auth',{{method:'POST',headers:{{'Content-Type':'text/plain'}},
  body:document.getElementById('u').value}});const j=await res.json();r.textContent=JSON.stringify(j,null,1);}}
 catch(e){{r.textContent='error: '+e;}}}}
</script></body></html>"""
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self):
        app = self.server.app
        watch = load_watchlist(app.watchlist)
        report = app.ledger.report(watchlist=watch, prices=app.get_prices(),
                                   max_positions=app.max_positions)
        report["pnl"] = pnl_stats(app.ledger)
        if app.capital_budget:
            used = sum(float(c.get("trigger") or 0) * float(c.get("qty") or 0)
                       for c in report["cups"] if c.get("status") == "in_trade")
            report["capital_budget"] = app.capital_budget
            report["capital_used"] = used
            report["capital_left"] = app.capital_budget - used
        return report

    def _api(self, path):
        body = json.dumps(self._payload()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        report = self._payload()
        counts, cups, pnl = report["counts"], report["cups"], report["pnl"]
        groups = {}
        for c in cups:
            groups.setdefault(c["status"], []).append(c)
        sec = []
        for status in ("trade_ready", "getting_ready", "order", "in_trade", "stop_loss", "error"):
            rows = groups.get(status, [])
            sec.append(
                f'<section><h3>{html.escape(STATUS_LABEL[status])} '
                f'<span class="n">{len(rows)}</span></h3>'
                + ("<p class='none'>—</p>"
                   if not rows else "".join(self._cup_card(c) for c in rows))
                + "</section>")
        body_html = "".join(sec)
        aw = report.get("awaiting") or []
        if aw:
            aw_cards = "".join(self._cup_card(c, tag="READY") for c in aw)
            cap_note = (f" · <b>position slot full ({report['in_trade']}/{report['max_positions']})</b> "
                        "— take a call (wait / free a slot)"
                        if report.get("cap_full") else
                        f" · slots free ({report['in_trade']}/{report.get('max_positions')})")
            fire_html = (f'<div class="fire"><h2>🔥 READY TO FIRE — waiting for a slot</h2>'
                         f'<span class="fn">{len(aw)}{cap_note}</span>{aw_cards}</div>')
        else:
            fire_html = ""
        page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Cup Lifecycle</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{{--g:#3ddc84;--r:#ff5252;--o:#ffb454;--b:#1d2a45;--i:#eaf2ff;--m:#9fb2d0;--bg:#0a101f}}
body{{font-family:ui-sans-serif,system-ui,sans-serif;background:var(--bg);color:var(--i);margin:0;padding:26px}}
h1{{font-size:26px}} h3{{margin:0}} h2{{margin:0 0 6px}}
.bar{{display:flex;gap:10px;background:#0e1626;border:1px solid var(--b);border-radius:12px;padding:16px}}
.bar b{{font-size:30px;color:var(--g)}} .bar span{{color:var(--m)}}
.fire{{background:#2a0f1d;border:2px solid var(--o);border-radius:14px;padding:12px 16px;margin-top:16px}}
.fire b{{font-size:34px;color:var(--o)}} .fire .fn{{color:var(--m);font-size:12px;display:block}}
.fire .card{{background:#1a0f24;border:1px solid var(--o)}}
section{{display:inline-block;vertical-align:top;width:31%;margin:12px 6px;padding:12px 14px;background:#0e1626;border:1px solid var(--b);border-radius:12px}}
.n{{color:var(--o);font-weight:700}} .none{{color:#6b7b99;font-size:12px}}
.card{{background:#111c30;border:1px solid var(--b);border-radius:10px;padding:10px;margin:8px 0}}
.card b{{display:block;font-size:17px}} .m{{color:var(--m);font-size:12px}}
.tag{{display:inline-block;padding:1px 8px;border-radius:8px;font-size:11px;color:var(--i);background:#2a3b5e}}
.pos{{color:var(--g);font-weight:700}} .neg{{color:var(--r);font-weight:700}}
</style></head><body>
<h1>☕ Cup-bot lifecycle &amp; outcomes</h1>
<div class="bar">
  <div><b>₹{pnl['total']:.2f}</b><span> realised pnl ({pnl['wins']}W / {pnl['losses']}L)</span></div>
  <div><b>{counts['in_trade']}</b><span> in trade</span></div>
  <div><b>{counts['order']}</b><span> orders</span></div>
  <div><b>{counts['stop_loss']}</b><span> stop-loss</span></div>
  <div><b>{counts['error']}</b><span> errors</span></div>
  <div><b>{counts['getting_ready']}</b><span> getting ready</span></div>
  <div><b>{counts['trade_ready']}</b><span> trade-ready</span></div>
  <div><b>₹{report.get('capital_left', report.get('capital_budget') or 0):,.0f}</b><span> cap left {f'of ₹{report.get("capital_budget"):,.0f}' if report.get('capital_budget') else '(unset)'}</span></div>
</div>
{fire_html}
{body_html}
</body></html>"""
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cup_card(self, c, tag=None):
        pnl = c.get("pnl")
        pnl_html = f'<span class="m"> pnl <b class="{"pos" if (pnl or 0) >= 0 else "neg"}">{pnl}</b></span>' if pnl is not None else ""
        err = f'<div class="m" style="color:var(--r)">{html.escape(str(c.get("error") or ""))}</div>' if c.get("error") else ""
        tag_html = f'<span class="tag">{html.escape(tag)}</span>' if tag else \
            f'<span class="tag">{html.escape(c["status"])}</span>'
        return (f'<div class="card"><b>{html.escape(c["symbol"])}</b><br>'
                f'<span class="m">trigger {c.get("trigger")}{tag_html}</span>'
                f'{pnl_html}{err}</div>')


class App:
    def __init__(self, db_path, watchlist, get_prices=lambda: {}, max_positions=None,
                 capital_budget=None):
        self.ledger = Ledger(db_path)
        self.watchlist = watchlist
        self.get_prices = get_prices
        self.max_positions = max_positions
        self.capital_budget = capital_budget


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default loopback: the Traefik https-fyers anchor reaches it; pass 0.0.0.0 only to deliberately expose a raw port)")
    ap.add_argument("--db", default="fyers_ledger.db")
    ap.add_argument("--watchlist", default="watchlist.csv")
    ap.add_argument("--max-positions", type=int, default=None)
    ap.add_argument("--capital-budget", type=int, default=None)
    a = ap.parse_args(argv)
    mp = a.max_positions
    if mp is None:
        try:
            for line in open(".env"):
                if line.strip().startswith("MAX_POSITIONS="):
                    mp = int(line.strip().split("=", 1)[1])
                    break
        except (OSError, ValueError):
            mp = None
    cb = a.capital_budget
    if cb is None:
        try:
            for line in open(".env"):
                if line.strip().startswith("CAPITAL_BUDGET="):
                    cb = int(line.strip().split("=", 1)[1])
                    break
        except (OSError, ValueError):
            cb = None
    app = App(a.db, a.watchlist, max_positions=mp, capital_budget=cb)
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    server.app = app
    print(f"cup-bot dashboard on http://localhost:{a.port}  (db={a.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()