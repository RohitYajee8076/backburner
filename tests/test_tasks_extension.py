"""Unit tests for the io.modelcontextprotocol/tasks extension (SEP-2663).

No task-capable MCP client exists to drive this end-to-end yet, so we test the
extension's handlers directly: build the request params + a stand-in request
context ourselves, call the handler coroutines, and assert on the wire-shaped
dicts they return.

The capability the client declares lives on ``ctx.session.client_capabilities``
(the SDK parses it off the 2026-07-28 request envelope), so we fake a context
carrying a ``ClientCapabilities`` — matching how the real SDK hands it to us.

conftest.py points BACKBURNER_HOME at a temp dir before import (see the plain
server tests), so these use a real, throwaway JobManager.
"""

import asyncio
import time
from dataclasses import dataclass

import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import ClientCapabilities
from mcp_types.jsonrpc import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
)

from backburner.jobs import JobManager
from backburner.tasks_extension import (
    TASKS_IDENTIFIER,
    BackburnerTasksExtension,
    _TaskIdParams,
    _UpdateTaskParams,
)


# --- a minimal stand-in for the SDK's per-request context ------------------
# The handlers only ever read ctx.session.client_capabilities, so that's all we
# fake. `require_client_extension` reads the same attribute.
@dataclass
class _FakeSession:
    client_capabilities: ClientCapabilities | None


@dataclass
class _FakeCtx:
    session: _FakeSession


def capable_ctx() -> _FakeCtx:
    """A context whose client declared the Tasks extension."""
    return _FakeCtx(_FakeSession(ClientCapabilities(extensions={TASKS_IDENTIFIER: {}})))


def plain_ctx() -> _FakeCtx:
    """A context whose client declared no extensions."""
    return _FakeCtx(_FakeSession(None))


@pytest.fixture
def ext():
    return BackburnerTasksExtension(JobManager())


def _run(coro):
    return asyncio.run(coro)


def _wait_done(manager, task_id, timeout=15.0):
    """Block until a job leaves the 'working' state (test helper)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if manager.status(task_id).status != "working":
            return
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")


# --------------------------------------------------------------- capability
def test_get_without_capability_is_rejected(ext):
    params = _TaskIdParams(task_id="whatever")
    with pytest.raises(MCPError) as exc:
        _run(ext._handle_get(plain_ctx(), params))
    assert exc.value.code == MISSING_REQUIRED_CLIENT_CAPABILITY


def test_unknown_task_id_is_invalid_params(ext):
    params = _TaskIdParams(task_id="does-not-exist")
    with pytest.raises(MCPError) as exc:
        _run(ext._handle_get(capable_ctx(), params))
    assert exc.value.code == INVALID_PARAMS


# --------------------------------------------------- intercept: create a task
def test_intercept_passes_through_non_task_tool(ext):
    async def call_next(ctx):
        return {"sentinel": "plain-tool-ran"}

    from mcp_types import CallToolRequestParams

    params = CallToolRequestParams(name="list_tasks", arguments={})
    result = _run(ext.intercept_tool_call(params, capable_ctx(), call_next))
    assert result == {"sentinel": "plain-tool-ran"}


def test_intercept_passes_through_when_capability_absent(ext):
    async def call_next(ctx):
        return {"sentinel": "plain-start_task-ran"}

    from mcp_types import CallToolRequestParams

    # start_task, but the client never declared Tasks support -> no task handle.
    params = CallToolRequestParams(name="start_task", arguments={"command": "echo hi"})
    result = _run(ext.intercept_tool_call(params, plain_ctx(), call_next))
    assert result == {"sentinel": "plain-start_task-ran"}


def test_intercept_capable_start_task_returns_task_handle(ext):
    async def call_next(ctx):  # must NOT be called
        raise AssertionError("call_next should be short-circuited")

    from mcp_types import CallToolRequestParams

    params = CallToolRequestParams(name="start_task", arguments={"command": "echo hi"})
    result = _run(ext.intercept_tool_call(params, capable_ctx(), call_next))

    assert result["resultType"] == "task"
    assert result["taskId"]
    assert result["status"] in ("working", "completed")
    assert result["ttlMs"] is None
    assert result["pollIntervalMs"] == 2000
    # The task is durably created: task_status can see it immediately.
    assert ext.manager.status(result["taskId"]).id == result["taskId"]


def test_intercept_missing_command_is_invalid_params(ext):
    async def call_next(ctx):
        raise AssertionError("should not run")

    from mcp_types import CallToolRequestParams

    params = CallToolRequestParams(name="start_task", arguments={})
    with pytest.raises(MCPError) as exc:
        _run(ext.intercept_tool_call(params, capable_ctx(), call_next))
    assert exc.value.code == INVALID_PARAMS


# --------------------------------------------------------- get: terminal state
def test_get_completed_task_carries_successful_result(ext):
    job = ext.manager.start("echo hi")
    _wait_done(ext.manager, job.id)

    params = _TaskIdParams(task_id=job.id)
    res = _run(ext._handle_get(capable_ctx(), params))

    assert res["resultType"] == "complete"
    assert res["status"] == "completed"
    assert res["result"]["isError"] is False
    assert res["createdAt"].endswith("Z")  # ISO 8601 UTC


def test_get_nonzero_exit_is_completed_with_tool_error(ext):
    # A command exiting non-zero is a TOOL error, not a protocol fault:
    # SEP-2663 => status "completed" with isError true (NOT "failed").
    job = ext.manager.start("exit 1")
    _wait_done(ext.manager, job.id)

    params = _TaskIdParams(task_id=job.id)
    res = _run(ext._handle_get(capable_ctx(), params))

    assert res["status"] == "completed"
    assert res["result"]["isError"] is True


# --------------------------------------------------------------- update/cancel
def test_update_acknowledges_known_task(ext):
    job = ext.manager.start("echo hi")
    params = _UpdateTaskParams(task_id=job.id)
    res = _run(ext._handle_update(capable_ctx(), params))
    assert res == {"resultType": "complete"}


def test_cancel_acks_and_cancels(ext):
    # A long-running job we can actually cancel mid-flight.
    job = ext.manager.start("python -c \"import time; time.sleep(30)\"")
    params = _TaskIdParams(task_id=job.id)

    res = _run(ext._handle_cancel(capable_ctx(), params))
    assert res == {"resultType": "complete"}

    _wait_done(ext.manager, job.id)
    assert ext.manager.status(job.id).status == "cancelled"


def test_cancel_unknown_task_is_invalid_params(ext):
    params = _TaskIdParams(task_id="nope")
    with pytest.raises(MCPError) as exc:
        _run(ext._handle_cancel(capable_ctx(), params))
    assert exc.value.code == INVALID_PARAMS
