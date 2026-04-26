# pyclaudir

**Your Telegram. Your Claude. Your rules.**

Claude, dropped straight into your Telegram, running on your machine.
It DMs you. Sits in your group chats. Takes notes in plain markdown
files you actually own. Schedules reminders. Reads back what you said
last Tuesday. Name it, pick a voice, set a language — all in
`prompts/project.md`. Configure it once. Ship it. It's yours.

Out of the box it's clean: messaging, notes, reminders, read-only web.
Want shell access? Code editing? Subagents? Jira? GitLab? GitHub? One
env var each. Off until you flip them. Full list in
[docs/tools.md](docs/tools.md).

**One process, no magic.** A Python program listens to Telegram, runs
`claude` as a subprocess (via a local MCP server), and posts the reply
back. You control who can talk to it (just you, or group chats, or anyone).
Runs on your laptop or any small VPS.

## What you can do with it

**Overnight engineer.** Brief it before bed — *"build a Stripe checkout
against a mock Customer table"*. Wake up to a pushed branch and a
working prototype.

**Market watch.** Point it at a topic, get pinged the second a trend
turns. Reads RSS, scrapes pages, summarises competitor release notes —
all from one Telegram thread. *Uses the always-on `WebFetch` and
`WebSearch`.*

**Team co-pilot for startups.** Your team already lives in Telegram —
now their AI does too. Drop the bot in the group chat and it tracks
who's blocked, what shipped, what slipped. Catch a bug
mid-conversation? Tell it — the Jira ticket files itself, with the
right repro steps and the right reporter. Need a stand-up summary?
It reads the last 24 hours of chat and writes one. Hand it the
backlog at 6am, get a clean board by 9. No context switch out of
Telegram, no extra dashboards to babysit.

**Personal assistant.** Reminders, notes, daily check-ins. Persistent
across sessions because everything writes to plain markdown files you
actually own. *Always on.*

<!-- TODO: 30s GIF demo for the README header once we have one -->

## Quickstart (3 minutes)

```bash
git clone https://github.com/Rustam-Z/pyclaudir-agents && cd pyclaudir-agents

cp .env.example .env && $EDITOR .env
#   set TELEGRAM_BOT_TOKEN  (from @BotFather)
#   set PYCLAUDIR_OWNER_ID  (your numeric Telegram user id, from @userinfobot)

cp prompts/project.md.example prompts/project.md && $EDITOR prompts/project.md
#   set bot name, language, personality

docker compose up -d --build
docker compose logs -f   # wait for "pyclaudir is live"
```

DM your bot. It replies.

**No Docker?** `uv sync --extra dev && uv run python -m pyclaudir`.
You need Python 3.11+ and the Claude Code CLI (`claude --version`).

> **This README is the high-level intro.** Deeper material lives in
> [docs/](docs/) — full technical manual, deployment walkthrough, tools
> reference, and the systems pyclaudir descends from. Start at
> [docs/README.md](docs/README.md).

## What's inside

| Capability | Tools | On by default? |
|---|---|---|
| Telegram messaging | `send_message`, `reply_to_message`, `edit_message`, `delete_message`, `add_reaction` | yes |
| Memory (markdown files in `data/memories/`) | `list_memories`, `read_memory`, `write_memory`, `append_memory` | yes |
| Reminders (one-shot + cron) | `set_reminder`, `list_reminders`, `cancel_reminder` | yes |
| Read its own message history | `query_db` (read-only SELECT, 100-row cap) | yes |
| Self-edit project prompt | `read_instructions`, `append_instructions` (owner-only) | yes |
| Web | `WebFetch`, `WebSearch` (private/internal URLs refused) | yes |
| Skills (operator-curated playbooks) | `list_skills`, `read_skill` | yes |
| Subagents | `Agent` | `PYCLAUDIR_ENABLE_SUBAGENTS=true` |
| Shell | `Bash`, `PowerShell`, `Monitor` | `PYCLAUDIR_ENABLE_BASH=true` |
| Code editing | `Edit`, `Write`, `Read`, `NotebookEdit`, `Glob`, `Grep`, `LSP` | `PYCLAUDIR_ENABLE_CODE=true` |
| Jira | 36 `mcp-atlassian` Jira tools | when `JIRA_*` env vars are set |
| GitLab | `mcp__mcp-gitlab` (full GitLab MCP surface) | when `GITLAB_*` env vars are set |
| GitHub | `mcp__github` (full GitHub MCP surface) | when `GITHUB_PERSONAL_ACCESS_TOKEN` is set |

Per-tool descriptions: [docs/tools.md](docs/tools.md).

## Architecture

```
Telegram  →  Engine (buffer + debounce)  →  Claude worker  →  claude process
                       │                                            │
                       ▼                                            ▼
                    SQLite                                   Local MCP server
```

- **Telegram listener** reads messages, saves them to SQLite, hands
  them off.
- **Engine** bundles messages that arrive close together. If a new
  one arrives while Claude is mid-reply, it's injected into the
  running turn.
- **Claude worker** runs the `claude` subprocess and restarts it on
  crash.
- **MCP server** auto-loads every tool in
  [pyclaudir/tools/](pyclaudir/tools/).

The engine handles **one turn at a time**. A long task in chat A
delays chat B until it finishes. Fine for one user; for busy setups,
run a separate bot per chat group.

The system prompt is two files: [prompts/system.md](prompts/system.md)
(generic pyclaudir behaviour, shipped) and `prompts/project.md`
(your overlay — gitignored, copy from
[prompts/project.md.example](prompts/project.md.example)).

## Configuration

All settings come from environment variables (or `.env`). Full list in
[pyclaudir/config.py](pyclaudir/config.py). The ones you'll touch:

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `PYCLAUDIR_OWNER_ID` | yes | your numeric Telegram user id |
| `PYCLAUDIR_MODEL` | yes | e.g. `claude-opus-4-6` |
| `PYCLAUDIR_EFFORT` | yes | `low` / `medium` / `high` / `max` |
| `PYCLAUDIR_DATA_DIR` | no | defaults to `./data` |
| `PYCLAUDIR_ENABLE_SUBAGENTS` | no | off by default — `Agent`, token-heavy |
| `PYCLAUDIR_ENABLE_BASH` | no | off — `Bash` / `PowerShell` / `Monitor` |
| `PYCLAUDIR_ENABLE_CODE` | no | off — `Edit` / `Write` / `Read` / `NotebookEdit` / `Glob` / `Grep` / `LSP` |
| `JIRA_URL` + `JIRA_USERNAME` + `JIRA_API_TOKEN` | no | turn on Jira via [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) |
| `GITLAB_URL` + `GITLAB_TOKEN` | no | turn on GitLab via [@zereight/mcp-gitlab](https://www.npmjs.com/package/@zereight/mcp-gitlab) |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | no | turn on GitHub via [@modelcontextprotocol/server-github](https://www.npmjs.com/package/@modelcontextprotocol/server-github); set `GITHUB_HOST` for Enterprise |

Access policy lives in `data/access.json` (hot-reloaded). DM policies:
`owner_only` (default), `allowlist`, `open`. Group chats must be in
`allowed_chats`. Owner-only commands (silent for non-owners):
`/access`, `/allow`, `/deny`, `/dmpolicy`, `/kill`, `/health`, `/audit`.
Details: [docs/documentation.md](docs/documentation.md).

## Extending

**Add a tool — one file, no other code changes:**

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

**Add a skill** — drop a playbook at `skills/<name>/SKILL.md`
(see [Agent Skills spec](https://agentskills.io/specification)). The
bot reads it on demand. The first one is `self-reflection`, a daily
loop that proposes new rules from observed mistakes.

**Observability** — live tagged log (`[RX]` / `[TX]` / `[CC.tool→]`),
session replay (`uv run python -m pyclaudir.scripts.trace --follow`),
raw wire log in `data/cc_logs/<session>.stream.jsonl`, plus
`sqlite3 data/pyclaudir.db` for cross-session queries. To talk to the
bot's own session interactively:
`claude --resume $(cat data/session_id) --fork-session`.

## Security

The bot is public-facing — anyone in an allowed chat can talk to it.
Enforced in code:

- **Tight default surface.** By default no shell, no file edits, no reads
  outside `data/memories/`, no subagents — by default. The CC
  subprocess is spawned with
  `--allowedTools mcp__pyclaudir,WebFetch,WebSearch` (plus opt-in
  groups when their env flag is set) and a deny list covering every
  gated tool. See [docs/tools.md](docs/tools.md).
- **Web access is read-only,** with private/internal URLs refused at
  the prompt layer. A determined prompt injection could still get
  one through — **don't host pyclaudir next to sensitive internal
  endpoints**.
- **Memory writes are guarded** — path-hardened, 64 KiB cap,
  read-before-write, no delete tool.
- **`query_db` is read-only** (single SELECT, sqlglot-validated,
  100-row cap).
- **Rate limit** of 20 DM/min per user (owner exempt; groups not
  limited).
- **Inbound text is scrubbed** for tokens/keys before hitting SQLite.
- **Owner-only commands** check `effective_user.id == PYCLAUDIR_OWNER_ID`.
- **Wedged subprocesses** are killed and respawned. Crash-loops give
  up after 10 crashes in 10 min and notify the owner.

## Contributing

Issues and PRs welcome. Three rules before you start:

- **Run the suite** — `uv run python -m pytest -q`. 312 tests today;
  keep them green.
- **Persona-agnostic code.** Don't hardcode bot names, owner-specific
  strings, or chat ids in `pyclaudir/`. Persona lives in
  `prompts/project.md` and stays there.
- **Default surface stays tight.** The bot ships off-by-default for
  shell, code editing, subagents, Jira, GitLab. New capabilities
  follow the same rule — gated behind `PYCLAUDIR_ENABLE_*` unless
  they're strictly safer than the current base.

Architecture deep-dive before bigger changes:
[docs/documentation.md](docs/documentation.md) and
[docs/reference-architectures.md](docs/reference-architectures.md).

## License

MIT. See [LICENSE](LICENSE).

## Layout

```
pyclaudir-agents/
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
