#!/usr/bin/env python3
"""
watchlist_sync.py — Safely sync qualifying candidates from nse_screener_candidates.csv
into watchlist.csv with deduplication, size cap, capital budget guard, backup,
atomic rewrite, and self-validation via run.load_watchlist().

CLI:
    python watchlist_sync.py [--dry-run] [--max-watchlist N]
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

MAX_WATCHLIST = 8
DEFAULT_CAPITAL_BUDGET = 50000.0
WATCHLIST_FIELDS = ("symbol", "rim", "trigger", "qty")


def _load_env_file(path: str) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def resolve_capital_budget(
    capital_budget: float | int | None = None,
    env_path: str | None = None,
) -> float:
    """Resolve CAPITAL_BUDGET from explicit arg, .env file, or environment (default 50000)."""
    if capital_budget is not None:
        return float(capital_budget)

    if env_path is not None:
        cfg = _load_env_file(env_path)
        raw = cfg.get("CAPITAL_BUDGET", "").strip()
        if raw:
            return float(raw)

    env_val = os.getenv("CAPITAL_BUDGET", "").strip()
    if env_val:
        return float(env_val)

    for candidate_env in (".env", os.path.join(HERE, ".env")):
        if os.path.exists(candidate_env):
            cfg = _load_env_file(candidate_env)
            raw = cfg.get("CAPITAL_BUDGET", "").strip()
            if raw:
                return float(raw)

    return float(DEFAULT_CAPITAL_BUDGET)


def _resolve_default_path(filename: str) -> str:
    if os.path.exists(filename):
        return filename
    return os.path.join(HERE, filename)


def _read_existing_watchlist(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Watchlist file not found: {path}")
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not set(WATCHLIST_FIELDS).issubset(set(reader.fieldnames)):
            raise ValueError(
                f"Invalid watchlist header in {path}: {reader.fieldnames!r} "
                f"(expected {list(WATCHLIST_FIELDS)!r})"
            )
        for idx, r in enumerate(reader, start=2):
            if not r or all((v is None or str(v).strip() == "") for v in r.values()):
                continue
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                raise ValueError(f"Missing symbol on line {idx} of {path}")
            rim = float(r["rim"])
            trigger = float(r["trigger"])
            qty = int(float(r["qty"]))
            rows.append({"symbol": sym, "rim": rim, "trigger": trigger, "qty": qty})
    return rows


def _read_screener_candidates(path: str) -> list[dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Candidates file not found: {path}")
    cands: list[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return cands
        for idx, r in enumerate(reader, start=2):
            if not r or all((v is None or str(v).strip() == "") for v in r.values()):
                continue
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                raise ValueError(f"Missing symbol on line {idx} of {path}")
            rim = float(r["ath"])
            trigger = float(r["buy_price"])
            qty = int(float(r["qty"]))
            cands.append({"symbol": sym, "rim": rim, "trigger": trigger, "qty": qty})
    return cands


def _build_watchlist_csv(rows: list[dict]) -> str:
    """Build full watchlist.csv content with header + every row newline-terminated."""
    lines = ["symbol,rim,trigger,qty\n"]
    for r in rows:
        lines.append(f"{r['symbol']},{r['rim']},{r['trigger']},{r['qty']}\n")
    return "".join(lines)


def _validate_with_run_load_watchlist(watchlist_path: str) -> list[dict]:
    """Import run and call run.load_watchlist(watchlist_path) to self-validate."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    # Guard optional third-party imports in case sync is invoked outside .venv
    for mod_name in ("fyers_apiv3", "requests"):
        if mod_name not in sys.modules:
            try:
                __import__(mod_name)
            except ImportError:
                stub = types.ModuleType(mod_name)
                if mod_name == "fyers_apiv3":
                    stub.fyersModel = types.SimpleNamespace(FyersModel=object)
                sys.modules[mod_name] = stub
    import run
    return run.load_watchlist(watchlist_path)


def sync_watchlist(
    watchlist_path: str | None = None,
    candidates_path: str | None = None,
    dry_run: bool = False,
    max_watchlist: int = MAX_WATCHLIST,
    capital_budget: float | int | None = None,
    env_path: str | None = None,
) -> dict:
    """Sync qualifying candidates from nse_screener_candidates.csv into watchlist.csv.

    Returns a summary dict with keys:
      added, skipped, final, total_notional, capital_budget, max_watchlist,
      dry_run, backup_path
    Raises an exception (after restoring backup if written) on failure.
    """
    if max_watchlist <= 0:
        raise ValueError(f"max_watchlist must be >= 1, got {max_watchlist}")

    wl_path = watchlist_path or _resolve_default_path("watchlist.csv")
    cand_path = candidates_path or _resolve_default_path("nse_screener_candidates.csv")
    budget = resolve_capital_budget(capital_budget=capital_budget, env_path=env_path)

    existing_rows = _read_existing_watchlist(wl_path)
    candidates = _read_screener_candidates(cand_path)

    final_rows = [dict(r) for r in existing_rows]
    seen_symbols = {r["symbol"].upper() for r in final_rows}

    added: list[dict] = []
    skipped: list[dict] = []

    for cand in candidates:
        sym = cand["symbol"].upper()
        rim = cand["rim"]
        trigger = cand["trigger"]
        qty = cand["qty"]
        cand_notional = trigger * qty

        # Rule 1: DEDUPE (case-insensitive)
        if sym in seen_symbols:
            skipped.append({
                "symbol": sym,
                "reason": "already in watchlist (dedupe)",
            })
            continue

        # Rule 2: CAP (MAX_WATCHLIST)
        if len(final_rows) >= max_watchlist:
            skipped.append({
                "symbol": sym,
                "reason": f"max_watchlist cap reached ({len(final_rows)}/{max_watchlist})",
            })
            continue

        # Rule 3: BUDGET GUARD
        current_notional = sum(r["trigger"] * r["qty"] for r in final_rows)
        projected_notional = current_notional + cand_notional
        if round(projected_notional, 2) > round(budget, 2):
            skipped.append({
                "symbol": sym,
                "reason": (
                    f"budget exceeded: current {current_notional:.2f} + "
                    f"new {cand_notional:.2f} ({trigger} * {qty}) = "
                    f"{projected_notional:.2f} > CAPITAL_BUDGET {budget:.2f}"
                ),
            })
            continue

        entry = {"symbol": sym, "rim": rim, "trigger": trigger, "qty": qty}
        final_rows.append(entry)
        added.append(entry)
        seen_symbols.add(sym)

    backup_path = None
    if not dry_run:
        # Rule 5: BACKUP before replacing
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"{wl_path}.bak-{ts}"
        shutil.copy2(wl_path, backup_path)

        # Rule 4: ATOMIC REWRITE (temp file in same directory -> os.replace)
        new_content = _build_watchlist_csv(final_rows)
        wl_dir = os.path.dirname(os.path.abspath(wl_path)) or "."
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=wl_dir,
                prefix=".watchlist_sync_",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
                newline="\n",
            ) as tf:
                tmp_path = tf.name
                tf.write(new_content)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, wl_path)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        # Rule 6: SELF-VALIDATE with run.load_watchlist()
        try:
            _validate_with_run_load_watchlist(wl_path)
        except Exception as exc:
            if backup_path and os.path.exists(backup_path):
                shutil.copy2(backup_path, wl_path)
            msg = (
                f"SELF-VALIDATION FAILED: run.load_watchlist({wl_path!r}) raised "
                f"{type(exc).__name__}: {exc}. Restored backup from {backup_path}."
            )
            print(msg, file=sys.stderr)
            raise RuntimeError(msg) from exc

    total_notional = sum(r["trigger"] * r["qty"] for r in final_rows)
    result = {
        "added": added,
        "skipped": skipped,
        "final": final_rows,
        "total_notional": round(total_notional, 2),
        "capital_budget": round(budget, 2),
        "max_watchlist": max_watchlist,
        "dry_run": dry_run,
        "backup_path": backup_path,
    }
    _print_summary(result)
    return result


sync = sync_watchlist


def _print_summary(res: dict) -> None:
    mode_str = "DRY-RUN (no files written)" if res["dry_run"] else "SYNC COMPLETE"
    print(
        f"=== Watchlist Sync [{mode_str}] | "
        f"max_watchlist={res['max_watchlist']} | "
        f"CAPITAL_BUDGET=Rs {res['capital_budget']:.2f} ==="
    )
    if res.get("backup_path"):
        print(f"Backup: {res['backup_path']}")

    print(f"\nAdded ({len(res['added'])}):")
    if res["added"]:
        for r in res["added"]:
            notional = r["trigger"] * r["qty"]
            print(
                f"  + {r['symbol']:<12} rim={r['rim']} trigger={r['trigger']} "
                f"qty={r['qty']} notional=Rs {notional:.2f}"
            )
    else:
        print("  (none)")

    print(f"\nSkipped ({len(res['skipped'])}):")
    if res["skipped"]:
        for s in res["skipped"]:
            print(f"  - {s['symbol']:<12} {s['reason']}")
    else:
        print("  (none)")

    print(f"\nFinal watchlist ({len(res['final'])}/{res['max_watchlist']}):")
    for r in res["final"]:
        notional = r["trigger"] * r["qty"]
        print(
            f"  * {r['symbol']:<12} rim={r['rim']} trigger={r['trigger']} "
            f"qty={r['qty']} notional=Rs {notional:.2f}"
        )
    print(
        f"Total notional: Rs {res['total_notional']:.2f} / Rs {res['capital_budget']:.2f} "
        f"(remaining: Rs {res['capital_budget'] - res['total_notional']:.2f})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync qualifying candidates from nse_screener_candidates.csv into watchlist.csv."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change and write nothing.",
    )
    parser.add_argument(
        "--max-watchlist",
        type=int,
        default=MAX_WATCHLIST,
        help=f"Maximum entries allowed in watchlist.csv (default: {MAX_WATCHLIST}).",
    )
    parser.add_argument(
        "--watchlist",
        default=None,
        help="Path to watchlist.csv (default: watchlist.csv in repo).",
    )
    parser.add_argument(
        "--candidates",
        default=None,
        help="Path to nse_screener_candidates.csv (default: nse_screener_candidates.csv in repo).",
    )
    parser.add_argument(
        "--env",
        default=None,
        help="Path to .env file for reading CAPITAL_BUDGET.",
    )
    parser.add_argument(
        "--capital-budget",
        type=float,
        default=None,
        help=f"Override CAPITAL_BUDGET (default: from .env or {DEFAULT_CAPITAL_BUDGET:.0f}).",
    )
    args = parser.parse_args(argv)

    try:
        sync_watchlist(
            watchlist_path=args.watchlist,
            candidates_path=args.candidates,
            dry_run=args.dry_run,
            max_watchlist=args.max_watchlist,
            capital_budget=args.capital_budget,
            env_path=args.env,
        )
        return 0
    except Exception as exc:
        print(f"ERROR: watchlist_sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
