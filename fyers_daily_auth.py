#!/usr/bin/env python3
"""
Fyers daily-login helper for the cup-bot fork — REFRESH-TOKEN EDITION (stub for review).

Modes:
  fyers_daily_auth.py check            -> exit 0 if today's token is fresh, 1 if stale/missing;
                                          prints the login URL either way.
  fyers_daily_auth.py url              -> print a fresh login URL + instructions.
  fyers_daily_auth.py apply <code>     -> exchange an auth_code via appIdHash OAuth and save token.json
                                          (persists access_token + refresh_token + date).
  fyers_daily_auth.py refresh          -> renew access_token from the stored refresh_token via
                                          validate-refresh-token (NO browser, NO TOTP). Requires
                                          FYERS_PIN in .env. Falls back to 'url' if no refresh_token.

Used by: the daily Telegram cron reminder, the dashboard, and direct chat.
Never prints secrets; writes token.json with 0600.
"""
from __future__ import annotations
import sys, os, json, time, hashlib, ssl, urllib.request, urllib.parse, urllib.error, datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(ROOT, ".env")
TOKEN = os.path.join(ROOT, "token.json")
REDIRECT = "https://trade.fyers.in/api-login/default.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def load_env(p=ENV):
    env = {}
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def token_data():
    try:
        return json.load(open(TOKEN))
    except Exception:
        return {}


def token_date():
    return token_data().get("date")


def today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def login_url():
    cfg = load_env()
    cid = cfg.get("FYERS_APP_ID", "")
    redirect = cfg.get("FYERS_REDIRECT_URI", REDIRECT)
    p = urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect,
        "response_type": "code", "state": "jarvis_login",
    })
    return f"https://api-t1.fyers.in/api/v3/generate-authcode?{p}"


def fresh():
    return token_date() == today()


def _save_token(access_token, refresh_token=None, keep_existing_refresh=True):
    """Write token.json 0600. Preserves an existing refresh_token unless a new one is supplied."""
    data = {"access_token": access_token, "date": today()}
    if refresh_token:
        data["refresh_token"] = refresh_token
    elif keep_existing_refresh:
        existing = token_data().get("refresh_token")
        if existing:
            data["refresh_token"] = existing
    with open(TOKEN, "w") as f:
        json.dump(data, f)
    os.chmod(TOKEN, 0o600)


def apply_auth_code(code):
    cfg = load_env()
    cid = cfg["FYERS_APP_ID"]; sec = cfg["FYERS_SECRET"]
    app_id_hash = hashlib.sha256(f"{cid}:{sec}".encode()).hexdigest()
    payload = {"grant_type": "authorization_code", "appIdHash": app_id_hash, "code": code.strip()}
    req = urllib.request.Request(
        "https://api-t1.fyers.in/api/v3/validate-authcode",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20)
        data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return False, f"exchange rejected (HTTP {e.code}) — code already used, expired, or malformed. Get a fresh auth_code and retry."
    except Exception as e:
        return False, f"exchange error: {e}"
    if data.get("s") != "ok":
        return False, data.get("message", "exchange failed")
    tok = data.get("access_token")
    if not tok:
        return False, "no access_token in response"
    _save_token(tok, refresh_token=data.get("refresh_token"))
    return True, f"len={len(tok)} refresh={'yes' if data.get('refresh_token') else 'none'}"


def refresh_token():
    """Renew access_token from stored refresh_token. NO browser. Requires FYERS_PIN in .env."""
    cfg = load_env()
    cid = cfg.get("FYERS_APP_ID"); sec = cfg.get("FYERS_SECRET"); pin = cfg.get("FYERS_PIN")
    if not (cid and sec):
        return False, "FYERS_APP_ID / FYERS_SECRET missing from .env"
    rt = token_data().get("refresh_token")
    if not rt:
        return False, "no refresh_token stored — run a manual 'apply <auth_code>' once to seed it"
    if not pin:
        return False, "FYERS_PIN missing from .env (required by validate-refresh-token)"
    app_id_hash = hashlib.sha256(f"{cid}:{sec}".encode()).hexdigest()
    payload = {"grant_type": "refresh_token", "appIdHash": app_id_hash,
               "refresh_token": rt, "pin": pin}
    req = urllib.request.Request(
        "https://api-t1.fyers.in/api/v3/validate-refresh-token",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA},
        method="POST")
    try:
        resp = urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20)
        data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        return False, f"refresh rejected (HTTP {e.code}) {body}"
    except Exception as e:
        return False, f"refresh error: {e}"
    if data.get("s") != "ok":
        return False, data.get("message", "refresh failed")
    tok = data.get("access_token")
    if not tok:
        return False, "no access_token in refresh response"
    # Fyers may or may not return a fresh refresh_token; keep the existing one if not.
    _save_token(tok, refresh_token=data.get("refresh_token"))
    return True, f"len={len(tok)}"


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    mode = sys.argv[1]
    if mode == "url":
        print(login_url())
    elif mode == "check":
        print("FRESH" if fresh() else "STALE")
        print(login_url())
        sys.exit(0 if fresh() else 1)
    elif mode == "refresh":
        ok, info = refresh_token()
        print(("REFRESHED token_len=%s" % info) if ok else ("FAILED: %s" % info))
        sys.exit(0 if ok else 1)
    elif mode == "apply":
        if len(sys.argv) < 3:
            print("usage: fyers_daily_auth.py apply <auth_code>"); sys.exit(2)
        ok, info = apply_auth_code(sys.argv[2])
        print(("APPLIED token_len=%s" % info) if ok else ("FAILED: %s" % info))
        sys.exit(0 if ok else 1)
    else:
        print("unknown mode", mode); sys.exit(2)


if __name__ == "__main__":
    main()