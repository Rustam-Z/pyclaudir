# pyclaudir

Python harness for running a Telegram agent powered by a long-running
Claude Code subprocess and a local MCP server.

`pyclaudir` is the Python distillation of the Rust *Claudir* architecture:
the LLM lives inside a `claude --print --input-format stream-json` subprocess
that talks to the outside world *only* through tools we register with a
locally-hosted MCP server. There is no shell access, no general filesystem
access, no web fetch — the agent can do exactly the things in `pyclaudir/tools/`
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

# 5. Start the bot
uv run python -m pyclaudir
```

DM your bot from Telegram. Your bot should reply.

### Stopping the bot

```bash
# If running in the foreground — Ctrl+C in the terminal

# If running in the background
pkill -f 'python -m pyclaudir'
```

**Important:** Only one instance can poll the same Telegram bot token at a
time. If you see `Conflict: terminated by other getUpdates request`, another
instance is already running — kill it first.

## Configuration

All knobs live in environment variables (or `.env`):

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | from @BotFather |
| `PYCLAUDIR_OWNER_ID` | yes | — | your numeric Telegram user id |
| `PYCLAUDIR_DATA_DIR` | no | `./data` | SQLite, memories, access config, raw CC logs |
| `PYCLAUDIR_MODEL` | no | `claude-opus-4-6` | passed to `--model` |
| `PYCLAUDIR_EFFORT` | no | `high` | `--effort` flag: `low`, `medium`, `high`, `max` |
| `PYCLAUDIR_DEBOUNCE_MS` | no | `0` | message coalescing window (0 = instant) |
| `PYCLAUDIR_RATE_LIMIT_PER_MIN` | no | `20` | per-chat outbound cap |
| `PYCLAUDIR_PROJECT_PROMPT` | no | `prompts/project.md` | path to project-specific prompt (concatenated after `system.md`) |
| `CLAUDE_CODE_BIN` | no | `claude` | path to the CC CLI |
| `JIRA_URL` | no | — | Jira site URL (enables mcp-atlassian) |
| `JIRA_USERNAME` | no | — | Jira username |
| `JIRA_API_TOKEN` | no | — | Jira API token |
| `GITLAB_URL` | no | — | GitLab instance URL (enables mcp-gitlab) |
| `GITLAB_TOKEN` | no | — | GitLab personal access token |

Group and DM access is managed via `data/access.json`, not env vars.
See **Access control** below.

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

## Known limitations

### Single-turn blocking

The engine processes **one CC turn at a time**. While the Claude subprocess
is working on a long task (code review, multi-project GitLab search, complex
Jira queries), the engine blocks on `worker.wait_for_result()`. Messages
from other chats queue up in the pending buffer and are only dispatched
after the current turn finishes.

In practice this means a 3-minute code review for Chat A will delay
responses to Chat B by up to 3 minutes. For single-user or low-traffic
deployments this is fine. For high-traffic multi-chat setups, consider
running separate pyclaudir instances per chat group.

The system prompt instructs the agent to send an immediate acknowledgment
(e.g. "On it, reviewing now...") via `send_message` before starting long
tasks, so users know their request was received even though the full
response takes time.

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

## Access control

Who can talk to the bot is governed by `data/access.json`, which is
**hot-reloaded on every inbound message** — edits take effect immediately
without a restart.

```json
{
  "dm_policy": "owner_only",
  "allowed_users": [],
  "allowed_chats": [-1003938080260]
}
```

### DM policies

| Policy | Who can DM the bot |
|---|---|
| `owner_only` | Only `PYCLAUDIR_OWNER_ID`. Default. |
| `allowlist` | Owner + user IDs in `allowed_users`. |
| `open` | Anyone. |

The owner is **always** implicitly allowed regardless of policy.

### Groups

A group must be in `allowed_chats` for the bot to respond in it. Messages
from unlisted groups are still persisted to SQLite (audit trail) but
dropped before the engine sees them.

### Managing access from Telegram (owner-only commands)

```
/access                      Show current policy, allowed users, allowed chats
/allow 123456789             Add a user to the DM allowlist
/deny 123456789              Remove a user from the DM allowlist
/dmpolicy allowlist          Change DM policy (owner_only | allowlist | open)
```

Or edit `data/access.json` directly — changes are picked up on the next
message.

### First run bootstrap

If `data/access.json` doesn't exist on startup, pyclaudir creates it from
`PYCLAUDIR_ALLOWED_CHATS` in `.env` (if set) with `dm_policy: "owner_only"`.
After that, `access.json` is the source of truth and the env var is ignored.

A template is provided at `data/access.json.example`.

## Memory

`data/memories/*.md` is the agent's working memory. It has four tools:

- `list_memories` — see what files exist
- `read_memory` — read a file (truncates at 64 KiB)
- `write_memory` — create or overwrite a file (max 64 KiB per file)
- `append_memory` — extend an existing file

Writes are guarded by a **read-before-write rule**: before overwriting or
appending to a file that already exists, the agent must first read it in the
same session. Brand-new files are exempt. The "read paths" set lives in
the `MemoryStore` instance and resets on every restart, so a fresh process
must re-read before mutating. This stops the agent from blindly destroying
operator-curated notes whose contents it never observed.

There is **no delete tool** by design. If the agent wants to "forget" something
it has to overwrite the file. Actually removing files is an operator
action — `rm data/memories/<file>` from the host.

You can also seed memory yourself by dropping markdown files into
`data/memories/` while the agent is running. It'll discover them on the next
`list_memories` call.

## Reminders

The agent can schedule one-shot and recurring reminders via three tools:

- `set_reminder` — schedule a reminder with a UTC trigger time and optional cron expression
- `list_reminders` — show pending reminders for a chat
- `cancel_reminder` — cancel a pending reminder by id

Reminders are stored in the `reminders` SQLite table. A background task polls
every 60 seconds for due entries and injects them into the engine as synthetic
inbound messages. The agent then sends the reminder text to the appropriate
chat. Recurring reminders (cron) automatically advance to the next occurrence.

All times are stored in UTC. The system prompt instructs the agent to ask
users for their timezone and convert to UTC before setting reminders.

## System prompt

The system prompt is assembled from two files:

1. **`prompts/system.md`** — generic pyclaudir template covering tool
   discipline, message format, memory, reminders, and prompt-injection
   resistance. Ships with the repo.
2. **`prompts/project.md`** — project-specific overlay (identity, integrations,
   custom instructions). Gitignored. Copy `prompts/project.md.example` to get
   started. Override the path with `PYCLAUDIR_PROJECT_PROMPT`.

If `project.md` doesn't exist, only the base prompt is used.

## External MCP integrations

pyclaudir can optionally connect to external MCP servers alongside its own:

- **Jira** via [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) —
  set `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` in `.env`.
- **GitLab** via [@zereight/mcp-gitlab](https://www.npmjs.com/package/@zereight/mcp-gitlab) —
  set `GITLAB_URL`, `GITLAB_TOKEN` in `.env`.

These are stdio-based MCP servers spawned as child processes alongside the
local pyclaudir MCP server.

## Monitoring & observability

Pyclaudir gives you **four complementary windows** into what the bot is doing.
Pick whichever fits the moment.

### 1. The live tagged log (the running terminal)

When the bot is running, the foreground terminal prints two streams of
structured tag lines on top of the usual lifecycle messages:

**Conversation transcript** (`pyclaudir.tx` logger):

| Tag | Meaning |
|---|---|
| `[RX]` | inbound message we forwarded to the engine |
| `[DROP]` | inbound message persisted but dropped (chat not allowed) |
| `[RX↺]` | inbound edited message |
| `[TX]` | outbound `send_message` / `reply_to_message` |
| `[EDIT]` / `[DEL]` / `[REACT]` | outbound edits, deletions, reactions |

**Claude Code subprocess transcript** (`pyclaudir.cc` logger):

| Tag | Meaning |
|---|---|
| `[CC.user]` | the XML batch we just shipped to CC's stdin |
| `[CC.text]` | a text block the assistant emitted (rare; signals dropped-text) |
| `[CC.tool→]` | the assistant called a tool (with args + tool_use_id) |
| `[CC.tool✓]` / `[CC.tool✗]` | a tool returned (success / error) |
| `[CC.done]` | turn finished, parsed `action` + `reason` |

Sample (DM with one message):

```
21:34:12 INFO  pyclaudir.tx       [RX] DM Rustam[587272213] m42 | how fast are you
21:34:12 INFO  pyclaudir.engine   starting turn with 1 msgs
21:34:12 INFO  pyclaudir.cc       [CC.user] <msg id="42" chat="587272213" ...>↵how fast are you↵</msg>
21:34:13 INFO  pyclaudir.cc       [CC.tool→] mcp__pyclaudir__send_message({"chat_id":587272213,"text":"Honestly?…"}) id=toolu_01
21:34:14 INFO  pyclaudir.tx       [TX] DM Rustam[587272213] m43 | Honestly? Not blazing fast 😅 …
21:34:14 INFO  pyclaudir.cc       [CC.tool✓] id=toolu_01 | sent message_id=43
21:34:14 INFO  pyclaudir.cc       [CC.done]  action=stop reason=Answered the user's question
```

The `httpx`/`mcp` per-poll noise is silenced by default. To bring it back
for debugging, comment the relevant lines in `pyclaudir/__main__.py:_setup_logging()`.

### 2. The replayable session viewer (`pyclaudir.scripts.trace`)

Claude Code persists every CC session as a JSONL file at
`~/.claude/projects/-Users-rustam-z-development-agents-yalla/<session_id>.jsonl`.
This is the **complete conversation log** — every user envelope, every
assistant message, every tool_use, every tool_result, every thinking block.

Render it as a human-readable transcript:

```bash
# List every session in the project dir; the bot's file is marked
uv run python -m pyclaudir.scripts.trace --list

# Replay the bot's session (resolved via data/session_id, NOT
# "most-recent-file" — important if you also have your own Claude Code
# session running in the same cwd)
uv run python -m pyclaudir.scripts.trace

# Replay one specific session
uv run python -m pyclaudir.scripts.trace --session 87f472fa-5e1a-48d6-bddc-824efca1fea5

# Tail the bot's running session live (refreshes every 0.5s)
uv run python -m pyclaudir.scripts.trace --follow

# Truncate huge text blocks
uv run python -m pyclaudir.scripts.trace --max 200

# Escape hatch: pick the most-recently-modified JSONL regardless of owner
uv run python -m pyclaudir.scripts.trace --latest --follow
```

The default picker reads `data/session_id` first, then falls back to
fingerprinting (a session is "the bot's" iff its first user event begins
with the engine's `<msg ...>` XML envelope). This stops the renderer from
accidentally tailing your own Claude Code session that happens to be the
most recently modified file in the same project directory.

The renderer is **read-only** and never touches the running pyclaudir
process — totally safe to run in a second terminal while the bot is live.

### 3. The raw wire-stream capture (`data/cc_logs/`)

Independent from Claude Code's own session JSONL, pyclaudir also captures
the raw bytes coming out of the CC subprocess on stdout/stderr to:

```
data/cc_logs/<session_id>.stream.jsonl   # one event per line, pre-parse
data/cc_logs/<session_id>.stderr.log     # timestamped stderr lines
```

This is the **wire log** (what came out of the subprocess) as opposed to
the *conversation log* (what was in the model's context). The two overlap
mostly but the wire log also captures `result` events, `ping` frames, and
any malformed JSON the parser would otherwise drop. Useful when debugging
parser bugs or weird stream artifacts.

```bash
# Live wire stream
tail -f data/cc_logs/*.stream.jsonl | jq -c .

# CC's stderr (rate-limit notices, retries, warnings)
tail -f data/cc_logs/*.stderr.log
```

Capture is on by default. Files rotate per session id, append across
respawns of the same session, and survive crashes.

### 4. SQLite — auditable, queryable history

Everything that touches Telegram or any MCP tool is in `data/pyclaudir.db`.
Useful one-liners:

```bash
# Last 10 messages in/out (from any chat)
sqlite3 data/pyclaudir.db \
  "SELECT direction, chat_id, user_id, substr(text,1,80) AS text
   FROM messages ORDER BY timestamp DESC LIMIT 10;"

# Every MCP tool call the bot has made (newest first)
sqlite3 data/pyclaudir.db \
  "SELECT created_at, tool_name, duration_ms, error
   FROM tool_calls ORDER BY id DESC LIMIT 20;"

# Per-user activity in a specific chat
sqlite3 data/pyclaudir.db \
  "SELECT username, first_name, message_count, last_message_date
   FROM users WHERE chat_id = 587272213 ORDER BY message_count DESC;"

# Find every reply chain involving a specific user
sqlite3 data/pyclaudir.db \
  "SELECT message_id, reply_to_id, substr(text,1,100)
   FROM messages WHERE user_id = 587272213 AND reply_to_id IS NOT NULL;"
```

`query_db` (the MCP tool) lets the agent run SELECTs against this
same database — sqlglot-validated, capped at 100 rows.

### 5. Bonus — interactive replay (`claude --resume`)

Drop into the bot's *exact* conversation state in a real Claude Code
interactive session:

```bash
# Stop pyclaudir first, OR use --fork-session to branch safely
claude --resume $(cat data/session_id)
```

You're now talking to Claude Code with the bot's full history loaded. Ask
"why did you reply that way to message 591?" and you'll get its
perspective on its own past turns. ⚠️ Don't run this on the same session
id as a live pyclaudir process unless you pass `--fork-session`.

### Cheatsheet

| You want to know… | Look at |
|---|---|
| Who said what to who right now | the foreground terminal (`[RX]`/`[TX]` lines) |
| Which tools is it calling and why | the foreground terminal (`[CC.tool→]`/`[CC.done]` lines) |
| The full story of a past conversation | `python -m pyclaudir.scripts.trace --session <sid>` |
| Whether the parser is missing events | `data/cc_logs/<sid>.stream.jsonl` |
| Whether CC is hitting rate limits | `data/cc_logs/<sid>.stderr.log` |
| Aggregate stats / cross-session queries | `sqlite3 data/pyclaudir.db` |
| What it would say *now* about its own history | `claude --resume $(cat data/session_id) --fork-session` |

## Security model

The agent is a *front-facing public agent*. Anyone in an allowed chat can talk
to it, and they're not always trustworthy. The security model is enforced
by code, not by hope, and tested in `tests/test_security_invariants.py`.

- **No shell, no edits, no writes outside `memories/`, no general reads
  outside `memories/`.** The CC subprocess is spawned with
  `--allowedTools mcp__pyclaudir,WebFetch,WebSearch
  --disallowedTools Bash,Edit,Write,Read,NotebookEdit
  --strict-mcp-config`. The forbidden flag `--dangerously-skip-permissions`
  is *never* passed; both the argv builder and the spawn-time assertion
  refuse it.
- **Web access (read-only).** `WebFetch` and `WebSearch` are deliberately
  enabled so the agent can answer questions that need fresh information.
  This is a real trade-off — see the next bullet. The system prompt
  instructs the agent to refuse private/internal URLs (localhost, RFC1918,
  link-local, `.local`), but a determined prompt-injection could still
  get it to fetch one. **Do not deploy the bot on a host with sensitive
  internal endpoints reachable from the same network.**
- **MCP namespace lockdown.** The local MCP server is registered as
  `pyclaudir`, so every pyclaudir tool Claude sees is named
  `mcp__pyclaudir__<x>`. The two web tools are Claude Code built-ins, not
  MCP tools, so they show up unprefixed (`WebFetch`, `WebSearch`).
- **Memory writes with safety rails.** `write_memory` and `append_memory`
  exist, but are guarded by:
  - **Path traversal hardening** (no `..`, no absolute paths, no symlinks)
    — applies to writes the same way it applies to reads.
  - **64 KiB per-file size cap** — both writes and post-append totals.
  - **Read-before-write** — overwriting or appending to an *existing*
    file requires `read_memory` to have been called on it first in the
    same session. New files are exempt. The set of "read paths" resets
    on every restart so a fresh process must re-read before mutating.
  - **No deletion tool** — forgetting requires explicit overwriting.
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
  DB-backed (`rate_limits` table, fixed-minute buckets) so it survives
  restarts. Enforced across every outbound tool: `send_message`,
  `reply_to_message`, `edit_message`, `delete_message`, `add_reaction`.
  When a chat exhausts its bucket the user gets a single throttle notice
  (bypassing the limiter via a one-shot flag), then the bot goes quiet
  until the bucket rolls over.
- **Audit log.** Every MCP tool invocation persists to `tool_calls` (name,
  args, result, error, duration).

If you weaken any of these, the security tests will fail loudly. They are
load-bearing — keep them.

## Manual end-to-end checklist

Once configured, you should be able to:

1. DM the bot, see the bot reply via `send_message`.
2. Drop `data/memories/user_preferences.md` containing "Rustam prefers
   Russian", ask "what do you know about me?", watch it call
   `list_memories` → `read_memory` and reply in Russian.
3. Send 5 messages in 2 seconds, see them batched into one turn (debounce).
4. Send a 6th message *while* it's mid-turn, see it injected.
5. `sqlite3 data/pyclaudir.db 'SELECT direction, text FROM messages ORDER BY timestamp DESC LIMIT 10;'`
6. Drop `pyclaudir/tools/echo.py` (above), restart, and watch the bot gain
   the new tool with zero other code changes.
7. `kill -9 $(pgrep -f 'claude --print')`, watch the worker respawn within
   seconds and resume the conversation.
8. Ask the bot to run a shell command — it should refuse, because it has
   no `Bash` tool and its system prompt tells it to.
9. Run `uv run pytest tests/test_security_invariants.py` and see all 8
   invariants pass.

## Docker deployment

pyclaudir ships with Docker support for deploying to a VPS.

### Prerequisites

1. A VPS with Docker installed (Hetzner, DigitalOcean, Linode, etc.)
2. Claude Code CLI authenticated on the host (one-time):
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude   # interactive login — creates ~/.claude/
   ```

### Deploy

```bash
# Clone and configure
git clone <repo> ~/pyclaudir && cd ~/pyclaudir
cp .env.example .env
vim .env   # set TELEGRAM_BOT_TOKEN, PYCLAUDIR_OWNER_ID, etc.
cp prompts/project.md.example prompts/project.md
vim prompts/project.md   # customize identity, integrations, team info

# Build and start
docker compose up -d

# Monitor
docker compose ps
docker compose logs -f

# Go inside container
docker compose exec pyclaudir bash

# Stop pyclaudir
docker compose down
```

### Volumes

| Mount | Container path | What it holds |
|-------|---------------|---------------|
| `./data` | `/app/data` | SQLite DB, memories, access.json, session_id, cc_logs |
| `./prompts/project.md` | `/app/prompts/project.md` | Project-specific prompt |
| `~/.claude` | `/root/.claude` | CC auth config + session JSONL files |

### Syncing memories

Use the included sync script to keep memory files and project config
in sync between your local machine and the server:

```bash
# Pull memories, DB, and access.json from server to local
./scripts/sync-memories.sh pull user@server

# Push project.md, memories, and access.json from local to server
./scripts/sync-memories.sh push user@server
```

After pushing `project.md`, restart the container for changes to take
effect: `ssh user@server 'cd ~/pyclaudir && docker compose restart'`

### Migrating from local

If you've been running pyclaudir locally and want to move to a server:

```bash
# Copy your existing data and config
scp -r ./data user@server:~/pyclaudir/data/
scp ./prompts/project.md user@server:~/pyclaudir/prompts/
scp .env user@server:~/pyclaudir/.env

# Then on the server
cd ~/pyclaudir && docker compose up -d
```

## Layout

```
pyclaudir/
├── pyproject.toml
├── README.md
├── Dockerfile
├── docker-compose.yml
├── prompts/
│   ├── system.md               # generic pyclaudir system prompt (shipped)
│   ├── project.md              # project-specific overlay (gitignored)
│   └── project.md.example      # template for project.md
├── data/                       # gitignored
│   ├── pyclaudir.db            # SQLite (messages, users, tool_calls, ...)
│   ├── access.json             # DM policy + allowed users/chats (hot-reloaded)
│   ├── session_id              # CC session id for --resume
│   ├── memories/               # the agent's working memory
│   └── cc_logs/                # raw CC stdout/stderr capture
├── scripts/
│   └── sync-memories.sh        # rsync helper for server ↔ local sync
├── pyclaudir/
│   ├── __main__.py             # entrypoint + log setup
│   ├── access.py               # hot-reloadable access.json gate
│   ├── config.py
│   ├── db/{database.py,messages.py,reminders.py,migrations/}
│   ├── telegram_io.py
│   ├── engine.py               # debouncer, queue, inject, control loop
│   ├── cc_worker.py            # subprocess + raw capture + crash recovery
│   ├── cc_schema.py            # ControlAction JSON schema
│   ├── mcp_server.py           # FastMCP host + tool auto-discovery
│   ├── memory_store.py         # path-hardened read-only file store
│   ├── rate_limiter.py
│   ├── transcript.py           # [RX]/[TX]/[CC.*] log helpers
│   ├── models.py
│   ├── scripts/
│   │   └── trace.py            # CC session JSONL replay/follow renderer
│   └── tools/
│       ├── base.py             # BaseTool, ToolContext, Heartbeat
│       ├── now.py
│       ├── send_message.py
│       ├── reply_to_message.py
│       ├── edit_message.py
│       ├── delete_message.py
│       ├── add_reaction.py
│       ├── memory.py           # list/read/write/append memory (read-before-write)
│       ├── query_db.py
│       └── reminder.py         # set/list/cancel reminders
└── tests/                      # 193 tests
    ├── test_db_schema.py
    ├── test_mcp_server.py
    ├── test_tool_discovery.py
    ├── test_memory_path_safety.py
    ├── test_security_invariants.py     # 8 invariants (#3 has 3 sub-tests)
    ├── test_access.py                   # gate(), hot-reload, atomic writes
    ├── test_memory_writes.py            # write_memory + append_memory + read-before-write
    ├── test_telegram_persistence.py
    ├── test_cc_worker_argv.py
    ├── test_cc_raw_capture.py          # raw stdout/stderr capture
    ├── test_engine_debouncer.py
    ├── test_inject_and_dropped_text.py
    ├── test_recovery_and_limits.py
    ├── test_reactions_update.py       # inbound + bot reactions fold into messages
    ├── test_reply_chain.py             # multi-hop reply expansion
    ├── test_transcript.py              # tagged log formatting
    └── test_query_db.py
```
