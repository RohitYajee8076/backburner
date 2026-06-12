import sys
import time

import pytest

from backburner import jobs
from backburner.jobs import (
    CANCELLED, COMPLETED, FAILED, INTERRUPTED, TIMED_OUT, WORKING,
    JobManager, check_command_allowed,
)

PY = sys.executable


def wait_until_done(m: JobManager, job_id: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = m.status(job_id)
        if job.status != WORKING:
            return job
        time.sleep(0.1)
    pytest.fail(f"job {job_id} still working after {timeout}s")


@pytest.fixture()
def manager():
    return JobManager()


# --------------------------------------------------------------- lifecycle
def test_successful_job_completes_with_output(manager):
    job = manager.start(f'{PY} -c "print(\'hello quest\')"')
    assert job.status == WORKING
    done = wait_until_done(manager, job.id)
    assert done.status == COMPLETED
    assert done.exit_code == 0
    result = manager.result(job.id)
    assert "hello quest" in result["output"]


def test_failing_job_reports_failed_and_exit_code(manager):
    job = manager.start(f'{PY} -c "raise SystemExit(3)"')
    done = wait_until_done(manager, job.id)
    assert done.status == FAILED
    assert done.exit_code == 3


def test_cancel_kills_running_job(manager):
    job = manager.start(f'{PY} -c "import time; time.sleep(60)"')
    time.sleep(1.0)  # let the process actually start
    manager.cancel(job.id)
    done = wait_until_done(manager, job.id, timeout=10)
    assert done.status == CANCELLED


def test_cancel_finished_job_is_noop(manager):
    job = manager.start(f'{PY} -c "print(1)"')
    done = wait_until_done(manager, job.id)
    again = manager.cancel(job.id)
    assert again.status == done.status == COMPLETED


def test_timeout_marks_job_timed_out(manager):
    job = manager.start(
        f'{PY} -c "import time; time.sleep(60)"', timeout_seconds=2
    )
    done = wait_until_done(manager, job.id, timeout=15)
    assert done.status == TIMED_OUT


def test_fast_job_beats_its_timeout(manager):
    job = manager.start(f'{PY} -c "print(\'quick\')"', timeout_seconds=30)
    done = wait_until_done(manager, job.id)
    assert done.status == COMPLETED
    # the pending timer must NOT later flip a finished job's status
    time.sleep(0.5)
    assert manager.status(job.id).status == COMPLETED


# ------------------------------------------------------------- validation
def test_unknown_task_id_raises(manager):
    with pytest.raises(KeyError):
        manager.status("nope1234")


def test_bad_cwd_raises(manager):
    with pytest.raises(ValueError):
        manager.start("echo hi", cwd="Z:/definitely/not/a/dir")


def test_nonpositive_timeout_raises(manager):
    with pytest.raises(ValueError):
        manager.start("echo hi", timeout_seconds=0)


# ------------------------------------------------------------ result tail
def test_result_tail_lines(manager):
    job = manager.start(f'{PY} -c "[print(i) for i in range(50)]"')
    wait_until_done(manager, job.id)
    result = manager.result(job.id, tail_lines=5)
    assert result["output_total_lines"] == 50
    assert result["output"].splitlines() == ["45", "46", "47", "48", "49"]


# ----------------------------------------------------------------- listing
def test_list_returns_newest_first(manager):
    a = manager.start(f'{PY} -c "print(\'a\')"')
    time.sleep(0.05)
    b = manager.start(f'{PY} -c "print(\'b\')"')
    wait_until_done(manager, a.id)
    wait_until_done(manager, b.id)
    listed = [j.id for j in manager.list(limit=50)]
    assert listed.index(b.id) < listed.index(a.id)


# ------------------------------------------------------------ crash honesty
def test_orphaned_jobs_marked_interrupted(manager):
    job = manager.start(f'{PY} -c "import time; time.sleep(60)"')
    # Simulate a server restart: a fresh manager finds the 'working' row
    # but owns no process handle for it.
    fresh = JobManager()
    assert fresh.status(job.id).status == INTERRUPTED
    manager.cancel(job.id)  # clean up the real process


# ---------------------------------------------------------- command policy
def test_deny_pattern_blocks_command(monkeypatch):
    monkeypatch.setenv("BACKBURNER_DENY", r"rm\s+-rf,format")
    with pytest.raises(PermissionError, match="deny pattern"):
        check_command_allowed("rm -rf /")


def test_allowlist_blocks_unlisted_command(monkeypatch):
    monkeypatch.setenv("BACKBURNER_ALLOW", r"^pytest,^npm (test|run build)")
    with pytest.raises(PermissionError, match="allow pattern"):
        check_command_allowed("curl http://evil.example")


def test_allowlist_permits_listed_command(monkeypatch):
    monkeypatch.setenv("BACKBURNER_ALLOW", r"^pytest,^npm (test|run build)")
    check_command_allowed("pytest -q tests/")
    check_command_allowed("npm run build")


def test_deny_wins_over_allow(monkeypatch):
    monkeypatch.setenv("BACKBURNER_ALLOW", r".*")
    monkeypatch.setenv("BACKBURNER_DENY", r"shutdown")
    with pytest.raises(PermissionError):
        check_command_allowed("shutdown /s /t 0")


def test_no_policy_allows_anything(monkeypatch):
    monkeypatch.delenv("BACKBURNER_ALLOW", raising=False)
    monkeypatch.delenv("BACKBURNER_DENY", raising=False)
    check_command_allowed("echo unrestricted")


def test_start_enforces_policy(manager, monkeypatch):
    monkeypatch.setenv("BACKBURNER_DENY", "forbidden")
    with pytest.raises(PermissionError):
        manager.start("echo forbidden-thing")


def test_cancelled_job_omits_exit_code(manager):
    job = manager.start(f'{PY} -c "import time; time.sleep(30)"')
    time.sleep(1.0)
    manager.cancel(job.id)
    d = manager.status(job.id).to_dict()
    assert d["status"] == "cancelled"
    assert "exit_code" not in d
