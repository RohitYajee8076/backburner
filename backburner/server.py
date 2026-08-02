"""
backburner — an MCP server that lets AI agents run long jobs in the
background and collect the results later, instead of blocking.

Run:  python -m backburner.server

Ported to the MCP 2026-07-28 spec SDK (mcp>=2.0.0). The old high-level class
`mcp.server.fastmcp.FastMCP` was REMOVED in mcp 2.0; the replacement is
`mcp.server.MCPServer`. Its `.tool()` decorator and `.run()` entrypoint are
near-identical, so these five plain tools carry over almost unchanged.

On top of those plain tools we mount the official MCP **Tasks** extension
(io.modelcontextprotocol/tasks, SEP-2663): a Tasks-capable client can drive the
same engine through tasks/get, tasks/update, tasks/cancel instead of polling the
plain tools. See tasks_extension.py — it wraps the SAME JobManager instance, so
both surfaces see the same jobs.
"""

from mcp.server import MCPServer

from backburner.jobs import JobManager
from backburner.tasks_extension import BackburnerTasksExtension

manager = JobManager()

mcp = MCPServer(
    "backburner",
    instructions=(
        "Run long shell commands as background tasks. Start a task, keep "
        "working on other things, then poll its status and fetch the output "
        "when it's done. Ideal for test suites, builds, scrapes, batch jobs — "
        "anything too slow to wait for."
    ),
    # The Tasks extension shares the engine above so a task created via
    # tools/call is the same job list_tasks/task_status report on.
    extensions=[BackburnerTasksExtension(manager)],
)


@mcp.tool()
def start_task(
    command: str, cwd: str | None = None, timeout_seconds: float | None = None
) -> dict:
    """Start a shell command as a background task and return immediately.

    Args:
        command: The shell command to run (e.g. "pytest -q" or "npm run build").
        cwd: Working directory for the command. Defaults to the server's cwd.
        timeout_seconds: If set, the task is killed and marked 'timed_out'
            when it runs longer than this. Recommended for unattended jobs.

    Returns the new task's id and initial status. The command keeps running
    after this call returns — use task_status / task_result to follow it.
    """
    return manager.start(command, cwd, timeout_seconds).to_dict()


@mcp.tool()
def task_status(task_id: str) -> dict:
    """Check on a task: working, completed, failed, cancelled, timed_out, or interrupted."""
    return manager.status(task_id).to_dict()


@mcp.tool()
def task_result(task_id: str, tail_lines: int = 100) -> dict:
    """Get a task's captured output (stdout+stderr merged).

    Args:
        task_id: The task to inspect. Works for finished AND still-running
            tasks, so you can peek at live progress.
        tail_lines: How many trailing lines of output to return.
    """
    return manager.result(task_id, tail_lines)


@mcp.tool()
def cancel_task(task_id: str) -> dict:
    """Cancel a running task, killing its whole process tree."""
    return manager.cancel(task_id).to_dict()


@mcp.tool()
def list_tasks(limit: int = 20) -> list[dict]:
    """List recent tasks, newest first."""
    return [j.to_dict() for j in manager.list(limit)]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
