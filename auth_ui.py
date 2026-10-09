#!/usr/bin/env python3
"""Fyers daily-token collection + bot restart. NO LLM.

Two entry points, both zero-token:
  apply_from_text(text)  -> pull auth_code out of a pasted redirect URL (or a raw JWT), exchange it
                            for today's access token, then restart the live bot so it picks it up.
  status()               -> token date + FRESH/STALE + whether the bot is running.

Safety rules baked in:
  * the auth_code is NEVER printed, logged, or returned in the result payload;
  * the exchange is refused when today's token is already fresh (single-use semantics);
  * the bot is stopped BY PID discovered from /proc -- never `pkill -f`, which self-matches and has
    already killed the calling shell once on this box;
  * the token file is written 0600 by fyers_daily_auth.apply_auth_code().
"""
import json
import os
import re
import signal
import subprocess
import sys
import time

BOT = "/opt/jarvis-dsh/scratch/fyers-cup-bot-forked"
PY = f"{BOT}/.venv/bin/python"
AUTH = os.path.join(BOT, "fyers_daily_auth.py")
TOKEN = os.path.join(BOT, "token.json")
LOG = f"/tmp/fyers_run_{time.strftime('%Y%m%d')}.log"

sys.path.insert(0, BOT)


def _today():
    return time.strftime("%Y-%m-%d")


def status():
    """Read-only token + process status. Never exposes the token itself."""
    fresh, date = False, None
    try:
        d = json.load(open(TOKEN))
        date = d.get("date")
        fresh = (date == _today()) and bool(d.get("access_token"))
    except Exception:
        pass
    return {"token_date": date, "fresh": fresh, "today": _today(),
            "bot_running": bool(bot_pids()), "bot_pids": bot_pids()}


def extract_code(text):
    """Accept a full redirect URL or a bare JWT. Returns the auth_code or ''."""
    if not text:
        return ""
    t = text.strip()
    m = re.search(r"[?&]auth_code=([^&\s]+)", t)
    if m:
        return m.group(1).strip()
    # a bare JWT (three dot-separated base64url segments)
    if t.count(".") == 2 and not t.startswith("http"):
        return t
    return ""


def bot_pids():
    """PIDs of the live runner only (the python process executing run.py).

    Deliberately strict: an earlier version matched any /proc cmdline containing the strings
    'fyers-cup-bot-forked' and 'run.py', which ALSO matches a bash shell whose command TEXT
    contains them (e.g. `bash -c 'cd .../fyers-cup-bot-forked && python run.py'`). That produced
    false bot PIDs, so the restart logic killed the wrong things and the real runner kept the
    run.lock -- the bot then refused to start and stayed on the previous day's dead token.
    Here we require: the executable is a python interpreter AND some argv element ENDS WITH
    'run.py', and no element contains 'dashboard.py'.
    """
    pids = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{p}/exe")
        except Exception:
            continue
        if "python" not in os.path.basename(exe):
            continue          # excludes bash/sh wrappers entirely
        try:
            argv = open(f"/proc/{p}/cmdline", "rb").read().decode("utf-8", "replace").split("\0")
        except Exception:
            continue
        argv = [a for a in argv if a]
        if any(a.endswith("run.py") for a in argv) and not any("dashboard.py" in a for a in argv):
            pids.append(int(p))
    return pids


def clear_stale_lock(path=None):
    """Remove run.lock ONLY when its recorded PID is dead AND no live process holds it.
    The bot refuses to start while run.lock exists, so a lock left by a killed runner would
    otherwise strand it on the previous day's dead token. Never touch a live lock."""
    path = path or os.path.join(BOT, "run.lock")
    try:
        pid = open(path).read().strip()
    except Exception:
        return False, "no lock file"
    if pid and os.path.isdir(f"/proc/{pid}"):
        return False, f"lock held by LIVE pid {pid}"
    try:
        import fcntl
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
    except BlockingIOError:
        return False, "lock still flock-held by a live process"
    except Exception:
        pass
    try:
        os.remove(path)
        return True, f"removed stale lock (dead pid {pid or 'unknown'})"
    except Exception as e:
        return False, f"could not remove: {e}"


def restart_bot():
    """Stop the runner by PID, relaunch it detached, and wait for the funds preflight line.
    Returns (ok, message). Deliberately never uses pkill -f."""
    pids = bot_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    # wait for them to actually die before relaunching (avoid port/state races)
    for _ in range(20):
        if not bot_pids():
            break
        time.sleep(0.5)

    lock_ok, lock_msg = clear_stale_lock()

    launcher = (
        f"cd {BOT} && setsid nohup {PY} run.py >> {LOG} 2>&1 < /dev/null &"
    )
    subprocess.Popen(["bash", "-c", launcher], cwd=BOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # verify: funds preflight ok within ~20s
    for _ in range(20):
        time.sleep(1)
        try:
            tail = subprocess.run(["tail", "-40", LOG], capture_output=True, text=True).stdout
        except Exception:
            tail = ""
        if "funds preflight ok" in tail:
            bal = ""
            for ln in tail.splitlines():
                if "funds preflight ok" in ln:
                    bal = ln.strip()
            return True, bal
    return bool(bot_pids()), f"relaunched ({lock_msg}); funds preflight not yet seen in the log"


def apply_from_text(text):
    """Extract -> refuse-if-fresh -> exchange -> restart. Returns a JSON-safe result dict."""
    code = extract_code(text)
    if not code:
        return {"ok": False, "stage": "extract",
                "error": "no auth_code found in the pasted text (paste the full redirect URL)"}
    st = status()
    if st["fresh"]:
        return {"ok": False, "stage": "guard",
                "error": f"today's token is already fresh ({st['token_date']}) — exchange refused"}

    import fyers_daily_auth as FDA
    try:
        ok, info = FDA.apply_auth_code(code)
    except Exception as e:
        return {"ok": False, "stage": "exchange", "error": f"{e.__class__.__name__}: {str(e)[:200]}"}
    if not ok:
        return {"ok": False, "stage": "exchange", "error": str(info)[:200]}

    tlen = info if isinstance(info, int) else len(str(info))
    r_ok, r_msg = restart_bot()
    new = status()
    return {"ok": True, "stage": "done", "token_len": tlen,
            "token_date": new["token_date"], "bot_restarted": r_ok, "bot_detail": r_msg,
            "fresh": new["fresh"]}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(status(), indent=1))
    else:
        print(json.dumps(status(), indent=1))
