"""
Close-and-reopen demo — proof, not a mock-up.

The differentiator that sets backburner apart from a client's built-in
"run this in the background" trick is simple: **built-in background execution
lives inside the conversation, so it evaporates the moment the session ends.**
backburner keeps every task and its full output on disk (SQLite + per-task log
files under ~/.backburner), so a task you start in one session is still there —
with its result — in a completely separate session later.

This script demonstrates exactly that using TWO REAL, SEPARATE PROCESSES that
share nothing but the on-disk state:

    session 1  ->  starts a job, lets it finish, then the process EXITS
                   (this is you closing Claude / quitting the client)
    session 2  ->  a brand-new process (you, in a new chat tomorrow) that never
                   saw the task id — it just calls list_tasks() and finds the
                   finished work waiting, output and all.

Run it from the repo root:

    python docs/demo_restart.py

It uses a throwaway temp directory for BACKBURNER_HOME, so it never touches
your real ~/.backburner.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Windows consoles default to cp1252, which chokes on the box-drawing chars and
# check-mark below (the same class of bug backburner fixes for its jobs via
# PYTHONIOENCODING=utf-8). Force UTF-8 for our own output.
if hasattr(sys.stdout, "reconfigure"):
    # line_buffering so our banners interleave in order with child-process output.
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

REPO = Path(__file__).resolve().parent.parent

# A little job that prints progress over a few seconds so there is real,
# multi-line output to recover in session 2.
JOB = (
    'python -c "'
    "import time;"
    "print('crunching the nightly report...');"
    "[ (print(f'  processed batch {i}/3'), time.sleep(1)) for i in range(1, 4) ];"
    "print('REPORT READY: 4182 rows written')"
    '"'
)


def _manager():
    """Import the engine AFTER BACKBURNER_HOME is set (jobs.py reads it at import)."""
    sys.path.insert(0, str(REPO))
    from backburner.jobs import JobManager

    return JobManager()


def _rule(text: str) -> None:
    print("\n" + "─" * 66)
    print(text)
    print("─" * 66)


def session_one() -> None:
    """You, right now: kick off a long job and let it run, then close the client."""
    mgr = _manager()
    job = mgr.start(JOB)
    print(f"[session 1] started task {job.id}  (status: {job.status})")
    print("[session 1] ...you keep chatting with the agent while it runs...")
    while mgr.status(job.id).status == "working":
        time.sleep(0.3)
    done = mgr.status(job.id)
    print(f"[session 1] task {job.id} → {done.status}. You close the client and shut the laptop.")


def session_two() -> None:
    """A brand-new session later: no memory of any task id — just look them up."""
    mgr = _manager()  # fresh process, fresh object; state comes purely from disk
    tasks = mgr.list(limit=5)
    if not tasks:
        print("[session 2] no tasks found — did session 1 run?")
        return
    print(f"[session 2] list_tasks() finds {len(tasks)} task(s) from before this session:")
    for t in tasks:
        d = t.to_dict()
        print(f"    • {d['task_id']}  {d['status']:<10}  {d.get('command','')[:48]}")

    newest = tasks[0]
    print(f"\n[session 2] task_result('{newest.id}') — the work that finished while you were away:")
    res = mgr.result(newest.id)
    for line in res["output"].splitlines():
        print(f"    | {line}")
    print(f"\n[session 2] status={res['status']}  → the result survived the client closing. ✅")


def orchestrate() -> None:
    """Run the two sessions as genuinely separate processes over a shared temp home."""
    home = tempfile.mkdtemp(prefix="bb_demo_")
    # BACKBURNER_HOME shares on-disk state between the two sessions; the encoding
    # var keeps the child sessions' UTF-8 output readable on Windows.
    env = {**os.environ, "BACKBURNER_HOME": home, "PYTHONIOENCODING": "utf-8"}

    _rule("SESSION 1  —  start a long job, then quit the client")
    subprocess.run([sys.executable, __file__, "_s1"], env=env, check=True)

    _rule("· · ·  client fully closed — new session, could be tomorrow  · · ·")
    time.sleep(1)

    _rule("SESSION 2  —  a fresh process that never saw the task id")
    subprocess.run([sys.executable, __file__, "_s2"], env=env, check=True)

    print(f"\n(demo state was in {home} — safe to delete)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "_s1":
        session_one()
    elif mode == "_s2":
        session_two()
    else:
        orchestrate()
