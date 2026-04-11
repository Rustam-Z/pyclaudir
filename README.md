# pyclaudir

Python harness running **Nodira**, a Telegram agent powered by a long-running
Claude Code subprocess and a local MCP server.

`pyclaudir` is the Python distillation of the Rust *Claudir* architecture:
the LLM lives inside a `claude --print --input-format stream-json` subprocess
that talks to the outside world *only* through tools we register with a
locally-hosted MCP server. There is no shell access, no general filesystem
access, no web fetch — Nodira can do exactly the things in `pyclaudir/tools/`
and nothing else.

## Quickstart

Prerequisites: Python 3.11+, [`uv`](https://github.com/astral-sh/uv), and the
Claude Code CLI (`claude --version` should work). Tested with Claude Code
2.1.85.

```bash
# 1. Get a Telegram bot token from @BotFather and find your numeric user id
#    (talk to @userinfobot if you don't know yours)

# 2. Configure
cp .env.example .env
$EDITOR .env   # set TELEGRAM_BOT_TOKEN and PYCLAUDIR_OWNER_ID

# 3. Install
uv sync --extra dev

# 4. Run the test suite
uv run pytest

# 5. Start Nodira
uv run python -m pyclaudir
```

DM your bot from Telegram. Nodira should reply.

## Configuration

All knobs live in environment variables (or `.env`):

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | from @BotFather |
| `PYCLAUDIR_OWNER_ID` | yes | — | your numeric Telegram user id |
| `PYCLAUDIR_ALLOWED_CHATS` | no | empty | comma-separated chat ids; empty = owner DMs only |
| `PYCLAUDIR_DATA_DIR` | no | `./data` | SQLite + memories live here |
| `PYCLAUDIR_MODEL` | no | `claude-opus-4-6` | passed to `--model` |
| `PYCLAUDIR_DEBOUNCE_MS` | no | `1000` | message coalescing window |
| `PYCLAUDIR_RATE_LIMIT_PER_MIN` | no | `20` | per-chat outbound cap |
| `CLAUDE_CODE_BIN` | no | `claude` | path to the CC CLI |

## How it works

Four components run inside one Python process:

```
Telegram dispatcher  →  Engine (debouncer/queue/inject)  →  CC worker  →  claude subprocess
                                          │                                       │
                                          ▼                                       ▼
                                       SQLite                              MCP server (HTTP, localhost:0)
```

1. **Telegram dispatcher** (`pyclaudir/telegram_io.py`). PTB v21 with manual
   lifecycle, polling. The handler does *only* two things: persist the
   incoming message to SQLite, then enqueue it on the engine. Owner-only
   `/kill`, `/reset`, `/restart` short-circuit the engine.
2. **Engine** (`pyclaudir/engine.py`). Owns the pending queue, the
   1-second debounce timer, the processing flag, and the inject channel.
   Coalesces bursts; if a message arrives while CC is mid-turn the engine
   pushes it through `worker.inject()` so the running turn picks it up.
   Detects "dropped text" (CC produced text without calling `send_message`)
   and corrects by injecting an `<error>...</error>` block.
3. **CC worker** (`pyclaudir/cc_worker.py`). Spawns and supervises the
   `claude` subprocess. Reads stream-json events from stdout, captures
   stderr for diagnostics, persists `session_id` so a restart resumes the
   same conversation, and respawns on crash with exponential backoff
   (2s → 64s, 10 crashes / 10 minutes = bail out).
4. **MCP server** (`pyclaudir/mcp_server.py`). FastMCP bound to a random
   port on `127.0.0.1`. Auto-discovers every `BaseTool` subclass in
   `pyclaudir/tools/` and registers them. Writes a tiny JSON config file
   pointing CC at the server via `--mcp-config`.

## Adding a new tool

Drop a single file in `pyclaudir/tools/`. No core code changes:

```python
# pyclaudir/tools/echo.py
from pydantic import BaseModel, Field
from pyclaudir.tools.base import BaseTool, ToolResult


class EchoArgs(BaseModel):
    text: str = Field(description="What to echo back.")


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo a string back to the caller."
    args_model = EchoArgs

    async def run(self, args: EchoArgs) -> ToolResult:
        return ToolResult(content=args.text)
```

Restart `python -m pyclaudir`. The tool is live.

## Memory

`data/memories/*.md` are read-only notes the **operator** curates by hand.
Nodira can list them with `list_memories` and read them with `read_memory`,
but cannot write to them in this version. If you want her to "know" something
durable, drop a file in `data/memories/` while she's running.

## Security model

Nodira is a *front-facing public agent*. Anyone in an allowed chat can talk
to her, and they're not always trustworthy. The security model is enforced
by code, not by hope, and tested in `tests/test_security_invariants.py`.

- **No shell, no edits, no writes, no general reads, no web access.** The
  CC subprocess is spawned with `--allowedTools mcp__pyclaudir
  --disallowedTools Bash,Edit,Write,Read,NotebookEdit,WebSearch,WebFetch
  --strict-mcp-config`. The forbidden flag `--dangerously-skip-permissions`
  is *never* passed; both the argv builder and the spawn-time assertion
  refuse it.
- **MCP namespace lockdown.** The local MCP server is registered as
  `pyclaudir`, so every tool Claude sees is named `mcp__pyclaudir__<x>`.
  Strict mode means Claude ignores all other MCP configs.
- **Read-only memory.** No `write_memory`/`edit_memory`/`delete_memory` tool
  exists. Path resolution is hardened against `..`, absolute paths, symlinks
  (rejected by `MemoryStore.resolve_path` and tested with hostile inputs).
- **No filesystem reads outside `memory.py`.** AST scan asserts no `open()`
  / `read_text()` / `read_bytes()` lives in any other tool module.
- **No subprocess calls in tools.** AST scan rejects `subprocess.*`,
  `os.system`, `os.popen`, `asyncio.create_subprocess_*` anywhere under
  `pyclaudir/tools/`. The *only* place those primitives are allowed is
  `cc_worker.py`, which spawns `claude` itself.
- **Owner-only privileged commands.** `/kill`, `/reset`, `/restart` check
  `update.effective_user.id == PYCLAUDIR_OWNER_ID` before running.
- **`query_db` is read-only.** Inputs are parsed with `sqlglot` and rejected
  unless they're a single SELECT. CTEs are walked recursively; semicolons,
  PRAGMA, ATTACH, INSERT/UPDATE/DELETE/DROP/CREATE/ALTER all fail. Results
  cap at 100 rows; text columns truncate at 2000 chars.
- **Per-chat outbound rate limit.** 20 messages / 60s / chat by default,
  enforced inside `send_message` (and therefore `reply_to_message`).
- **Audit log.** Every MCP tool invocation persists to `tool_calls` (name,
  args, result, error, duration).

If you weaken any of these, the security tests will fail loudly. They are
load-bearing — keep them.

## Manual end-to-end checklist

Once configured, you should be able to:

1. DM the bot, see Nodira reply via `send_message`.
2. Drop `data/memories/user_preferences.md` containing "Rustam prefers
   Russian", ask "what do you know about me?", watch her call
   `list_memories` → `read_memory` and reply in Russian.
3. Send 5 messages in 2 seconds, see them batched into one turn (debounce).
4. Send a 6th message *while* she's mid-turn, see it injected.
5. `sqlite3 data/pyclaudir.db 'SELECT direction, text FROM messages ORDER BY timestamp DESC LIMIT 10;'`
6. Drop `pyclaudir/tools/echo.py` (above), restart, and watch Nodira gain
   the new tool with zero other code changes.
7. `kill -9 $(pgrep -f 'claude --print')`, watch the worker respawn within
   seconds and resume the conversation.
8. Ask Nodira to run a shell command — she should refuse, because she has
   no `Bash` tool and her system prompt tells her to.
9. Run `uv run pytest tests/test_security_invariants.py` and see all 8
   invariants pass.

## Layout

```
pyclaudir/
├── pyproject.toml
├── README.md
├── prompts/system.md
├── data/                       # gitignored — pyclaudir.db, session_id, memories/
├── pyclaudir/
│   ├── __main__.py             # entrypoint
│   ├── config.py
│   ├── db/{database.py,messages.py,migrations/001_initial.sql}
│   ├── telegram_io.py
│   ├── engine.py
│   ├── cc_worker.py
│   ├── cc_schema.py
│   ├── mcp_server.py
│   ├── memory_store.py
│   ├── rate_limiter.py
│   ├── models.py
│   └── tools/
│       ├── base.py            # BaseTool, ToolContext, Heartbeat
│       ├── now.py
│       ├── send_message.py
│       ├── reply_to_message.py
│       ├── edit_message.py
│       ├── delete_message.py
│       ├── add_reaction.py
│       ├── memory.py          # read_memory + list_memories (read-only)
│       └── query_db.py
└── tests/
    ├── test_db_schema.py
    ├── test_mcp_server.py
    ├── test_tool_discovery.py
    ├── test_memory_path_safety.py
    ├── test_security_invariants.py
    ├── test_telegram_persistence.py
    ├── test_cc_worker_argv.py
    ├── test_engine_debouncer.py
    ├── test_inject_and_dropped_text.py
    ├── test_recovery_and_limits.py
    └── test_query_db.py
```
