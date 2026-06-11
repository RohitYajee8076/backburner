# backburner

**Put your AI agent's slow work on the back burner. Keep cooking.**

`backburner` is an MCP server that gives any AI assistant (Claude, and any
other MCP client) the ability to run long shell commands as **background
tasks** — start a test suite, a build, a scrape, a batch job — then keep
working and check back for the results, instead of sitting frozen until
it finishes.

```
agent: start_task("pytest -q")        ->  { task_id: "a1b2c3d4", status: "working" }
        ... agent does other useful work ...
agent: task_status("a1b2c3d4")        ->  { status: "completed", duration_seconds: 312 }
agent: task_result("a1b2c3d4")        ->  { output: "418 passed in 311.2s" }
```

## Why

AI agents are bad at waiting. A tool call that takes 10 minutes blocks the
whole conversation — or times out and loses the work entirely. The MCP
specification is formalizing a Tasks pattern for exactly this problem
(extension finalized in the 2026-07-28 spec release); `backburner` brings
that workflow to every client **today** via plain tools, with first-class
Tasks-extension support on the roadmap.

## Tools

| Tool | What it does |
|------|--------------|
| `start_task(command, cwd?, timeout_seconds?)` | Run a shell command in the background, returns a task id immediately |
| `task_status(task_id)` | `working` / `completed` / `failed` / `cancelled` / `timed_out` / `interrupted` |
| `task_result(task_id, tail_lines?)` | Captured output — works mid-run too, so you can peek at progress |
| `cancel_task(task_id)` | Kill the task and its whole process tree |
| `list_tasks(limit?)` | Recent tasks, newest first |

## Features

- **Survives restarts** — tasks are tracked in SQLite under `~/.backburner/`;
  output is captured to per-task log files. If the server dies mid-task,
  orphaned tasks are honestly marked `interrupted`, never silently lost.
- **Real cancellation** — kills the full process tree (worker processes
  included), on Windows and Unix.
- **Peek at live progress** — `task_result` on a running task returns the
  output so far.
- **Timeouts** — pass `timeout_seconds` and a runaway task is killed and
  honestly marked `timed_out` instead of hanging forever.
- **Command policy** — restrict what the AI may run with environment
  variables (regexes, comma-separated; deny always wins):

  ```bash
  BACKBURNER_ALLOW="^pytest,^npm (test|run build)"   # only these may run
  BACKBURNER_DENY="rm -rf,shutdown,format"           # these never run
  ```
- **Zero infrastructure** — stdlib only (SQLite, subprocess, threads).
  No Redis, no Celery, no Docker.
- **Tested** — a pytest suite covers the full job lifecycle: completion,
  failure, cancellation, timeouts, crash recovery, and the command policy.

## Install

```bash
pip install backburner-mcp
```

### Claude Code

```bash
claude mcp add backburner -- python -m backburner.server
```

### Claude Desktop / other clients

```json
{
  "mcpServers": {
    "backburner": {
      "command": "python",
      "args": ["-m", "backburner.server"]
    }
  }
}
```

## Security note

`backburner` executes the shell commands the AI sends it, with your user's
permissions. That is its job — but treat it like giving your agent a
terminal. Run it only with clients whose tool-use you review/approve,
prefer permission modes that require confirmation for `start_task`, and
use `BACKBURNER_ALLOW` / `BACKBURNER_DENY` to scope what may run.

## Roadmap

- [x] Task timeouts and max-runtime limits
- [x] Allowlist/denylist for commands
- [ ] MCP Tasks extension support (spec 2026-07-28) — native `tasks/get`,
      `tasks/cancel` alongside the plain tools
- [ ] Structured progress reporting (parse % / step markers from output)
- [x] PyPI release — `pip install backburner-mcp`

## License

MIT
