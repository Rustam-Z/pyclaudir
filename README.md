# pyclaudir

**Your own AI assistant inside Telegram, powered by Claude.**

pyclaudir turns Claude into a Telegram bot you control. DM it like you'd
DM a friend. Add it to group chats. Tell it your name, your tone, the
language you prefer — it remembers, because it keeps notes in plain
markdown files on your machine (`data/memories/`). Ask it to remind you
about something next Tuesday and it will. Ask it what you said last week
and it can look it up in its own message log.

**How it works, briefly.** One Python program runs in the background. It
listens to Telegram, hands incoming messages to a real `claude` process
running on your machine, and posts the reply back. The bot can only do
the few things you give it tools for: send messages, take notes, set
reminders, search the web. It **cannot** run shell commands, edit your
files, or read random files on your computer — those are turned off on
purpose, so it stays safe to leave running. But you can run on if you would like to.

You decide who can talk to it: just you, a list of friends, or anyone.
You can run it on your laptop, or put it on a small server so it's
always online.

> **This README is the high-level intro.** Everything else lives in
> [docs/](docs/) — full technical manual, deployment walkthrough, and
> notes on the systems pyclaudir descends from. See
> [docs/README.md](docs/README.md) for an index.

## Quickstart

You need Python 3.11+, [`uv`](https://github.com/astral-sh/uv), and the Claude
Code CLI (`claude --version`).

```bash
# 1. Get a bot token from @BotFather and your numeric user id from @userinfobot

# 2. Configure
cp .env.example .env
$EDITOR .env   # set TELEGRAM_BOT_TOKEN and PYCLAUDIR_OWNER_ID

# 3. Install + run
uv sync --extra dev
uv run python -m pyclaudir
```

DM your bot. It should reply.

Stop with Ctrl+C, or `pkill -f 'python -m pyclaudir'` if it's running in the
background. Only one instance can poll a given bot token at a time.

## Tests

```bash
uv run python -m pytest -q
```

Always go through `python -m pytest`, not bare `pytest` — the bare form can
pick up a system pytest that's missing this project's deps.

## Config

All settings come from environment variables (or `.env`). The full list lives
in [pyclaudir/config.py](pyclaudir/config.py). The ones you'll actually touch:

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `PYCLAUDIR_OWNER_ID` | yes | your numeric Telegram user id |
| `PYCLAUDIR_MODEL` | yes | e.g. `claude-opus-4-6` |
| `PYCLAUDIR_EFFORT` | yes | `low` / `medium` / `high` / `max` |
| `PYCLAUDIR_DATA_DIR` | no | defaults to `./data` |
| `PYCLAUDIR_ENABLE_SUBAGENTS` | no | off by default — subagents burn tokens |

Optional integrations: `JIRA_URL` + `JIRA_USERNAME` + `JIRA_API_TOKEN` turns on
[mcp-atlassian](https://github.com/sooperset/mcp-atlassian); `GITLAB_URL` +
`GITLAB_TOKEN` turns on [@zereight/mcp-gitlab](https://www.npmjs.com/package/@zereight/mcp-gitlab).

## How it works

Four parts, one Python process:

```
Telegram  →  Engine (buffer + debounce)  →  Claude worker  →  claude process
                       │                                            │
                       ▼                                            ▼
                    SQLite                                   Local MCP server
```

- **Telegram listener** reads messages, saves them to SQLite, hands them off.
- **Engine** bundles messages that arrive close together. If a new one arrives
  while Claude is mid-reply, it's injected into the running turn.
- **Claude worker** runs the `claude` subprocess and restarts it on crash.
- **MCP server** auto-loads every tool in [pyclaudir/tools/](pyclaudir/tools/).

The engine handles **one turn at a time**. A long task in chat A delays
chat B until it finishes. Fine for one user; for busy setups, run a separate
bot per chat group.

## Adding a tool

Drop one file in [pyclaudir/tools/](pyclaudir/tools/), no other code changes:

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

Restart the bot. The tool is live.

## Access

Who can talk to the bot lives in `data/access.json` (hot-reloaded — edits
take effect on the next message):

```json
{
  "dm_policy": "owner_only",
  "allowed_users": [],
  "allowed_chats": [-1001234567890]
}
```

DM policies: `owner_only` (default), `allowlist`, `open`. The owner is always
allowed. Groups must be in `allowed_chats`.

Owner-only commands (silently no-op for everyone else):

```
/access /allow /deny /dmpolicy   manage access
/kill                            graceful shutdown
/health /audit                   quick + detailed health
```

## Memory, reminders, skills

The bot keeps notes in `data/memories/*.md` via four tools (`list_memories`,
`read_memory`, `write_memory`, `append_memory`). It can't overwrite a file
without reading it first in the same session — this stops the bot from
clobbering notes you wrote but it never saw. There's no delete tool on
purpose; remove files yourself with `rm` if needed.

Reminders are stored in SQLite and injected back as synthetic messages when
they fire. All times are UTC.

Skills are operator-curated playbooks under [skills/](skills/) (see
[Agent Skills spec](https://agentskills.io/specification)). Each has a
`SKILL.md` with YAML frontmatter. The bot reads them on demand. The first
one is `self-reflection`, a daily loop that proposes new rules from observed
mistakes.

## Prompts

The system prompt is two files:

- [prompts/system.md](prompts/system.md) — generic pyclaudir behavior (shipped)
- `prompts/project.md` — your overlay (gitignored; copy from
  `prompts/project.md.example`)

## Watching the bot

Four ways to see what's happening:

1. **Live terminal** — tagged `[RX]` / `[TX]` / `[CC.tool→]` lines.
2. **Replay a session** — `uv run python -m pyclaudir.scripts.trace --follow`.
3. **Raw wire log** — `data/cc_logs/<session>.stream.jsonl` and `.stderr.log`.
4. **SQLite** — `sqlite3 data/pyclaudir.db` for cross-session queries.

To talk to the bot's *own* Claude session interactively:
`claude --resume $(cat data/session_id) --fork-session`.

## Security

The bot is public-facing — anyone in an allowed chat can talk to it, and not
all of them are friendly. Enforced in code, tested in
[tests/test_security_invariants.py](tests/test_security_invariants.py):

- No shell, no file edits, no reads outside `data/memories/`. The CC
  subprocess is spawned with `--allowedTools mcp__pyclaudir,WebFetch,WebSearch`
  and `--disallowedTools Bash,Edit,Write,Read,NotebookEdit`.
- `WebFetch` and `WebSearch` are on (the bot needs fresh info). The system
  prompt blocks private/internal URLs, but a determined prompt-injection could
  still fetch one — **don't host pyclaudir next to sensitive internal endpoints**.
- Memory writes are guarded: path-hardened, 64 KiB cap, read-before-write.
- `query_db` is read-only (single SELECT, sqlglot-validated, 100-row cap).
- 20 DM/min per user (owner exempt, groups not limited).
- Inbound text is scrubbed for tokens/keys before hitting SQLite.
- Owner-only commands check `effective_user.id == PYCLAUDIR_OWNER_ID`.
- Wedged subprocesses are killed and respawned. Crash-loops give up after 10
  crashes in 10 min and notify the owner.

## Docker

```bash
git clone <repo> ~/pyclaudir && cd ~/pyclaudir
cp .env.example .env && $EDITOR .env
cp prompts/project.md.example prompts/project.md && $EDITOR prompts/project.md
docker compose up -d
docker compose logs -f
```

Volumes: `./data` (DB + memories), `./prompts/project.md`, `~/.claude` (CC
auth). [scripts/sync-memories.sh](scripts/sync-memories.sh) pushes/pulls
memory + access between local and server.

## Layout

```
pyclaudir/
├── prompts/        system.md (shipped) + project.md (yours)
├── skills/         operator-curated playbooks
├── data/           gitignored — SQLite, memories, session id, CC logs
├── scripts/        sync + maintenance helpers
├── pyclaudir/
│   ├── __main__.py        entrypoint
│   ├── config.py          single source of env vars
│   ├── telegram_io.py     listener
│   ├── engine.py          debounce + buffer + inject
│   ├── cc_worker.py       Claude subprocess + crash recovery
│   ├── mcp_server.py      FastMCP host + tool auto-discovery
│   ├── access.py          hot-reload access gate
│   ├── tools/             one file per tool
│   └── scripts/trace.py   replay a session
└── tests/
```
