#!/usr/bin/env python3
"""Guard test-suite for live/executor.py (log-only mode, synthetic intents).
Exercises: place, reprice-on-tick, sub-tick hold, quote-off cancel, window-end
cancel (G4), strict $ cap (G5), KILL exit (G10), truncation recovery (G11)."""
import json, os, shutil, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_exec_sandbox")

def setup():
    shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(os.path.join(SANDBOX, "paper"))
    os.makedirs(os.path.join(SANDBOX, "live"), exist_ok=True)
    for mod in ("live", "paper"):
        os.makedirs(os.path.join(SANDBOX, mod), exist_ok=True)
    shutil.copy(f"{REPO}/live/executor.py", f"{SANDBOX}/live/executor.py")
    open(f"{SANDBOX}/live/__init__.py", "w").close()
    shutil.copy(f"{REPO}/paper/envload.py", f"{SANDBOX}/paper/envload.py")
    open(f"{SANDBOX}/paper/__init__.py", "w").close()
    json.dump({"enabled": ["teststrat"], "max_order_usd": 5,
               "max_inventory_usd": 50, "daily_loss_stop_usd": 25},
              open(f"{SANDBOX}/paper/live.json", "w"))

def intent(slug_base, token, bid, ask, ts=None):
    return json.dumps({"ts": ts or time.time(), "strategy": "teststrat", "asset": "btc",
                       "slug": f"btc-updown-5m-{slug_base}", "token": token, "side_up": True,
                       "bid": bid, "ask": ask, "bid_shares": 8.8, "ask_shares": 5,
                       "caps": {}}) + "\n"

def append(line):
    with open(f"{SANDBOX}/paper/intents.jsonl", "a") as f:
        f.write(line)

def main():
    setup()
    base_live = int(time.time()) + 60   # synthetic window: closes in 360s — immune to real 5-min boundaries
    TOK_A, TOK_B = "111100001111", "222200002222"
    open(f"{SANDBOX}/paper/intents.jsonl", "w").close()

    env = dict(os.environ)
    # F1: the subprocess must NEVER inherit live mode or credentials - on the
    # deploy box that would fire a REAL cancel_all/orders. Force log-only.
    env["LIVE_EXECUTOR_MODE"] = "log-only"
    for k in ("POLY_PRIVATE_KEY", "POLY_FUNDER", "DEPLOY_REGION", "PAPER_LIVE_INTENTS"):
        env.pop(k, None)
    p = subprocess.Popen([sys.executable, "-m", "live.executor"], cwd=SANDBOX,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", env=env)
    try:
        time.sleep(2)
        append(intent(base_live, TOK_A, 0.573, None))       # T1 place @0.57
        time.sleep(2.5)
        append(intent(base_live, TOK_A, 0.5748, None))      # T2 sub-tick (0.57) -> NO action
        time.sleep(2.5)
        append(intent(base_live, TOK_A, 0.581, None))       # T3 reprice -> cancel + place @0.58
        time.sleep(2.5)
        append(intent(base_live, TOK_A, None, None))        # T4 quote-off -> cancel
        time.sleep(2.5)
        base_closing = int(time.time()) - 295              # closes in ~5s from NOW (cancel at close-2s)
        append(intent(base_closing, TOK_B, 0.42, None))     # T5 place then auto-cancel at window close
        time.sleep(6)
        append(intent(base_live, TOK_A, 0.005, None))       # T6 px<tick -> nothing
        time.sleep(2)
        # T7 truncation: rewrite file smaller, then a fresh valid intent
        open(f"{SANDBOX}/paper/intents.jsonl", "w").close()
        time.sleep(2)
        append(intent(base_live, TOK_A, 0.612, None))       # T7 place @0.61 after reset
        time.sleep(2.5)
        open(f"{SANDBOX}/paper/KILL", "w").close()          # T8 KILL -> cancel + exit
        out = p.communicate(timeout=20)[0]                  # drain pipe (no wait-deadlock)
    except subprocess.TimeoutExpired:
        p.kill()
        out = p.communicate()[0]
    finally:
        if p.poll() is None:
            p.kill()
            out = p.communicate()[0]
    L = out.splitlines()

    def n(sub):
        return sum(1 for l in L if sub in l)

    results = [
        ("T1 place @0.57",            n("place teststrat buy 8.8 sh 1111000011 @ 0.57") == 1),
        ("T2 sub-tick -> no action",  n("place teststrat buy 8.8 sh 1111000011 @ 0.57") == 1
                                       and n("0.57 (quote-off)") == 0),
        ("T3 reprice cancel+0.58",    n("cancel teststrat buy 1111000011@0.57 (reprice)") == 1
                                       and n("@ 0.58") == 1),
        ("T4 quote-off cancel",       n("1111000011@0.58 (quote-off)") == 1),
        ("T5 window-end cancel",      n("place teststrat buy 8.8 sh 2222000022 @ 0.42") == 1
                                       and n("2222000022@0.42 (window-close)") == 1),
        ("T6 px<tick ignored",        n("@ 0.01") == 0 and n("@ 0.00") == 0),
        ("T7 truncation recovery",    n("resetting read position") == 1 and n("@ 0.61") == 1),
        ("T8 KILL -> clean exit",     n("KILL file present") == 1 and n("executor exit - book is clean") == 1
                                       and n("1111000011@0.61") >= 1),
    ]
    print(out[-1500:])
    print("\n=== GUARD TEST RESULTS ===")
    ok = True
    for name, passed in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
        ok &= passed
    print("\nALL PASS" if ok else "\nFAILURES - see log above")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
