"""
The job engine: start shell commands as background jobs, track them in
SQLite, capture their output to log files, and cancel them on demand.

This is deliberately independent of MCP — it's a plain Python library,
so it can be tested alone and later exposed through any protocol.
"""

from __future__ import annotations

import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# All job state lives under the user's home dir so it survives restarts
# and works no matter where the server is launched from.
DATA_DIR = Path(os.environ.get("BACKBURNER_HOME", Path.home() / ".backburner"))
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "jobs.db"

WORKING = "working"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"
INTERRUPTED = "interrupted"  # was running when the server died


def check_command_allowed(command: str) -> None:
    """Enforce the operator's command policy, set via environment variables.

    BACKBURNER_DENY  — comma-separated regexes; a command matching ANY is refused.
    BACKBURNER_ALLOW — comma-separated regexes; if set, a command must match at
                      least one or it is refused. Deny wins over allow.

    Both are matched case-insensitively anywhere in the command
    (e.g. BACKBURNER_ALLOW="^pytest,^npm (test|run build)").
    Raises PermissionError with a clear message when a command is refused.
    """
    deny = [p.strip() for p in os.environ.get("BACKBURNER_DENY", "").split(",") if p.strip()]
    for pattern in deny:
        if re.search(pattern, command, re.IGNORECASE):
            raise PermissionError(
                f"command refused: matches deny pattern {pattern!r} (BACKBURNER_DENY)"
            )
    allow = [p.strip() for p in os.environ.get("BACKBURNER_ALLOW", "").split(",") if p.strip()]
    if allow and not any(re.search(p, command, re.IGNORECASE) for p in allow):
        raise PermissionError(
            "command refused: does not match any allow pattern (BACKBURNER_ALLOW)"
        )


@dataclass
class Job:
    id: str
    command: str
    cwd: str
    status: str
    created_at: float
    finished_at: float | None
    exit_code: int | None
    pid: int | None
    log_path: str

    def to_dict(self) -> dict:
        d = {
            "task_id": self.id,
            "command": self.command,
            "cwd": self.cwd,
            "status": self.status,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at)),
        }
        if self.status == WORKING:
            d["running_for_seconds"] = round(time.time() - self.created_at)
        if self.finished_at is not None:
            d["duration_seconds"] = round(self.finished_at - self.created_at)
        # Only completed/failed have a meaningful exit code; a killed job's
        # code is just an artifact of how the OS terminated it.
        if self.exit_code is not None and self.status in (COMPLETED, FAILED):
            d["exit_code"] = self.exit_code
        return d


class JobManager:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}
        self._init_db()
        self._mark_orphans()

    # ---------------------------------------------------------------- db
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                       id TEXT PRIMARY KEY,
                       command TEXT NOT NULL,
                       cwd TEXT NOT NULL,
                       status TEXT NOT NULL,
                       created_at REAL NOT NULL,
                       finished_at REAL,
                       exit_code INTEGER,
                       pid INTEGER,
                       log_path TEXT NOT NULL
                   )"""
            )

    def _mark_orphans(self) -> None:
        # Jobs still marked 'working' from a previous run can't be ours —
        # their monitor threads died with the old process.
        with self._db() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE status = ?",
                (INTERRUPTED, time.time(), WORKING),
            )

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], command=row["command"], cwd=row["cwd"],
            status=row["status"], created_at=row["created_at"],
            finished_at=row["finished_at"], exit_code=row["exit_code"],
            pid=row["pid"], log_path=row["log_path"],
        )

    def _get(self, job_id: str) -> Job:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"no task with id '{job_id}'")
        return self._row_to_job(row)

    # ------------------------------------------------------------- public
    def start(
        self, command: str, cwd: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Job:
        check_command_allowed(command)
        job_id = uuid.uuid4().hex[:8]
        cwd = str(Path(cwd).resolve()) if cwd else os.getcwd()
        if not Path(cwd).is_dir():
            raise ValueError(f"working directory does not exist: {cwd}")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        log_path = str(LOG_DIR / f"{job_id}.log")

        log_file = open(log_path, "w", encoding="utf-8", errors="replace")
        # Children buffer stdout when it's a file, which would make live
        # peeking useless; force line-buffering where the runtime honors it.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        # New process group so cancel() can kill the command AND any
        # children it spawned (e.g. pytest workers), not just the shell.
        if sys.platform == "win32":
            proc = subprocess.Popen(
                command, shell=True, cwd=cwd, env=env,
                stdout=log_file, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            proc = subprocess.Popen(
                command, shell=True, cwd=cwd, env=env,
                stdout=log_file, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

        now = time.time()
        with self._db() as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (job_id, command, cwd, WORKING, now, proc.pid, log_path),
            )
        with self._lock:
            self._procs[job_id] = proc

        threading.Thread(
            target=self._watch, args=(job_id, proc, log_file), daemon=True
        ).start()
        if timeout_seconds is not None:
            timer = threading.Timer(
                timeout_seconds, self._finish_early, args=(job_id, TIMED_OUT)
            )
            timer.daemon = True
            timer.start()
        return self._get(job_id)

    def _watch(self, job_id: str, proc: subprocess.Popen, log_file) -> None:
        exit_code = proc.wait()
        log_file.close()
        with self._lock:
            self._procs.pop(job_id, None)
        with self._db() as conn:
            # Don't overwrite a 'cancelled' status written by cancel().
            conn.execute(
                "UPDATE jobs SET status = CASE WHEN status = ? THEN ? ELSE status END,"
                " finished_at = ?, exit_code = ? WHERE id = ?",
                (WORKING, COMPLETED if exit_code == 0 else FAILED,
                 time.time(), exit_code, job_id),
            )

    def status(self, job_id: str) -> Job:
        return self._get(job_id)

    def result(self, job_id: str, tail_lines: int = 100) -> dict:
        job = self._get(job_id)
        out = job.to_dict()
        try:
            text = Path(job.log_path).read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            out["output_total_lines"] = len(lines)
            out["output"] = "\n".join(lines[-tail_lines:]) if lines else ""
        except FileNotFoundError:
            out["output"] = ""
            out["output_total_lines"] = 0
        return out

    def cancel(self, job_id: str) -> Job:
        return self._finish_early(job_id, CANCELLED)

    def _finish_early(self, job_id: str, final_status: str) -> Job:
        """Kill a working job's process tree and record why (cancel/timeout)."""
        job = self._get(job_id)
        if job.status != WORKING:
            return job  # already finished; nothing to do
        with self._db() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ? AND status = ?",
                (final_status, time.time(), job_id, WORKING),
            )
        with self._lock:
            proc = self._procs.get(job_id)
        if proc is not None and proc.poll() is None:
            if sys.platform == "win32":
                # /T kills the whole process tree.
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        return self._get(job_id)

    def list(self, limit: int = 20) -> list[Job]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]
