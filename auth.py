"""
Fyers login -> saves access token (+ the date it was issued) to token.json.

Fyers access tokens expire daily, so this needs to run once each trading
morning before run.py. Two ways to get a token:

  1. MANUAL (default, always available): `python auth.py` prints a login URL,
     you log in in your browser with your own Fyers credentials, and paste
     back the `auth_code` from the redirect URL. Nothing here ever sees your
     Fyers password.

  2. TOTP AUTO-LOGIN (opt-in, only if FYERS_ID / FYERS_TOTP_SECRET / FYERS_PIN
     are set in .env): drives the same login Fyers's own web app performs,
     using your TOTP secret to generate the 2FA code instead of you typing it
     in every morning. See the big warning below before turning this on.

=== IMPORTANT: read before enabling TOTP auto-login ===
This is NOT a documented, supported Fyers API. It is a reverse-engineered
replay of the internal endpoints Fyers's own login page calls (send_login_otp_v2
-> verify_otp -> verify_pin_v2 -> token), sourced from the community reference
implementation at github.com/tkanhe/fyers-api-access-token-v3. Fyers can change
these endpoints, payload shapes, or block this pattern entirely at any time,
without notice, since it's their internal web-login plumbing rather than a
published API contract. It ships here because it's genuinely useful (no more
opening a browser every trading morning), but:
  - Use only YOUR OWN Fyers credentials. This is not a multi-account tool.
  - FYERS_TOTP_SECRET and FYERS_PIN are as sensitive as a password — they live
    only in .env (gitignored, never commit it) and nowhere else.
  - If Fyers changes the flow, this fails LOUDLY (see below) and falls back
    to the manual browser flow — it never silently does nothing.
  - Treat this as a convenience with a known, non-zero maintenance burden, not
    a "fire and forget" feature. run.py's own token-freshness check runs this
    automatically ONLY when the TOTP env vars are present; if you never set
    them, nothing about this file's normal behavior changes.
"""
import base64
import hashlib
import hmac
import json
import struct
import time
from datetime import datetime

import requests
from fyers_apiv3 import fyersModel
from fyers_client import load_env

TOTP_BASE_URL = "https://api-t2.fyers.in/vagator/v2"
TOKEN_URL = "https://api.fyers.in/api/v2/token"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class TotpLoginError(RuntimeError):
    """Raised when any step of the reverse-engineered TOTP login flow fails
    (bad credentials, bad TOTP/PIN, or Fyers having changed the endpoints).
    Callers must NOT treat this as "no token available and silently proceed"
    — an algo bot running on a stale/missing token is worse than one that
    stops and tells you to log in by hand."""


def _totp_now(secret_b32: str) -> str:
    """RFC 6238 TOTP, 30s step, 6 digits, HMAC-SHA1 — done by hand (no pyotp
    dependency) so there's nothing here you can't read top to bottom."""
    key = base64.b32decode(secret_b32.strip().upper().replace(" ", ""), casefold=True)
    counter = int(time.time()) // 30
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _post(url, payload, headers=None, what="request", as_form=True):
    """POST to Fyers, matching the content-type the endpoint actually expects.

    The three `vagator/v2` OTP/login calls accept a **form-encoded** body
    (the reference `tkanhe/fyers-api-access-token-v3` sends them as `data=`,
    NOT `json=`); forcing Content-Type: application/json makes Fyers reject
    `send_login_otp_v2` with `-1025 invalid request`. The final `token` call
    wants a raw JSON *string* body with `Content-Type: application/json; charset=UTF-8`.

    - as_form=True  -> body is a JSON string sent as `data=` (vagator calls)
    - as_form=False -> for anything that needs an explicit JSON header (unused
                       by the default flow; kept for completeness)
    """
    h = {"User-Agent": _UA}
    h.update(headers or {})
    body = json.dumps(payload) if as_form else None
    try:
        r = requests.post(url, data=body, headers=h, timeout=15)
        data = r.json()
    except Exception as e:
        raise TotpLoginError(f"{what}: request failed ({e}) — Fyers may have "
                              f"changed this endpoint; use the manual login instead") from e
    if not r.ok or str(data.get("s", "")).lower() != "ok":
        raise TotpLoginError(f"{what}: Fyers rejected the request — {data}")
    return data


def totp_login(cfg) -> str:
    """Drives the 4-request Fyers web-login flow with a TOTP code generated
    from FYERS_TOTP_SECRET instead of a human typing one in. Returns an
    auth_code, ready for SessionModel.set_token(). Raises TotpLoginError on
    any step that doesn't come back exactly as expected — see the module
    docstring for why that's the right failure mode here."""
    fyers_id = cfg.get("FYERS_ID", "").strip()
    totp_secret = cfg.get("FYERS_TOTP_SECRET", "").strip()
    pin = cfg.get("FYERS_PIN", "").strip()
    app_id = cfg.get("FYERS_APP_ID", "").strip()
    redirect_uri = cfg.get("FYERS_REDIRECT_URI", "http://127.0.0.1:5000/fyers/callback")
    if not (fyers_id and totp_secret and pin and app_id):
        raise TotpLoginError("FYERS_ID / FYERS_TOTP_SECRET / FYERS_PIN / FYERS_APP_ID "
                              "must all be set in .env for TOTP auto-login")

    # 1) send_login_otp_v2 -> a request_key, keyed off the (obfuscated) client id
    fy_id_b64 = base64.b64encode(fyers_id.encode()).decode()
    r1 = _post(f"{TOTP_BASE_URL}/send_login_otp_v2",
               {"fy_id": fy_id_b64, "app_id": "2"}, what="send_login_otp_v2")
    request_key = r1["request_key"]

    # 2) verify_otp -> the TOTP code, generated fresh right before sending so
    # it can't have rolled over the 30s window in transit
    code = _totp_now(totp_secret)
    r2 = _post(f"{TOTP_BASE_URL}/verify_otp",
               {"request_key": request_key, "otp": code}, what="verify_otp")
    request_key_2 = r2["request_key"]

    # 3) verify_pin_v2 -> a bearer access_token for the NEXT (authorization) call
    pin_b64 = base64.b64encode(pin.encode()).decode()
    r3 = _post(f"{TOTP_BASE_URL}/verify_pin_v2",
               {"request_key": request_key_2, "identity_type": "pin", "identifier": pin_b64},
               what="verify_pin_v2")
    bearer = r3["data"]["access_token"]

    # 4) the authorization redirect itself — same call the browser makes when
    # you click "Allow" on the consent screen. The response's Url carries
    # auth_code as a query param, not a real redirect we follow.
    app_id_short = app_id[:-4] if len(app_id) > 4 else app_id   # "XXXX-100" -> "XXXX"
    r4 = _post(TOKEN_URL,
               {"fyers_id": fyers_id, "app_id": app_id_short, "redirect_uri": redirect_uri,
                "appType": "100", "code_challenge": "", "state": "abcdefg", "scope": "",
                "nonce": "", "response_type": "code", "create_cookie": True},
               headers={"Authorization": f"Bearer {bearer}",
                        "Content-Type": "application/json; charset=UTF-8"},
               what="token")
    redirect_url = r4.get("Url") or r4.get("data", {}).get("Url", "")
    if "auth_code=" not in redirect_url:
        raise TotpLoginError(f"token step didn't return an auth_code — {r4}")
    auth_code = redirect_url.split("auth_code=", 1)[1].split("&", 1)[0]
    return auth_code


def _save_token(token: str):
    with open("token.json", "w") as f:
        json.dump({"access_token": token, "date": datetime.now().strftime("%Y-%m-%d")}, f)


def get_token_totp(cfg=None) -> str:
    """Full TOTP auto-login -> saved token.json. Raises TotpLoginError (never
    returns a falsy token) so a caller like run.py can decide to fall back to
    the manual flow or stop and alert, rather than silently limping on."""
    cfg = cfg or load_env()
    app_id, secret = cfg.get("FYERS_APP_ID"), cfg.get("FYERS_SECRET")
    redirect = cfg.get("FYERS_REDIRECT_URI", "http://127.0.0.1:5000/fyers/callback")
    auth_code = totp_login(cfg)
    session = fyersModel.SessionModel(client_id=app_id, secret_key=secret,
                                       redirect_uri=redirect, response_type="code",
                                       grant_type="authorization_code")
    session.set_token(auth_code)
    resp = session.generate_token()
    token = resp.get("access_token")
    if not token:
        raise TotpLoginError(f"SessionModel.generate_token() returned no access_token: {resp}")
    _save_token(token)
    return token


def manual_login():
    cfg = load_env()
    app_id = cfg.get("FYERS_APP_ID")
    secret = cfg.get("FYERS_SECRET")
    redirect = cfg.get("FYERS_REDIRECT_URI", "http://127.0.0.1:5000/fyers/callback")
    if not app_id or not secret:
        print("ERROR: set FYERS_APP_ID and FYERS_SECRET in .env first.")
        return

    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret,
        redirect_uri=redirect,
        response_type="code",
        grant_type="authorization_code",
    )

    print("\n1) Open this URL and log into Fyers:\n")
    print("   " + session.generate_authcode() + "\n")
    print("2) After login you'll be redirected to a URL with 'auth_code=...'")
    auth_code = input("3) Paste the auth_code value here: ").strip()

    session.set_token(auth_code)
    resp = session.generate_token()
    token = resp.get("access_token")
    if not token:
        print("Failed to get token. Response:", resp)
        return
    _save_token(token)
    print("\n✓ Saved access token to token.json. You can now run:  python demo.py")


def main():
    cfg = load_env()
    have_totp = all(cfg.get(k) for k in ("FYERS_ID", "FYERS_TOTP_SECRET", "FYERS_PIN"))
    if have_totp:
        print("FYERS_ID / FYERS_TOTP_SECRET / FYERS_PIN are set — trying TOTP auto-login...")
        try:
            get_token_totp(cfg)
            print("\n✓ Logged in via TOTP and saved token.json. You can now run:  python run.py")
            return
        except TotpLoginError as e:
            print(f"TOTP auto-login failed ({e}).\nFalling back to the manual browser login.\n")
    manual_login()


if __name__ == "__main__":
    main()
