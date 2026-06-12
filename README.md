<p align="center">
  <img src="assets/pyclaudir-logo.jpg" alt="pyclaudir" width="600">
</p>

<p align="center">
  <b>pyclaudir</b> is a framework for running your own persistent AI companion on Telegram — one you fully own, control, can extend, one that learns from you.
</p>

---

> **Try it live:** a running instance lives in the [@rustamz_workshop](https://t.me/rustamz_workshop) Telegram group — join and message Luna, assistant running on top of pyclaudir, to see it in action before you install.

**pyclaudir** runs a persistent AI assistant in your Telegram. Not a chatbot — an agent that has memory, runs scheduled tasks, can monitor things, and can be extended with any tool you wire up.

You own everything: the memory files, the skill playbooks, the MCP connections, the logs, the tokens. Nothing is routed through a third-party product. You can read every decision it made and change any rule from a DM.

Out of the box it:
- Stays in your group chat and joins conversations when it has something useful to say
- Runs *self-reflection* — reviews what it got wrong and proposes new rules for your approval
- Executes scheduled research tasks in background subagents while staying responsive to messages
- Remembers context across restarts via file-based memory

It is extendable: add MCPs to connect it to anything: GitHub, Jira, email, calendar, your own APIs. Add skills, build custom tools.

Runs on a laptop or small VPS.

The goal is a [Jarvis](https://www.youtube.com/watch?v=Qav7NJIsKL4&t=2s) — an AI that lives with you, monitors what matters, and acts on your behalf. pyclaudir is the foundation.

## Quickstart (3 minutes)

If you don't know where to run, I recommend [Contabo](https://contabo.com/en/vps/) or [Hetzner](https://www.hetzner.com/cloud/).
 
Pre-requisite: 
* Install Docker compose
* Install Claude Code CLI. Login, you can use Claude Code subscription, or API

**Instructions for running on Linux**
```bash
git clone https://github.com/Rustam-Z/pyclaudir && cd pyclaudir

cp .env.example .env && nano .env
#   set TELEGRAM_BOT_TOKEN  (create a bot in @BotFather and copy its token here)
#   set PYCLAUDIR_OWNER_ID  (your numeric Telegram user id, from @userinfobot)
#   update if necessary: PYCLAUDIR_MODEL and PYCLAUDIR_EFFORT

cp access.json.example access.json
#   give access to extra DMs and groups, you can use /access and /deny commands after bot started to update the list

cp plugins.json.example plugins.json && nano plugins.json
#   single source of truth for the bot's capability surface — see below

cp prompts/project.md.example prompts/project.md && nano prompts/project.md
#   set bot name, language, personality

docker compose up -d --build                                              # build and run, wait for "pyclaudir is live"
docker compose logs -f                                                    # [optional] monitor logs
docker compose exec pyclaudir python -m pyclaudir.scripts.trace --follow  # [optional] monitor Claude Code I/O logs
```

DM your bot. It replies.

**On macOS with Docker?** macOS stores your `claude login` token in the Keychain, which the container can't read. See [docs/deployment.md](docs/deployment.md#macos-docker-credentials).

**On Windows with Docker?** Preferably use Linux. For windows. [WSL](https://docs.microsoft.com/en-us/windows/wsl), but make sure you logged in to Claude Code inside WSL, so that there is `~/.claude/.credentials.json`.

**No Docker?** You need Python 3.11+ and the Claude Code CLI (`claude --version`).

```bash
uv sync --extra dev
uv run python -m pyclaudir                                               # run, wait for "pyclaudir is live"
uv run python -m pyclaudir.scripts.trace --follow                        # [optional] monitor, Claude Code I/O logs
```

## What you can do with it

Use as a **personal assistant.** Set reminders, take notes, ask it to research things and report back. It remembers context across restarts. Every day it reviews its own behavior and proposes improvements — you approve, it learns.

Use as a **team companion.** Drop it in a group chat. It tracks conversations, answers questions, and stays quiet when it has nothing useful to add. Ask it to summarize the last 24 hours, create a GitHub issue from a bug you described, or watch a repo and notify the team when something ships. Or review code, or write code, or create a bug report.

Use as an **automation layer.** Wire up MCPs and schedule agents to do real work while you sleep — fetch news, check deploys, monitor competitors, draft reports. Results land in Telegram when they're ready.

### Try saying

- *"hey reschedule the meeting, and message X"*
- *"read the last 24h of our team chat and DM me a 5-bullet status."* — uses `set_reminder` + `query_db` + `send_message`. Ships on by default.
- *"pull the top AI stories from Hacker News and send me a briefing"* — `WebFetch` + `WebSearch`, default tools.
- *"watch https://example.com/changelog hourly and ping me the moment a new entry mentions 'pricing'."* — cron `set_reminder` + `WebFetch`. Diff state lives in a memory file.
- *"review this week's git log on `~/code/myapp` and open a PR if the README has drifted."* — needs `tool_groups.bash: true` and `tool_groups.code: true` in `plugins.json`, plus `GITHUB_PERSONAL_ACCESS_TOKEN` in `.env` for the PR.
- *"every morning at 7am, DM me my Jira tickets due this week, grouped by project."* — needs the `mcp-atlassian` entry enabled (Atlassian's remote MCP, OAuth set up on the host).

<!-- TODO: 30s GIF demo for the README header once we have one -->

## Configuration

> **This README is the high-level intro.** Deeper material lives in
> [docs/](docs/) — full technical manual, deployment walkthrough, tools
> reference, and the systems pyclaudir descends from. Start at
> [docs/README.md](docs/README.md).

Out of the box: messaging, memory, reminders, web, vision. Want shell access? Code editing? Plug in any other MCP server — GitHub, Jira, Notion, Slack, your own — same one-entry pattern, stdio or remote HTTP/SSE with auth headers.

### Telegram @BotFather configs
- Disable "Allow groups" if you don't want others to add bot in groups. 
- Enable "Bot to bot communication" so that bot can see other bot's messages.

### The four setup files

| File | Tracked in git? | What it controls |
|---|---|---|
| `.env` | no | secrets — Telegram bot token, owner id, plus any credentials your `plugins.json` entries reference via `${VAR}` (the example file's GitLab / GitHub entries demonstrate the pattern) |
| `prompts/project.md` | no | persona — bot name, language, house rules, owner-specific instructions; appended to the shipped `prompts/system.md` |
| `plugins.json` | no | capability surface — what tools, skills, and MCPs are on |
| `access.json` | no | who can DM the bot or use it in groups (hot-reloaded, no restart) |

`.env.example`, `prompts/project.md.example`, `plugins.json.example`, and `access.json.example` are tracked so you have a starting point; the real files are gitignored so different deployments carry different config without fighting over the file. Without Docker, `access.json` is auto-created on first run with the safest default (`owner_only`, no allowlist) if you don't seed it from the example. Under Docker it must exist on the host before `docker compose up` (it's bind-mounted so `/allow` and `/deny` edits persist across restarts) — copy the example as shown in the Quickstart.

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

### `.env`

All settings come from environment variables (or `.env`). Full list in
[pyclaudir/config.py](pyclaudir/config.py). The ones you'll touch:

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `PYCLAUDIR_OWNER_ID` | yes | your numeric Telegram user id |
| `PYCLAUDIR_MODEL` | yes | e.g. `claude-opus-4-7` |
| `PYCLAUDIR_EFFORT` | yes | `low` / `medium` / `high` / `max` |

Credentials for any external MCP you wire in live in `.env` and are
pulled into `plugins.json` via `${VAR}` references. 

### Access
Access lives in `access.json` at the repo root (hot-reloaded). One `policy`
gates DMs and groups: `owner_only` (default, owner DM only) · `allowlist`
(`allowed_users` for DMs, `allowed_chats` for groups) · `open` (everyone).
Owner-only commands: `/access`, `/allow`, `/deny`, `/policy`, `/kill`, `/health`, `/audit`, `/reset_session`.
Blocked DMs get one canned "this is a private assistant" reply on their
first message; blocked groups stay silent.

Details: [docs/documentation.md](docs/documentation.md).

## What pyclaudir can do

**communication:** send / reply / edit / delete text, emoji reactions, polls (regular + quiz, multi-answer, auto-close).

**media:** render HTML to PNG (tables, charts, diffs — Chart.js / D3 inline) and LaTeX to PNG (math via KaTeX), send back as inline photos. Read inbound photos (vision), text-like docs (md / txt / log / csv / json / yaml / code …), and PDFs (extracted text with `--- page N ---` markers).

**memory:** persistent markdown files under `data/memories/` (list / read / write / append / send-as-document), 64 KiB per file, read-before-write rail, survives restarts. Per-user / per-group / journal layout.

**search & history:** web search and web fetch (no internal / RFC1918 URLs). Read-only SQL SELECTs on the chat database (`messages`, `users`, `reminders`, ≤100 rows). Multi-hop reply-chain expansion.

**scheduling:** one-shot + cron-recurring reminders. Auto-seeded daily self-reflection skill that promotes corrections into durable rules with owner approval.

**self-edit:** append rules to `prompts/project.md` (owner-only); shipped `system.md` is git-tracked and not exposed.

**skills:** read operator-curated playbooks under `skills/` — `render-style` (house style for renders), `self-reflection` (learning loop). Reference skills are read on initiative; invoked skills require a real `<reminder>` envelope.

**opt-in:** shell (`Bash` / `PowerShell` / `Monitor`), code editing (`Edit` / `Write` / `Read` / `NotebookEdit` / `Glob` / `Grep` / `LSP`), and subagents (`Agent`) — all toggled in `plugins.json`. Off by default. Plug in any external MCP server the same way (the example file ships sample Jira / GitLab / GitHub entries to copy from).

**what can't do:** generate images. Send voice messages, GIFs, animations, stickers. Read voice / video / video notes / stickers (they arrive empty — ask for a screenshot or description). Moderate (mute / ban / kick / unban / member lists). Make phone calls or watch videos.

**How to make AI assistant more proactive?**
- Event subscribers: GitHub webhooks, file watchers, or a CI poller feed into the same engine as Telegram messages. The bot pings you when a PR review lands or a build fails, instead of you asking.
- Scheduled check-ins
- More reminders 
- Idle-time sweeps: if no Telegram message for N hours, run a low-stakes routine (lint, dep audit, memory cleanup) and only ping if it finds something.
- Self-followups

**Make it yours.** Almost every axis is pluggable without touching the core — drop a `BaseTool` into [pyclaudir/tools/](pyclaudir/tools/), append an MCP server to `plugins.json`, add a skill at `skills/<name>/SKILL.md`, reshape the persona in `prompts/project.md`, or run a fleet with separate `PYCLAUDIR_DATA_DIR` paths. Full recipes in [docs/documentation.md](docs/documentation.md#adding-a-new-tool).

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

### Known limitations

- **One process per data dir (enforced).** A second instance on the same
  `PYCLAUDIR_DATA_DIR` refuses to start (`data/.lock` is held by the
  first). Give each instance its own data dir to run a fleet.
- **Crashes don't lose buffered messages.** Messages waiting for a turn
  are replayed on the next start (only ones newer than 24 hours). In rare
  crash timing a message can be answered twice — never silently dropped.
- **Claude's context grows over days.** The Claude session resumes across
  restarts, so very long-running chats eventually fill its context window.
  Send `/reset_session` to start a fresh session — chat history (database)
  and memories (markdown files) are kept.
- **Edits don't re-run.** Editing a Telegram message updates the stored
  copy, but the bot does not re-process the edited text. Send a new message
  instead.
- **Groups are not rate-limited.** Only DMs have the per-user message cap;
  groups are trusted because you allowlist them yourself.
- **Secret scrubbing covers text only.** API keys inside screenshots or
  scanned PDFs are stored as-is — don't send secrets as images.

## Security

The bot is public-facing and the security model is enforced in code, not by hope — see [Security model](docs/documentation.md#security-model) for the full list of rails and [docs/tools.md](docs/tools.md) for the per-tool surface.

## Contributing

Issues and PRs welcome.

Architecture deep-dive before bigger changes: [docs/documentation.md](docs/documentation.md).

## License

MIT. See [LICENSE](LICENSE).
