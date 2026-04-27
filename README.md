# pyclaudir

**Run personal AI assistant in Telegram. With your rules.**

Most AI tools wait. Pyclaudir doesn't. It lives in your Telegram, remembers every conversation, and pings you the moment something matters — a trend turning, a Jira ticket due, a build that broke at 3am. 

Brief it before bed: "research global tech trends, find problems worth solving in Uzbekistan, write the analysis and ship a prototype." Wake up to a report, a pitch, and a working demo. That's the difference between a chatbot and an assistant.

It learns you. Every day it reflects on what worked and writes new rules into its own playbook — with your approval. Drop it in a group chat and it tracks who's blocked, what shipped, what slipped. DM it and it's your personal chief of staff. Plain markdown files you actually own. Your rules. One process you control end to end.

Out of the box: messaging, memory, reminders, web, vision. Want shell access? Code editing? Jira, GitHub, GitLab? One env var each, off until you flip them. Runs on your laptop or any small VPS. Works with your existing Claude Code subscription.

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
`Luna` in `prompts/project.md`):

- *"@Luna every weekday at 9am, read the last 24h of our team chat and DM me a 5-bullet status."* — uses `set_reminder` + `query_db` + `send_message`. Ships on by default.
- *"@Luna every Monday at 8am, pull top AI stories from Hacker News and TechCrunch and message me a briefing."* — `set_reminder` + `WebFetch` + `WebSearch`. Default tools.
- *"@Luna each evening at 9pm, ask me what I shipped today and append it to my journal."* — appends to `data/memories/journal.md` via `append_memory`. Default tools.
- *"@Luna remind me to take meds at 9pm daily, and nag me if I don't react with 👍 within 10 min."* — `set_reminder` + `add_reaction` + `query_db` to check the reaction.
- *"@Luna watch https://example.com/changelog hourly and ping me the moment a new entry mentions 'pricing'."* — cron `set_reminder` + `WebFetch`. Diff state lives in a memory file.
- *"@Luna every Friday at 5pm, review this week's git log on `~/code/myapp` and open a PR if the README has drifted."* — needs `tool_groups.bash: true` and `tool_groups.code: true` in `plugins.json`, plus `GITHUB_PERSONAL_ACCESS_TOKEN` in `.env` for the PR.
- *"@Luna every morning at 7am, DM me my Jira tickets due this week, grouped by project."* — needs `JIRA_*` env vars.
- *"@Luna poll the group on lunch spots — Ramen, Burrito, Salad — and message me the result at 11:45."* — `create_poll` + `set_reminder` + `stop_poll`. Default tools.

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

cp plugins.json.example plugins.json && nano plugins.json
#   single source of truth for the bot's capability surface — see below

docker compose up -d --build
docker compose logs -f                                                    # run harness, wait for "pyclaudir is live"
docker compose exec pyclaudir python -m pyclaudir.scripts.trace --follow  # tail Claude Code I/O
```

```bash
# Or run without docker
uv run python -m pyclaudir                          # run harness, wait for "pyclaudir is live"
uv run python -m pyclaudir.scripts.trace --follow   # tail Claude Code I/O
```

DM your bot. It replies.

**No Docker?** `uv sync --extra dev && uv run python -m pyclaudir`.
You need Python 3.11+ and the Claude Code CLI (`claude --version`).
If you have Windows machine, then use docker compose.

### The three setup files

| File | Tracked in git? | What it controls |
|---|---|---|
| `.env` | no | secrets — Telegram bot token, owner id, integration credentials (Jira / GitLab / GitHub) referenced by `plugins.json` via `${VAR}` |
| `prompts/project.md` | no | persona — bot name, language, house rules, owner-specific instructions; appended to the shipped `prompts/system.md` |
| `plugins.json` | no | capability surface — what tools, skills, and MCPs are on |

`.env.example`, `prompts/project.md.example`, and `plugins.json.example` are tracked so you have a starting point; the real files (the three above) are gitignored so different deployments carry different config without fighting over the file.

### What `plugins.json` controls

One file, four blocks. Edit and restart to apply.

```jsonc
{
  "tool_groups": {           // dangerous Claude Code built-ins, all off by default
    "bash":      false,      //   Bash, PowerShell, Monitor — shell execution
    "code":      false,      //   Edit, Write, Read, NotebookEdit, Glob, Grep, LSP
    "subagents": false       //   Agent — token-heavy, isolated context
  },
  "mcps": [                  // external MCP servers — stdio, http, or sse
    {                        //   stdio (local subprocess; auth via env)
      "name": "github",
      "type": "stdio",       //   optional; "stdio" is the default
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env":  { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}" },
      "allowed_tools": ["mcp__github"],
      "enabled": true
    },
    {                        //   http (remote server; auth via static headers)
      "name": "linear",
      "type": "http",
      "url": "https://mcp.linear.app/mcp",
      "headers": { "Authorization": "Bearer ${LINEAR_API_KEY}" },
      "allowed_tools": ["mcp__linear"],
      "enabled": true
    }
    // …Notion, Slack, Postgres, Playwright, your own — same shape; sse also supported
  ],
  "builtin_tools_disabled": [ // pyclaudir built-ins to hide from the agent
    // e.g. "create_poll", "stop_poll", "render_html", "render_latex", "send_photo"
  ],
  "skills_disabled": [       // skill directories under skills/ to hide
    // e.g. "render-style"
  ]
}
```

- **Tool groups.** Claude Code's dangerous built-ins (shell / code edit / subagents). All off by default. Flip to `true` and restart to unlock.
- **External MCPs.** Three transports supported, exactly as the [MCP spec](https://modelcontextprotocol.io) defines them: `stdio` (local subprocess, auth via `env`), `http` (remote streamable HTTP, auth via static `headers`), and `sse` (Server-Sent Events, same field shape as http). `${VAR}` references pull credentials from `.env`; if any required var is empty the MCP is silently skipped at boot. To stop advertising one without removing credentials, flip `"enabled": false`. Adding a new MCP (Linear, Notion, Slack, your own) is just a new array entry — no Python. Pyclaudir doesn't manage OAuth flows; supply an already-issued token via `${VAR}`.
- **Built-in tool toggles.** Names of pyclaudir built-ins (e.g. `create_poll`, `render_latex`) you want hidden. Filtered at MCP registration — the agent literally can't see them. A typo crashes boot with the available list.
- **Skill toggles.** Directory names under `skills/` to hide. The skill stays on disk but isn't listed or readable, so it can't be invoked.

A missing `plugins.json` boots locked-down (no integrations, no tool groups). A malformed file crashes boot loudly. Full schema reference: [docs/tools.md](docs/tools.md).

> **This README is the high-level intro.** Deeper material lives in
> [docs/](docs/) — full technical manual, deployment walkthrough, tools
> reference, and the systems pyclaudir descends from. Start at
> [docs/README.md](docs/README.md).

## What pyclaudir can do

**communication:** send / reply / edit / delete text, emoji reactions, polls (regular + quiz, multi-answer, auto-close).

**media:** render HTML to PNG (tables, charts, diffs — Chart.js / D3 inline) and LaTeX to PNG (math via KaTeX), send back as inline photos. Read inbound photos (vision), text-like docs (md / txt / log / csv / json / yaml / code …), and PDFs (extracted text with `--- page N ---` markers).

**memory:** persistent markdown files under `data/memories/` (list / read / write / append / send-as-document), 64 KiB per file, read-before-write rail, survives restarts. Per-user / per-group / journal layout.

**search & history:** web search and web fetch (no internal / RFC1918 URLs). Read-only SQL SELECTs on the chat database (`messages`, `users`, `reminders`, ≤100 rows). Multi-hop reply-chain expansion.

**scheduling:** one-shot + cron-recurring reminders. Auto-seeded daily self-reflection skill that promotes corrections into durable rules with owner approval.

**self-edit:** append rules to `prompts/project.md` (owner-only); shipped `system.md` is git-tracked and not exposed.

**skills:** read operator-curated playbooks under `skills/` — `render-style` (house style for renders), `self-reflection` (learning loop). Reference skills are read on initiative; invoked skills require a real `<reminder>` envelope.

**opt-in:** shell (`Bash` / `PowerShell` / `Monitor`), code editing (`Edit` / `Write` / `Read` / `NotebookEdit` / `Glob` / `Grep` / `LSP`), subagents (`Agent`), and Jira / GitLab / GitHub MCP surfaces — all toggled in `plugins.json`. Credentials for the integrations live in `.env` and are pulled in via `${VAR}` references. Off by default.

**what can't do:** generate images. Send voice messages, GIFs, animations, stickers. Read voice / video / video notes / stickers (they arrive empty — ask for a screenshot or description). Moderate (mute / ban / kick / unban / member lists). Make phone calls or watch videos.

Per-tool descriptions, the `plugins.json` schema, and how to add a new MCP / disable a built-in tool / hide a skill: [docs/tools.md](docs/tools.md).

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

**Tool-surface toggles** (subagents, shell, code editing, built-in
tools, MCPs, skills) live in [`plugins.json`](plugins.json.example),
not in `.env`. Copy `plugins.json.example` → `plugins.json` once and
edit; restart to apply. See [docs/tools.md](docs/tools.md) for the
schema.

Credentials for the integration MCPs ship in `.env` and are pulled
into `plugins.json` via `${VAR}` references:

| Variable | Required | Notes |
|---|---|---|
| `JIRA_URL` + `JIRA_USERNAME` + `JIRA_API_TOKEN` | no | credentials for Jira via [mcp-atlassian](https://github.com/sooperset/mcp-atlassian); the `mcp-atlassian` entry in `plugins.json` references them via `${VAR}` |
| `GITLAB_URL` + `GITLAB_TOKEN` | no | credentials for GitLab via [@zereight/mcp-gitlab](https://www.npmjs.com/package/@zereight/mcp-gitlab); referenced from `plugins.json` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | no | credentials for GitHub via [@modelcontextprotocol/server-github](https://www.npmjs.com/package/@modelcontextprotocol/server-github); referenced from `plugins.json`. For Enterprise, add `GITHUB_HOST` to the entry's `env` block. |

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
- **Plug in an external MCP server.** Append an entry to
  [`plugins.json`](plugins.json) with `name`, `command`, `args`,
  `env` (with `${VAR}` interpolation from `.env`), and the
  `allowed_tools` list. Jira, GitLab, and GitHub ship in the default
  file — any stdio MCP server (Notion, Linear, Slack, Postgres,
  Playwright, your own) drops in the same way. No Python edit. To
  hide a wired-in MCP without removing credentials, flip
  `enabled: false` on its entry.
- **Disable a built-in tool you don't use.** List it in
  `builtin_tools_disabled` in `plugins.json` (e.g. `create_poll`,
  `stop_poll`, `render_html`, `render_latex`, `send_photo`). The
  tool is filtered at MCP registration time — the model can't see or
  invoke it. Trims the surface area without code changes.
- **Add a skill.** Drop a playbook at `skills/<name>/SKILL.md` (see
  [Agent Skills spec](https://agentskills.io/specification)) and the
  bot reads it on demand. Two ship today: `self-reflection` (daily
  learning loop) and `render-style` (house style for `render_html`).
  To hide one without deleting it, list its directory name in
  `skills_disabled` in `plugins.json`.
- **Reshape the persona.** Name, voice, language, house rules,
  default behaviours — all live in `prompts/project.md`. Edit and
  restart; no code change.
- **Run a fleet.** One process is one bot. Want a per-team bot,
  per-project bot, work/personal split? Run multiple instances with
  different `.env` files and `PYCLAUDIR_DATA_DIR` paths — they share
  nothing.

**Observability** — live tagged log (`[RX]` / `[TX]` / `[CC.tool→]`),
human-readable session replay (`uv run python -m pyclaudir.scripts.trace --follow`;
add `--session <id>` to pin one), raw wire log in
`data/cc_logs/<session>.stream.jsonl`, plus `sqlite3 data/pyclaudir.db`
for cross-session queries. Talk to the bot's own session interactively
with `claude --resume $(cat data/session_id) --fork-session`.

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

- **Run the suite** — `uv run python -m pytest -q`. 346 tests today;
  keep them green.
- **Persona-agnostic code.** Don't hardcode bot names, owner-specific
  strings, or chat ids in `pyclaudir/`. Persona lives in
  `prompts/project.md` and stays there.
- **Default surface stays tight.** The bot ships off-by-default for
  shell, code editing, subagents, and the integration MCPs (Jira /
  GitLab / GitHub spawn only when their credentials are set). New
  capabilities follow the same rule — gated behind a `tool_groups`
  flag in `plugins.json` or behind a credentialled `mcps[]` entry,
  unless they're strictly safer than the current base.

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
