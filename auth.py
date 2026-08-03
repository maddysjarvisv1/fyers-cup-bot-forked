"""
One-time-per-day Fyers login -> saves access token to token.json.

Fyers access tokens expire daily, so run this each trading morning.
YOU do the login (your Fyers credentials). This script never sees your password.

Steps:
  1. python auth.py
  2. It prints a login URL — open it, log into Fyers.
  3. You get redirected to a URL containing '...auth_code=XXXX...'
     (the page may show an OpenAlgo/blank page — that's fine, the code is in the address bar)
  4. Paste the auth_code here. Token is saved to token.json.
"""
import json
from fyers_apiv3 import fyersModel
from fyers_client import load_env


def main():
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
    with open("token.json", "w") as f:
        json.dump({"access_token": token}, f)
    print("\n✓ Saved access token to token.json. You can now run:  python demo.py")


if __name__ == "__main__":
    main()
