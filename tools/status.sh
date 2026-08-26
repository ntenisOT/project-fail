#!/usr/bin/env bash
# One-screen status of everything running on the Ireland box.
# Read-only: starts nothing, stops nothing, places nothing.
#
#   ssh -i ~/.ssh/pm_deploy ubuntu@3.254.130.64 'bash ~/project-fail/tools/status.sh'
#
# Columns that matter, in order:
#   edge$     FIFO-paired economics, direction-free. The half that works.
#   outcome$  settlement luck on the naked residual. The half that bleeds.
#   pnl$      edge + outcome. Do not read this alone - both review seats said so.
#   validity  scored windows / attempted. Below ~90% the sample is thin.
set -u
cd /home/ubuntu/project-fail || exit 1

echo "=== SESSIONS ==="
tmux ls 2>/dev/null || echo "(no tmux server)"

echo
echo "=== PAPER: flatten A/B (basket99 = control, f285/f240/f180 = flatten) ==="
./.venv/bin/python -m paper.report 2>&1 | head -8

echo
echo "=== FLATTEN FILLS (control must never appear here) ==="
./.venv/bin/python - <<'PY'
import sqlite3
try:
    db = sqlite3.connect("paper/paper.db")
    rows = db.execute(
        "SELECT strategy, count(*), round(sum(size),1), round(sum(signed_cash),2) "
        "FROM fills WHERE action='flatten_sell' GROUP BY strategy ORDER BY strategy"
    ).fetchall()
except sqlite3.Error as exc:
    print(f"  ledger unreadable: {exc}")
    rows = []
if rows:
    print(f"  {'strategy':<15}{'n':>4}{'shares':>9}{'cash$':>9}")
    for name, n, sh, cash in rows:
        print(f"  {name:<15}{n:>4}{sh:>9}{cash:>9}")
else:
    print("  none yet")
PY

echo
echo "=== LIVE PATH (must read log-only / zero orders) ==="
grep -a '^LIVE_EXECUTOR_MODE' .env 2>/dev/null || echo "  LIVE_EXECUTOR_MODE unset"
grep -a '^MINTBOT_MODE' .env 2>/dev/null || echo "  MINTBOT_MODE unset (default shadow)"
tail -2 live/executor.log 2>/dev/null | cut -c1-120

echo
echo "=== BRAKES (touch either to stop instantly) ==="
echo "  paper/KILL          $( [ -f paper/KILL ] && echo PRESENT || echo absent )"
echo "  paper/MINTBOT_KILL  $( [ -f paper/MINTBOT_KILL ] && echo PRESENT || echo absent )"
