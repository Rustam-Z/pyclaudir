# pyclaudir

**Your personal AI agent. In telegram. With your rules.**

This is agent, dropped straight into your Telegram, running on your machine.
It DMs you. Sits in your group chats. Takes notes in plain markdown files you actually own. Schedules reminders. 
Reads back what you said last Tuesday. Name it, pick a voice, set a language — all in `prompts/project.md`. Configure it once. Ship it. It's yours.

Out of the box it's clean: messaging in DM & group chats, memory, reminders & scheduled tasks, web access, security.
Want shell access? Code editing? Subagents? GitLab? GitHub? Jira? One env var each. Off until you flip them. Full list in [docs/tools.md](docs/tools.md).

**One process, no magic.** A Python harness that listens Telegram, runs `claude` as a subprocess (via a local MCP server), and posts the reply back. 
Main work: message-as-stimulus loop, MCP tool host, persistent memory, multi-bot orchestration, harness logic.
You control who can talk to it (just you, or group chats, or anyone). 
Runs on your laptop or any small VPS. 
Can be used with your existing Claude Code subscription.

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

### Try saying

Concrete one-liners you can DM the bot today (assumes you named it
`Nodira` in `prompts/project.md`):

- *"@Nodira every weekday at 9am, read the last 24h of our team chat and DM me a 5-bullet status."* — uses `set_reminder` + `query_db` + `send_message`. Ships on by default.
- *"@Nodira every Monday at 8am, pull top AI stories from Hacker News and TechCrunch and message me a briefing."* — `set_reminder` + `WebFetch` + `WebSearch`. Default tools.
- *"@Nodira each evening at 9pm, ask me what I shipped today and append it to my journal."* — appends to `data/memories/journal.md` via `append_memory`. Default tools.
- *"@Nodira remind me to take meds at 9pm daily, and nag me if I don't react with 👍 within 10 min."* — `set_reminder` + `add_reaction` + `query_db` to check the reaction.
- *"@Nodira watch https://example.com/changelog hourly and ping me the moment a new entry mentions 'pricing'."* — cron `set_reminder` + `WebFetch`. Diff state lives in a memory file.
- *"@Nodira every Friday at 5pm, review this week's git log on `~/code/myapp` and open a PR if the README has drifted."* — needs `PYCLAUDIR_ENABLE_BASH=true` and `PYCLAUDIR_ENABLE_CODE=true`, plus `GITHUB_PERSONAL_ACCESS_TOKEN` for the PR.
- *"@Nodira every morning at 7am, DM me my Jira tickets due this week, grouped by project."* — needs `JIRA_*` env vars.
- *"@Nodira poll the group on lunch spots — Ramen, Burrito, Salad — and message me the result at 11:45."* — `create_poll` + `set_reminder` + `stop_poll`. Default tools.

The pattern: you describe the *outcome* in chat, the bot picks the
tools and schedules itself. No YAML, no cron syntax to memorise.

<!-- TODO: 30s GIF demo for the README header once we have one -->

## Quickstart (3 minutes)

```bash
git clone https://github.com/Rustam-Z/pyclaudir && cd pyclaudir

cp .env.example .env && nano .env
#   set TELEGRAM_BOT_TOKEN  (from @BotFather)
#   set PYCLAUDIR_OWNER_ID  (your numeric Telegram user id, from @userinfobot)
#   update if necessary: PYCLAUDIR_MODEL and PYCLAUDIR_EFFORT

cp prompts/project.md.example prompts/project.md && nano prompts/project.md
#   set bot name, language, personality

docker compose up -d --build
docker compose logs -f   # wait for "pyclaudir is live"
```

DM your bot. It replies.

**No Docker?** `uv sync --extra dev && uv run python -m pyclaudir`.
You need Python 3.11+ and the Claude Code CLI (`claude --version`).
If you have Windows machine, then use docker compose. 

> **This README is the high-level intro.** Deeper material lives in
> [docs/](docs/) — full technical manual, deployment walkthrough, tools
> reference, and the systems pyclaudir descends from. Start at
> [docs/README.md](docs/README.md).

## What's inside

| Capability | Tools | On by default? |
|---|---|---|
| Telegram messaging | `send_message`, `reply_to_message`, `edit_message`, `delete_message`, `add_reaction`, `create_poll`, `stop_poll` | yes |
| Inbound attachments (photos + documents) | `read_attachment` (path-scoped to `data/attachments/`) | yes |
| Rendered visuals (tables, charts, diffs) | `render_html` (headless Chromium → PNG, network blocked), `send_photo` | yes |
| Memory (markdown files in `data/memories/`) | `list_memories`, `read_memory`, `write_memory`, `append_memory`, `send_memory_document` | yes |
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
`allowed_chats`. `allowlist` is only for exclusive users in `allowed_users`, not groups.
Owner-only commands (silent for non-owners):
`/access`, `/allow`, `/deny`, `/dmpolicy`, `/kill`, `/health`, `/audit`.
Details: [docs/documentation.md](docs/documentation.md).

## Extending

Almost every axis of the bot is pluggable without touching the core:

- **Add a built-in tool.** Drop a `BaseTool` subclass into
  [pyclaudir/tools/](pyclaudir/tools/) — one file, Pydantic args
  model, async `run`. The local MCP server auto-discovers it on
  restart. No registry edits, no wiring.
- **Plug in an external MCP server.** Jira, GitLab, and GitHub are
  already wired this way — flip an env var and the community MCP
  server is spawned alongside ours and merged into the agent's tool
  surface. Any stdio MCP server (Notion, Linear, Slack, Postgres,
  Playwright, your own) drops in the same way. Pattern lives in
  [pyclaudir/__main__.py](pyclaudir/__main__.py).
- **Add a skill.** Drop a playbook at `skills/<name>/SKILL.md` (see
  [Agent Skills spec](https://agentskills.io/specification)) and the
  bot reads it on demand. Two ship today: `self-reflection` (daily
  learning loop) and `render-style` (house style for `render_html`).
- **Reshape the persona.** Name, voice, language, house rules,
  default behaviours — all live in `prompts/project.md`. Edit and
  restart; no code change.
- **Run a fleet.** One process is one bot. Want a per-team bot,
  per-project bot, work/personal split? Run multiple instances with
  different `.env` files and `PYCLAUDIR_DATA_DIR` paths — they share
  nothing.

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

- **Run the suite** — `uv run python -m pytest -q`. 342 tests today;
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
pyclaudir/
├── prompts/        system.md (shipped) + project.md (yours)
├── skills/         operator-curated playbooks
├── data/           gitignored — SQLite, memories, attachments, renders, CC logs
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
