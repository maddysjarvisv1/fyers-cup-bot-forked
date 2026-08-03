"""
POC demo — proves the lean DIY Fyers bot works. READ-ONLY (no orders placed).

Run after `python auth.py` has saved token.json:
    python demo.py

Shows: your profile, funds, and LIVE quotes for a few watchlist stocks —
proving we can pull real Fyers data with ~20 packages and no OpenAlgo.
"""
from fyers_client import FyersClient


def main():
    c = FyersClient.from_files()

    print("=== PROFILE ===")
    p = c.profile()
    print("  status:", p.get("s"), "| name:", (p.get("data") or {}).get("name"))

    print("\n=== FUNDS ===")
    f = c.funds()
    if f.get("s") == "ok":
        for row in (f.get("fund_limit") or [])[:3]:
            print(f"  {row.get('title')}: {row.get('equityAmount')}")
    else:
        print("  ", f)

    print("\n=== LIVE QUOTES (real Fyers LTP) ===")
    watch = ["SOTL", "APOLLOHOSP", "LAURUSLABS", "TMB", "SUNPHARMA"]
    ltps = c.ltps(watch)
    if ltps:
        for sym, ltp in ltps.items():
            print(f"  {sym:12} LTP {ltp}")
    else:
        print("  no quotes returned — check the raw response:")
        print("  ", c.quotes(watch))

    print("\nDIY Fyers connection works. ✓  (no OpenAlgo, ~20 packages)")


if __name__ == "__main__":
    main()
