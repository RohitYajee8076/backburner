"""
The MCP **Tasks** extension for backburner (spec: SEP-2663,
identifier ``io.modelcontextprotocol/tasks``, MCP revision 2026-07-28).

WHAT THIS IS, IN PLAIN TERMS
----------------------------
backburner already has five ordinary tools (start_task, task_status, ...).
Those work with every MCP client. The *Tasks extension* is a second, richer way
to talk to the same engine — the official protocol for "this tool call will take
a while, here's a ticket, poll me later." A client that understands Tasks calls a
tool the normal way; instead of a normal answer the server hands back a *task
handle* (a ``taskId``), and the client polls ``tasks/get`` until it's done.

That maps almost 1:1 onto our engine, because a backburner job ALREADY is a
long-running thing with an id, a status, and captured output:

    MCP task            backburner job (jobs.py)
    --------            ------------------------
    taskId              job.id
    tasks/get           manager.result(id)   (status + output)
    tasks/cancel        manager.cancel(id)
    task status         job.status  (mapped to the spec's status vocabulary)

HOW THE SDK PLUGS THIS IN
-------------------------
We subclass ``mcp.server.extension.Extension`` and override two things:

* ``intercept_tool_call`` — wraps every ``tools/call``. When a Tasks-capable
  client calls ``start_task``, we short-circuit and return a ``CreateTaskResult``
  (``resultType: "task"``) instead of the plain tool output. Any other tool, or a
  client that didn't opt in, passes straight through unchanged.
* ``methods`` — registers the three new request methods the extension defines:
  ``tasks/get``, ``tasks/update``, ``tasks/cancel``.

The instance is handed to ``MCPServer(extensions=[...])`` in server.py.

A NOTE ON STATUS MAPPING (this is the subtle, spec-correct part)
----------------------------------------------------------------
SEP-2663 draws a hard line:

* ``failed``    — ONLY for JSON-RPC *protocol* faults (the machinery broke).
* ``completed`` — for a request that produced a result, *even a tool error*
  (a ``CallToolResult`` with ``isError: true``).

A shell command exiting non-zero, or being killed by a timeout, is a perfectly
ordinary *tool* outcome — not a protocol fault. So backburner's ``failed`` and
``timed_out`` map to task status ``completed`` with ``isError: true`` in the
result. Only ``interrupted`` (our own server died mid-job) is a genuine
machinery fault, and that alone maps to ``failed``. See ``_render_task``.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import require_client_extension
from mcp.shared.exceptions import MCPError
from mcp_types import RequestParams
from mcp_types.jsonrpc import INTERNAL_ERROR, INVALID_PARAMS

from backburner.jobs import (
    CANCELLED,
    COMPLETED,
    FAILED,
    INTERRUPTED,
    TIMED_OUT,
    WORKING,
    JobManager,
)

# The reverse-DNS label the whole extension is published under. The client
# advertises support with this exact string; the server advertises it back via
# ServerCapabilities.extensions (the SDK does that from `identifier`).
TASKS_IDENTIFIER = "io.modelcontextprotocol/tasks"

# backburner keeps every job in SQLite forever, so a task never expires — the
# spec lets us signal "no TTL" with null. And we suggest clients poll roughly
# every couple of seconds (jobs settle within a few seconds of finishing).
_TTL_MS = None
_POLL_INTERVAL_MS = 2000

# We only turn ONE tool into a task: start_task is the inherently long-running
# one. The other four tools stay plain (and keep working for every client).
_TASK_TOOL = "start_task"


# ---------------------------------------------------------------------------
# request-params shapes for the three new methods
#
# MethodBinding validates incoming params against a model before our handler
# runs. These subclass RequestParams (an MCPModel), which uses a camelCase alias
# generator + populate_by_name — so a field named `task_id` accepts the wire key
# `taskId` automatically. That's why there are no explicit Field(alias=...) here.
# ---------------------------------------------------------------------------
class _TaskIdParams(RequestParams):
    """Params for tasks/get and tasks/cancel: just the task to act on."""

    task_id: str  # wire: "taskId"


class _UpdateTaskParams(RequestParams):
    """Params for tasks/update.

    ``inputResponses`` answers questions a task raised via ``inputRequests``.
    backburner tasks never ask for input, so we accept the field for spec
    conformance but have nothing to feed it into — see the handler.
    """

    task_id: str  # wire: "taskId"
    input_responses: dict[str, Any] | None = None  # wire: "inputResponses"


def _client_supports_tasks(ctx: ServerRequestContext[Any, Any]) -> bool:
    """True if the connected client declared support for the Tasks extension.

    The SDK parses the client's per-request capability declaration off the
    2026-07-28 request envelope and exposes it as
    ``ctx.session.client_capabilities`` — that, NOT the raw ``params._meta``, is
    where the capability lives. ``extensions`` is a dict keyed by extension
    identifier; our identifier being present means "opted in".

    This is the read-only check used to *decide* whether to make a task. The
    method handlers instead call the SDK's ``require_client_extension`` (below),
    which raises the spec's ``-32021`` when the client hasn't opted in.
    """
    caps = ctx.session.client_capabilities
    extensions = caps.extensions if caps else None
    return bool(extensions) and TASKS_IDENTIFIER in extensions


def _iso(epoch: float) -> str:
    """Format a Unix timestamp as an ISO 8601 UTC string, e.g. 2026-07-28T10:30:00Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class BackburnerTasksExtension(Extension):
    """Serves the io.modelcontextprotocol/tasks protocol on top of a JobManager."""

    identifier = TASKS_IDENTIFIER

    def __init__(self, manager: JobManager) -> None:
        # The same engine instance server.py's plain tools use, so a task
        # created here is visible to task_status/list_tasks and vice-versa.
        self.manager = manager

    # -- new request methods -------------------------------------------------
    def methods(self):
        """Register tasks/get, tasks/update, tasks/cancel with the server."""
        return (
            MethodBinding("tasks/get", _TaskIdParams, self._handle_get),
            MethodBinding("tasks/update", _UpdateTaskParams, self._handle_update),
            MethodBinding("tasks/cancel", _TaskIdParams, self._handle_cancel),
        )

    # -- turning a tools/call into a task ------------------------------------
    async def intercept_tool_call(
        self,
        params: Any,  # CallToolRequestParams
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        """Answer a Tasks-capable ``start_task`` call with a task handle.

        Server-directed creation: WE decide, per request, to respond with a task
        rather than a normal result. We only do so when (a) the tool is
        start_task and (b) the client declared Tasks support — otherwise the
        spec forbids returning a CreateTaskResult, so we pass through and the
        plain tool answers normally.
        """
        if params.name != _TASK_TOOL or not _client_supports_tasks(ctx):
            return await call_next(ctx)

        args = params.arguments or {}
        try:
            job = self.manager.start(
                command=args["command"],
                cwd=args.get("cwd"),
                timeout_seconds=args.get("timeout_seconds"),
            )
        except KeyError:
            # `command` is required; missing it is a client mistake.
            raise MCPError(INVALID_PARAMS, "start_task requires a 'command' argument")
        except (PermissionError, ValueError) as exc:
            # Command policy refusal or a bad cwd/timeout: a pre-execution
            # problem, so report it as a protocol error rather than a task.
            raise MCPError(INVALID_PARAMS, str(exc))

        # The job row is committed before start() returns, so a tasks/get for
        # this id would already resolve — satisfying the spec's "durably
        # created before responding" rule. resultType "task" marks this as a
        # task handle rather than the normal tool result.
        return self._render_task("task", job.id)

    # -- method handlers -----------------------------------------------------
    async def _handle_get(self, ctx, params: _TaskIdParams) -> HandlerResult:
        """tasks/get — report a task's current status (and result once done)."""
        require_client_extension(ctx, TASKS_IDENTIFIER)
        return self._render_task("complete", params.task_id)

    async def _handle_update(self, ctx, params: _UpdateTaskParams) -> HandlerResult:
        """tasks/update — deliver inputResponses. backburner never needs any.

        Our tasks are fire-and-forget shell jobs; they never enter
        ``input_required``, so there are no outstanding requests to answer. Per
        spec a server SHOULD ignore responses to keys that aren't outstanding,
        which for us is all of them. We still validate the task exists and then
        return the empty acknowledgement the spec mandates.
        """
        require_client_extension(ctx, TASKS_IDENTIFIER)
        self._get_or_404(params.task_id)  # 404 if the id is bogus
        return {"resultType": "complete"}

    async def _handle_cancel(self, ctx, params: _TaskIdParams) -> HandlerResult:
        """tasks/cancel — ask the engine to kill the job, then ack.

        Cancellation is cooperative and the ack carries no body. We DO trigger
        the real kill (manager.cancel) so the intent actually takes effect; the
        client learns the outcome by polling tasks/get afterward if it cares.
        """
        require_client_extension(ctx, TASKS_IDENTIFIER)
        self._get_or_404(params.task_id)
        self.manager.cancel(params.task_id)
        return {"resultType": "complete"}

    # -- helpers -------------------------------------------------------------
    def _get_or_404(self, task_id: str) -> dict:
        """Fetch a job's full result dict, or raise the spec's not-found error.

        SEP-2663: an unknown/expired taskId is -32602 (Invalid params).
        """
        try:
            return self.manager.result(task_id)
        except KeyError:
            raise MCPError(INVALID_PARAMS, f"Failed to retrieve task: Task not found ({task_id})")

    def _render_task(self, result_type: str, task_id: str) -> dict:
        """Build the flat task payload shared by CreateTaskResult and GetTaskResult.

        In SEP-2663, ``CreateTaskResult = Result & Task`` and
        ``GetTaskResult = Result & DetailedTask`` — intersections, so the Task
        fields sit *flat* on the result (not nested under a "task" key). The only
        differences between the two are ``resultType`` and, for terminal states,
        the ``result``/``error`` payload — which is exactly why one builder
        serves both.
        """
        job = self._get_or_404(task_id)  # dict from manager.result(): status, output, ...
        status = job["status"]

        # created_at in the dict is a preformatted local string; for ISO 8601 we
        # go back to the engine's raw epoch via a fresh status() lookup.
        raw = self.manager.status(task_id)
        created_iso = _iso(raw.created_at)
        updated_iso = _iso(raw.finished_at) if raw.finished_at is not None else created_iso

        payload: dict[str, Any] = {
            "resultType": result_type,
            "taskId": task_id,
            "status": self._spec_status(status),
            "createdAt": created_iso,
            "lastUpdatedAt": updated_iso,
            "ttlMs": _TTL_MS,
            "pollIntervalMs": _POLL_INTERVAL_MS,
        }

        if status == WORKING:
            payload["statusMessage"] = "The task is running; poll tasks/get for progress."
        elif status == INTERRUPTED:
            # The only genuine protocol fault: our server died mid-job. `failed`
            # status carries a JSON-RPC error object.
            payload["statusMessage"] = "The server was interrupted while the task was running."
            payload["error"] = {
                "code": INTERNAL_ERROR,
                "message": "server interrupted while the task was running",
            }
        else:
            # completed / failed / timed_out / cancelled — all produced a result
            # (output), so they carry a CallToolResult. For cancelled the client
            # mostly just wants the status, but returning the captured output so
            # far is harmless and useful.
            is_error = status in (FAILED, TIMED_OUT)
            payload["statusMessage"] = self._status_message(status)
            payload["result"] = {
                "content": [{"type": "text", "text": self._result_text(job)}],
                "structuredContent": job,
                "isError": is_error,
            }
        return payload

    @staticmethod
    def _spec_status(status: str) -> str:
        """Map a backburner state to a SEP-2663 task status.

        See the module docstring's status-mapping note for the reasoning:
        non-zero exit / timeout are tool-level (=> completed), only a server
        interruption is a protocol fault (=> failed).
        """
        return {
            WORKING: "working",
            COMPLETED: "completed",
            FAILED: "completed",
            TIMED_OUT: "completed",
            CANCELLED: "cancelled",
            INTERRUPTED: "failed",
        }.get(status, "completed")

    @staticmethod
    def _status_message(status: str) -> str:
        return {
            COMPLETED: "The command finished successfully.",
            FAILED: "The command exited with a non-zero status.",
            TIMED_OUT: "The command was killed after exceeding its timeout.",
            CANCELLED: "The task was cancelled.",
        }.get(status, status)

    @staticmethod
    def _result_text(job: dict) -> str:
        """Human-readable one-liner + captured output tail for the result content."""
        header = f"[{job['status']}] {job.get('command', '')}".strip()
        exit_code = job.get("exit_code")
        if exit_code is not None:
            header += f" (exit code {exit_code})"
        output = job.get("output", "")
        return f"{header}\n{output}" if output else header
