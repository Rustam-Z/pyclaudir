# pyclaudir documentation

Deep-dive technical documentation. The README is the high-level intro;
this is the manual. Read this when you're modifying internals,
debugging, or auditing.

## Table of contents

- [What gets passed to `claude`](#what-gets-passed-to-claude)
- [Full configuration](#full-configuration)
- [How it works (in detail)](#how-it-works-in-detail)
- [Known limitations](#known-limitations)
- [Adding a new tool](#adding-a-new-tool)
- [Access control](#access-control)
- [Memory](#memory)
- [Rendered visuals](#rendered-visuals)
- [Self-reflection skill](#self-reflection-skill)
- [Agent skills](#agent-skills)
- [Reminders](#reminders)
- [System prompt](#system-prompt)
- [External MCP integrations](#external-mcp-integrations)
- [Monitoring & observability](#monitoring--observability)
- [Security model](#security-model)
- [Manual end-to-end checklist](#manual-end-to-end-checklist)
- [Repo layout](#repo-layout)

## What gets passed to `claude`

pyclaudir runs Claude inside a long-lived
`claude --print --input-format stream-json` process and limits what Claude
can do with the `--allowedTools` and `--disallowedTools` flags. By
default the bot has only its own MCP tools (in `pyclaudir/tools/`,
served by a local MCP server) plus `WebFetch` and `WebSearch`.

The toggle source of truth is [`plugins.json`](../plugins.json) at
the repo root:

* `tool_groups` — flips for the dangerous CC built-ins. `bash`
  unlocks `Bash` / `PowerShell` / `Monitor`; `code` unlocks `Edit` /
  `Write` / `Read` / `NotebookEdit` / `Glob` / `Grep` / `LSP`;
  `subagents` unlocks `Agent`.
* `mcps` — list of external MCP servers to spawn. Three transports
  supported: `stdio`, `http`, `sse`. `${VAR}` references pull
  credentials from `.env`. The shipped `plugins.json.example`
  carries sample Jira / GitLab / GitHub entries you can keep, edit,
  or delete — they're starting points, not first-class. Add a new
  entry to plug in any other MCP server.
* `builtin_tools_disabled` — names of pyclaudir built-in tools to
  hide (e.g. `create_poll`, `render_html`). Filtered at MCP
  registration time, never advertised to Claude.
* `skills_disabled` — names of skill directories to hide.

The full per-tool list and the schema reference live in
[tools.md](tools.md); the loader is `pyclaudir/plugins.py`; the
allow/deny argv is assembled in `pyclaudir/cc_worker.py`.

## Full configuration

All settings come from environment variables (or a `.env` file). They are
read once when the bot starts, in `pyclaudir/config.py` (`Config.from_env`).
The rest of the code reads values from the `Config` object, never from
`os.environ` directly. To add a new setting, add a field to `Config`
instead of calling `os.environ.get` from somewhere else. Tests build a
`Config.for_test(tmp_path)` and set values on it, so they don't depend on
what's in your environment.

The one allowed exception is `pyclaudir/plugins.py` — it reads
`os.environ` directly to substitute `${VAR}` references in
`plugins.json` `mcps[].args`, `env`, `url`, and `headers` values.
That's how an external MCP's credentials reach the spawned server
(or the auth headers for an HTTP/SSE MCP) without being copied
into a `Config` field.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | from @BotFather |
| `PYCLAUDIR_OWNER_ID` | yes | — | your numeric Telegram user id |
| `PYCLAUDIR_MODEL` | yes | — | which Claude model to use (e.g. `claude-opus-4-6`); passed to `--model` |
| `PYCLAUDIR_EFFORT` | yes | — | how hard Claude thinks; passed to `--effort` (one of `low`, `medium`, `high`, `max`) |
| `PYCLAUDIR_DATA_DIR` | no | `./data` | SQLite, memories, access config, raw CC logs |
| `CLAUDE_CODE_BIN` | no | `claude` | name or full path of the `claude` program |
| `PYCLAUDIR_DEBOUNCE_MS` | no | `0` | wait this long after a message before sending it to Claude. Messages that arrive during the wait are bundled into one turn. `0` = send right away. |
| `PYCLAUDIR_RATE_LIMIT_PER_MIN` | no | `20` | max DMs per minute from one user. The owner is not limited. Group chats are not limited. |
| `PYCLAUDIR_SELF_REFLECTION_CRON` | no | `0 0 * * *` | when the daily self-reflection task runs (UTC cron). Default: midnight UTC. |
| `PYCLAUDIR_LIVENESS_TIMEOUT_SECONDS` | no | `300` | if Claude is mid-turn and goes silent (no output, no tool activity) for this many seconds, the bot kills it and starts it again. |
| `PYCLAUDIR_LIVENESS_POLL_SECONDS` | no | `30` | how often the watcher wakes up to check the timeout above. |
| `PYCLAUDIR_TOOL_ERROR_MAX_COUNT` | no | `3` | how many tool errors trigger a stop. Used in two places: (a) failed tool calls in one turn — too many ends the turn; (b) turns where Claude wrote text but didn't call `send_message` — too many in a row makes the bot show the underlying error to the user. Stops the bot from looping forever on a broken tool or a broken model setup. |
| `PYCLAUDIR_TOOL_ERROR_WINDOW_SECONDS` | no | `30` | if errors keep arriving for this many seconds after the first one in a turn, end the turn — even below the count above. |
| `PYCLAUDIR_CRASH_BACKOFF_BASE` | no | `2` | seconds to wait before the first restart after Claude crashes. Doubles after each crash, up to `CRASH_BACKOFF_CAP`. |
| `PYCLAUDIR_CRASH_BACKOFF_CAP` | no | `64` | maximum wait between restarts. Once the wait reaches this, it stops growing. |
| `PYCLAUDIR_CRASH_LIMIT` | no | `10` | how many crashes within `CRASH_WINDOW_SECONDS` count as "too many". When reached, the bot tells the owner and active chats, then exits — and something outside (systemd, docker) is expected to restart the whole bot. |
| `PYCLAUDIR_CRASH_WINDOW_SECONDS` | no | `600` | the time window used for `CRASH_LIMIT`. Only crashes from the last X seconds are counted. |
External-service credentials referenced by the default `plugins.json`
via `${VAR}`. Set these in `.env` to make the corresponding MCP
spawn; clear them to silently skip its MCP at boot.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `JIRA_URL` | no | — | Jira site URL — referenced by the `mcp-atlassian` plugin entry |
| `JIRA_USERNAME` | no | — | Jira username — same |
| `JIRA_API_TOKEN` | no | — | Jira API token — same |
| `GITLAB_URL` | no | — | GitLab URL — referenced by the `mcp-gitlab` plugin entry |
| `GITLAB_TOKEN` | no | — | GitLab personal access token — same |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | no | — | GitHub PAT — referenced by the `github` plugin entry. For Enterprise, add `GITHUB_HOST` to the entry's `env` block in `plugins.json` and set it here too. |

Who can DM the bot or use it in groups is set in `access.json` at the
repo root (sibling of `plugins.json`), not in environment variables.
See [Access control](#access-control).

## How it works (in detail)

Four parts run inside one Python process:

```
Telegram listener  →  Engine (buffer + send/inject)  →  Claude worker  →  claude process
                                  │                                              │
                                  ▼                                              ▼
                               SQLite                                   MCP server (HTTP, localhost:0)
```

1. **Telegram listener** (`pyclaudir/telegram_io.py`). Uses
   python-telegram-bot v21 in polling mode. For each message it does two
   things: save it to SQLite, then hand it to the engine. Owner-only
   commands (`/kill`, `/health`, `/audit`, the access commands) skip the
   engine and run directly.
2. **Engine** (`pyclaudir/engine.py`). Holds the pending message buffer,
   the debounce timer, the mid-turn processing flag, and the inject path.
   Bundles messages that arrive close together. If a new message comes in
   while Claude is mid-reply, the engine sends it via `worker.inject()` so
   the running turn picks it up. If a turn ends with text but no
   `send_message` call (we call this "dropped text"), the engine sends a
   corrective `<error>...</error>` block to nudge Claude into using the
   tool.
3. **Claude worker** (`pyclaudir/cc_worker.py`). Starts the `claude`
   process and watches it. Reads stream-json events from stdout, saves
   stderr for diagnostics, stores `session_id` so a restart can resume
   the same conversation, and starts Claude again after a crash —
   waiting longer each time (`CRASH_BACKOFF_BASE`=2s up to
   `CRASH_BACKOFF_CAP`=64s, with a give-up after `CRASH_LIMIT`=10
   crashes in `CRASH_WINDOW_SECONDS`=600s).
4. **MCP server** (`pyclaudir/mcp_server.py`). A FastMCP server on a
   random port on `127.0.0.1`. It finds every `BaseTool` subclass in
   `pyclaudir/tools/` and registers it. It writes a small JSON config
   file so Claude can connect via `--mcp-config`.

## Known limitations

### One turn at a time

The engine handles **one Claude turn at a time**. While Claude is busy
with a long task (a code review, a big GitLab search, a complex Jira
query), the engine waits for it to finish. Messages from other chats
sit in the buffer and only go through after the current turn ends.

So a 3-minute code review for Chat A will delay replies to Chat B by up
to 3 minutes. For one user or a small group, this is fine. For busy
setups with many chats, run a separate pyclaudir for each chat group.

The system prompt tells the bot to send a quick "On it, reviewing
now..." reply via `send_message` before it starts a long task, so users
know the bot got their message even when the full reply takes time.

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

`access.json` at the repo root governs who can talk to the bot.
Hot-reloaded on every inbound message. Gitignored; template at
`access.json.example`. First run seeds `policy: "owner_only"` with
empty allowlists.

```json
{
  "policy": "owner_only",
  "allowed_users": [],
  "allowed_chats": [-1001234567890]
}
```

| Policy | DMs | Groups |
|---|---|---|
| `owner_only` (default) | Owner only | Blocked |
| `allowlist` | Owner + `allowed_users` | `allowed_chats` |
| `open` | Anyone | Any group |

The owner is always allowed in DMs. Blocked messages are still persisted
to SQLite (audit trail), then dropped before the engine sees them.
Non-allowlisted DMs receive: *"You don't have access to this bot. To
request access, message the owner (Telegram user ID: N)."* Groups stay
silent.

### Owner commands

Owner-only — silently no-op for everyone else. `update.effective_user.id
== PYCLAUDIR_OWNER_ID` is the actual gate; `BotCommandScopeChat` just
hides the `/` menu from non-owners.

```
/access                      Show policy + allowlists
/allow user <id>             Add user to allowed_users
/allow group <chat_id>       Add chat to allowed_chats
/deny user <id>              Remove user
/deny group <chat_id>        Remove chat
/policy <owner_only|allowlist|open>
/kill                        SIGTERM (graceful shutdown)
/health                      Last send, reminder state, rate-limit notices
/audit                       Recent tool failures, backups, memory footprint
```

Edit `access.json` directly if you prefer — changes are hot-reloaded.

## Memory

`data/memories/*.md` is where the bot keeps its notes. It has five tools:

- `list_memories` — list the files
- `read_memory` — read a file (cuts off at 64 KiB)
- `write_memory` — create or overwrite a file (max 64 KiB)
- `append_memory` — add text to an existing file
- `send_memory_document` — send a memory file to a chat as a Telegram
  document (path-locked to `data/memories/`, optional caption + reply-to)

**Read before write.** To overwrite or append to a file that already
exists, the bot has to read it first in the same session. Brand-new files
are exempt. The list of "files I've read" is held in memory and clears
every time the bot restarts, so a fresh start has to re-read before
changing anything. This stops the bot from accidentally destroying notes
you wrote but it never read.

**No delete tool, on purpose.** If the bot wants to "forget" something,
it has to overwrite the file. Actually deleting a file is up to you:
`rm data/memories/<file>` on the host.

You can also seed memory yourself by putting markdown files into
`data/memories/` while the bot is running. It will see them on the next
`list_memories` call.

### Learning — `self/learnings.md`

Mistakes, corrections, and patterns the bot wants to carry forward live
in `data/memories/self/learnings.md`. Conventions (enforced by the system
prompt):

- **On correction, same-turn capture.** When a user corrects the bot, or
  it notices mid-conversation it got something wrong, it writes a new
  `## <date> — <topic>` entry before the turn ends. Don't batch, don't
  defer — the signal evaporates fast.
- **`[pending]` marker** in the h2 header flags an entry as a candidate
  for promotion to a durable rule in `prompts/project.md`. Plain headers
  (no marker) are pure history. The daily self-reflection skill picks up
  `[pending]` entries and stress-tests them (see below). Status
  transitions: `[pending]` → `[promoted]` / `[discarded]` / `[refined]`.
- **`**Proposed rule:**` line** accompanies every `[pending]` entry so
  the skill knows what rule text to consider. Without it, the skill asks
  the operator to re-file the entry.

## Rendered visuals

Two tools turn structured data into a Telegram photo:

- `render_html(html, width?=800, height?=600, title?)` — runs the HTML
  through headless Chromium (Playwright) with **all outbound network
  blocked at the route layer**, takes a full-page PNG, saves it under
  `data/renders/<utc-stamp>-<slug>-<rand>.png`, returns the relative
  path. Inline any CSS/JS the page needs (Chart.js, D3, fonts) — the
  browser can't fetch.
- `send_photo(chat_id, path, caption?, reply_to_message_id?)` — sends
  a file from `data/renders/` as an inline Telegram photo. Path-locked
  to the renders root with the same hardening as `read_memory`.

The agent's `render_html` calls follow the house style in
[`skills/render-style/`](../skills/render-style/) — three skeletons
(dashboard, timeline, architecture diagram) the agent reads via
`read_skill render-style` before composing HTML. Tokens are dark-navy
with semantic colors (green/blue/red/amber/purple/cyan/gray).

Playwright + Chromium are pre-installed in the Docker image. For local
runs: `uv sync && uv run playwright install chromium`.

## Self-reflection skill

A daily two-phase loop that drives self-improvement. Triggered by an
auto-seeded recurring reminder (default midnight UTC every day; override
with `PYCLAUDIR_SELF_REFLECTION_CRON`):

- **Phase A — introspect.** Bot reads the last 24h of outbound messages
  + their reactions via `query_db`, applies a checklist (over-long
  replies, ping-rule deviations, negative reactions, repeated rewrites,
  tone/language mismatches), and writes up to 3 candidate lessons into
  `learnings.md` with `[pending]` markers. This catches drift the user
  hasn't called out yet.
- **Phase B — process.** Bot reads every `[pending]` entry (Phase-A's
  fresh ones plus anything from the on-correction rule above),
  stress-tests each against 10-20 hypothetical scenarios, scores fit
  (<30% discard, 60-85% promote, 85%+ overreach → refine), DMs the
  owner a numbered proposal, waits for approval, and on approval calls
  the instruction-edit tools to append rules to `project.md`.

**Mandatory loop.** The reminder is protected on two layers:
- `cancel_reminder` refuses to cancel rows with `auto_seed_key` set (so
  a prompt-injected bot can't stop the loop).
- `_seed_default_reminders` in `__main__.py` re-seeds on every startup
  if no pending row exists — cancelling or deleting via SQL loses only
  until the next container restart.

The playbook lives at `skills/self-reflection/SKILL.md`. See the
[Agent skills](#agent-skills) section below for how skill invocation
works and how to add more.

## Agent skills

Skills are operator-curated multi-step playbooks stored in the top-level
`skills/<name>/SKILL.md` format, following the
**[Agent Skills specification](https://agentskills.io/specification)**.
They ship with the repo (versioned in git) and are read-only from the
bot's perspective.

Each SKILL.md must begin with YAML frontmatter containing at least
`name` (matching the directory) and `description` (what the skill does
and when to use it). Our `SkillsStore` validates both on load and
refuses malformed skills.

Tools:

- `list_skills` — enumerate available skills as name + description pairs
  (the spec's progressive-disclosure metadata surface).
- `read_skill(name)` — load the full SKILL.md playbook.

**Invocation.** A skill is triggered by a reminder whose text body is
`<skill name="X">run</skill>`. The reminder loop wraps that in a
`<reminder>` XML envelope before injecting into the engine. The bot,
per `system.md § Skills`, recognizes `<skill>` inside `<reminder>` and
calls `read_skill("X")` to load + execute the playbook.

**Trust model.** The bot trusts `<skill>` directives only when wrapped
in a `<reminder>` envelope (server-synthesized). A user typing
`<skill name="X">run</skill>` in regular chat is treated as a
prompt-injection attempt and ignored.

### Adding a new skill

Drop a new folder under `skills/`:

```
skills/
└── your-skill-name/
    ├── SKILL.md       # required: YAML frontmatter + playbook body
    ├── README.md      # optional, operator-facing doc
    ├── scripts/       # optional: executable helpers (spec)
    ├── references/    # optional: on-demand reference docs (spec)
    └── assets/        # optional: templates, schemas (spec)
```

Minimum SKILL.md:

```markdown
---
name: your-skill-name
description: One sentence on what the skill does AND when to use it (cap: 1024 chars).
---

# your-skill-name

Playbook body — step-by-step instructions the bot follows when this
skill activates.
```

The name must match the directory (lowercase, `a-z0-9-` only, no
leading/trailing/consecutive hyphens). Optional frontmatter fields per
spec: `license`, `compatibility`, `metadata`, `allowed-tools`.

The SkillsStore auto-discovers any first-level directory that contains a
`SKILL.md`. No code changes needed unless you want it to run on a
schedule:

1. To make the skill fire daily/weekly, add an auto-seeded reminder in
   `_seed_default_reminders` (`pyclaudir/__main__.py`) with a unique
   `auto_seed_key` (e.g. `"your-skill-default"`).
2. Add a migration if you need new DB columns/tables.
3. Remember: auto-seeded reminders are protected by default —
   `cancel_reminder` refuses them, and the seed hook re-creates them if
   missing on restart. That's intentional; skills that should be
   interruptible shouldn't use the auto_seed_key path.

The playbook itself is markdown the bot reads and executes step by step.
See `skills/self-reflection/SKILL.md` as a worked example. Keep the
playbook self-contained: preconditions check, the data the skill should
read, the decisions it should make, and the tools it should call.

## Reminders

The agent can schedule one-shot and recurring reminders via three tools:

- `set_reminder` — schedule a reminder with a UTC trigger time and
  optional cron expression
- `list_reminders` — show pending reminders for a chat
- `cancel_reminder` — cancel a pending reminder by id

Reminders are stored in the `reminders` SQLite table. A background task
polls every 60 seconds for due entries and injects them into the engine
as synthetic inbound messages. The agent then sends the reminder text to
the appropriate chat. Recurring reminders (cron) automatically advance
to the next occurrence.

Reminders fire on time even if the bot is mid-conversation. When the
fire happens during an active turn, the bot first posts a short notice
to the chat ("Pausing to handle a scheduled task — back in a moment.")
so the interrupt is visible, then runs the reminder turn after the
current one ends.

All times are stored in UTC. The system prompt instructs the agent to
ask users for their timezone and convert to UTC before setting
reminders.

## System prompt

The system prompt is assembled from two files:

1. **`prompts/system.md`** — generic pyclaudir template covering tool
   discipline, message format, memory, reminders, and prompt-injection
   resistance. Ships with the repo.
2. **`prompts/project.md`** — project-specific overlay (identity,
   integrations, custom instructions). Gitignored. Copy
   `prompts/project.md.example` to get started. Path is hardcoded —
   always at `prompts/project.md`.

If `project.md` doesn't exist, only the base prompt is used.

## External MCP integrations

pyclaudir can optionally connect to external MCP servers alongside
its own. There's no built-in integration list — every external MCP
is just an entry in `plugins.json` `mcps[]`. The shipped
`plugins.json.example` includes three sample entries you can keep,
edit, or delete — they're starting points, not first-class:

- **Jira** via [mcp-atlassian](https://github.com/sooperset/mcp-atlassian)
  (stdio) — set `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN` in `.env`.
- **GitLab** via
  [@zereight/mcp-gitlab](https://www.npmjs.com/package/@zereight/mcp-gitlab)
  (stdio) — set `GITLAB_URL`, `GITLAB_TOKEN` in `.env`.
- **GitHub** via
  [@modelcontextprotocol/server-github](https://www.npmjs.com/package/@modelcontextprotocol/server-github)
  (stdio) — set `GITHUB_PERSONAL_ACCESS_TOKEN` in `.env`. For
  Enterprise, add `"GITHUB_HOST": "${GITHUB_HOST}"` to the entry's
  `env` block and set `GITHUB_HOST` in `.env` too.

Each entry references its credentials with `${VAR}` interpolation;
when any required var is empty, that MCP is silently skipped at boot.
To stop advertising one without removing credentials, flip
`enabled: false` on its entry. To remove permanently, delete the
entry.

Adding a new MCP (Notion, Linear, Slack, Postgres, Playwright, your
own — stdio, http, or sse) is a `plugins.json` edit — no Python
change. See [tools.md](tools.md) for the schema and per-transport
shape.

## Monitoring & observability

Pyclaudir gives you **four complementary windows** into what the bot is
doing. Pick whichever fits the moment.

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
21:34:12 INFO  pyclaudir.tx       [RX] DM Alice[12345] m42 | how fast are you
21:34:12 INFO  pyclaudir.engine   starting turn with 1 msgs
21:34:12 INFO  pyclaudir.cc       [CC.user] <msg id="42" chat="12345" ...>↵how fast are you↵</msg>
21:34:13 INFO  pyclaudir.cc       [CC.tool→] mcp__pyclaudir__send_message({"chat_id":12345,"text":"Honestly?…"}) id=toolu_01
21:34:14 INFO  pyclaudir.tx       [TX] DM Alice[12345] m43 | Honestly? Not blazing fast 😅 …
21:34:14 INFO  pyclaudir.cc       [CC.tool✓] id=toolu_01 | sent message_id=43
21:34:14 INFO  pyclaudir.cc       [CC.done]  action=stop reason=Answered the user's question
```

The `httpx`/`mcp` per-poll noise is silenced by default. To bring it
back for debugging, comment the relevant lines in
`pyclaudir/__main__.py:_setup_logging()`.

### 2. The replayable session viewer (`pyclaudir.scripts.trace`)

Claude Code persists every CC session as a JSONL file at
`~/.claude/projects/<encoded-project-dir>/<session_id>.jsonl`, where
`<encoded-project-dir>` is the absolute project path with every
non-alphanumeric character replaced by `-` (e.g. `/home/alice/pyclaudir`
→ `-home-alice-pyclaudir`). `pyclaudir.scripts.trace` computes this
automatically from the cwd; override with `CLAUDE_PROJECT_DIR` if
needed. This is the **complete conversation log** — every user
envelope, every assistant message, every tool_use, every tool_result,
every thinking block.

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
fingerprinting (a session is "the bot's" iff its first user event
begins with the engine's `<msg ...>` XML envelope). This stops the
renderer from accidentally tailing your own Claude Code session that
happens to be the most recently modified file in the same project
directory.

The renderer is **read-only** and never touches the running pyclaudir
process — totally safe to run in a second terminal while the bot is
live.

### 3. The raw wire-stream capture (`data/cc_logs/`)

Independent from Claude Code's own session JSONL, pyclaudir also
captures the raw bytes coming out of the CC subprocess on stdout/stderr
to:

```
data/cc_logs/<session_id>.stream.jsonl   # one event per line, pre-parse
data/cc_logs/<session_id>.stderr.log     # timestamped stderr lines
```

This is the **wire log** (what came out of the subprocess) as opposed
to the *conversation log* (what was in the model's context). The two
overlap mostly but the wire log also captures `result` events, `ping`
frames, and any malformed JSON the parser would otherwise drop. Useful
when debugging parser bugs or weird stream artifacts.

```bash
# Live wire stream
tail -f data/cc_logs/*.stream.jsonl | jq -c .

# CC's stderr (rate-limit notices, retries, warnings)
tail -f data/cc_logs/*.stderr.log
```

Capture is on by default. Files rotate per session id, append across
respawns of the same session, and survive crashes.

### 4. SQLite — auditable, queryable history

Everything that touches Telegram or any MCP tool is in
`data/pyclaudir.db`. Useful one-liners:

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
   FROM users WHERE chat_id = 12345 ORDER BY message_count DESC;"

# Find every reply chain involving a specific user
sqlite3 data/pyclaudir.db \
  "SELECT message_id, reply_to_id, substr(text,1,100)
   FROM messages WHERE user_id = 12345 AND reply_to_id IS NOT NULL;"
```

`query_db` (the MCP tool) lets the agent run SELECTs against this same
database — sqlglot-validated, capped at 100 rows.

### 5. Bonus — interactive replay (`claude --resume`)

Drop into the bot's *exact* conversation state in a real Claude Code
interactive session:

```bash
# Stop pyclaudir first, OR use --fork-session to branch safely
claude --resume $(cat data/session_id)
```

You're now talking to Claude Code with the bot's full history loaded.
Ask "why did you reply that way to message 591?" and you'll get its
perspective on its own past turns. ⚠️ Don't run this on the same
session id as a live pyclaudir process unless you pass `--fork-session`.

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

The agent is a *front-facing public agent*. Anyone in an allowed chat
can talk to it, and they're not always trustworthy. The security model
is enforced by code, not by hope, and tested in
`tests/test_security_invariants.py`.

- **No shell, no edits, no writes outside `memories/`, no general reads
  outside `memories/`, no subagents — by default.** The CC subprocess
  is spawned with `--allowedTools mcp__pyclaudir,WebFetch,WebSearch`
  (the always-on base) and a deny list covering every gated tool:
  `--disallowedTools Bash,PowerShell,Monitor,Edit,Write,Read,
  NotebookEdit,Glob,Grep,LSP,Agent --strict-mcp-config`. Each gated
  group flips on via `plugins.json` `tool_groups`; external-MCP tool
  advertisement follows from `plugins.json` `mcps[]` entries whose
  `${VAR}` references resolve. The forbidden flag
  `--dangerously-skip-permissions` is *never* passed; both the argv
  builder and the spawn-time assertion refuse it. See
  [tools.md](tools.md) for the full per-tool list and the
  `plugins.json` schema.
- **Web access (read-only).** `WebFetch` and `WebSearch` are
  deliberately enabled so the agent can answer questions that need
  fresh information. This is a real trade-off — see the next bullet.
  The system prompt instructs the agent to refuse private/internal
  URLs (localhost, RFC1918, link-local, `.local`), but a determined
  prompt-injection could still get it to fetch one. **Do not deploy
  the bot on a host with sensitive internal endpoints reachable from
  the same network.**
- **MCP namespace lockdown.** The local MCP server is registered as
  `pyclaudir`, so every pyclaudir tool Claude sees is named
  `mcp__pyclaudir__<x>`. The two web tools are Claude Code built-ins,
  not MCP tools, so they show up unprefixed (`WebFetch`, `WebSearch`).
- **Memory writes with safety rails.** `write_memory` and
  `append_memory` exist, but are guarded by:
  - **Path traversal hardening** (no `..`, no absolute paths, no
    symlinks) — applies to writes the same way it applies to reads.
  - **64 KiB per-file size cap** — both writes and post-append totals.
  - **Read-before-write** — overwriting or appending to an *existing*
    file requires `read_memory` to have been called on it first in the
    same session. New files are exempt. The set of "read paths"
    resets on every restart so a fresh process must re-read before
    mutating.
  - **No deletion tool** — forgetting requires explicit overwriting.
- **No filesystem reads outside `memory.py`.** AST scan asserts no
  `open()` / `read_text()` / `read_bytes()` lives in any other tool
  module.
- **No subprocess calls in tools.** AST scan rejects `subprocess.*`,
  `os.system`, `os.popen`, `asyncio.create_subprocess_*` anywhere
  under `pyclaudir/tools/`. The *only* place those primitives are
  allowed is `cc_worker.py`, which spawns `claude` itself.
- **Owner-only privileged commands.** `/kill`, `/health`, `/audit`,
  `/access`, `/allow`, `/deny`, `/policy` check
  `update.effective_user.id == PYCLAUDIR_OWNER_ID` before running and
  silently no-op for anyone else.
- **`query_db` is read-only.** Inputs are parsed with `sqlglot` and
  rejected unless they're a single SELECT. CTEs are walked
  recursively; semicolons, PRAGMA, ATTACH, INSERT/UPDATE/DELETE/DROP/
  CREATE/ALTER all fail. Results cap at 100 rows; text columns
  truncate at 2000 chars.
- **Per-user inbound DM rate limit.** 20 messages / 60s / user by
  default, DB-backed (`rate_limits` table, fixed-minute buckets) so it
  survives restarts. Enforced at `telegram_io._on_message` before
  `engine.submit()`: over-limit DMs are still persisted (audit trail)
  but never reach the CC subprocess. **Groups are not rate-limited** —
  noisy users in groups are the group's problem. **The owner
  (`PYCLAUDIR_OWNER_ID`) is fully exempt** — the counter never ticks
  for the owner. When a user exhausts their bucket they get one
  Telegram notice ("you're sending too fast…") then the bot goes quiet
  until the bucket rolls over.
- **Audit log.** Every MCP tool invocation persists to `tool_calls`
  (name, args, result, error, duration). Owner can review recent
  failures via `/audit`.
- **Secrets scrubbing at persistence.** Inbound message text and the
  raw Telegram `Update` JSON are passed through
  `secrets_scrubber.scrub()` before `insert_message` writes to
  SQLite. Redacts Bearer tokens, `sk-…` keys, GitHub/Slack tokens,
  AWS access keys, JWTs, PEM private-key blocks, and DSNs with
  embedded passwords. An accidental credential paste never lands in
  the DB.
- **Wedged-subprocess detection.** `CcWorker._liveness_loop` watches
  for silent-mid-turn subprocesses: if `max(last stdout event, last
  MCP tool call) < now - PYCLAUDIR_LIVENESS_TIMEOUT_SECONDS` (default
  300s) and a turn is in progress, the subprocess is terminated so
  the crash-recovery path respawns it with the same session id.
  Doesn't fire when idle (silence is expected between turns).
- **Tool-error circuit breaker.** A stream-json `tool_result` with
  `is_error=true` increments a per-turn counter in `CcWorker`; when
  the counter hits `PYCLAUDIR_TOOL_ERROR_MAX_COUNT` (default 3) or
  the first-error window exceeds
  `PYCLAUDIR_TOOL_ERROR_WINDOW_SECONDS` (default 30s), the worker
  puts a sentinel `TurnResult` on the result queue and schedules
  `_terminate_proc`. The engine unblocks immediately; `_on_cc_crash`
  notifies the user on respawn. Prevents Claude from burning minutes
  looping on a deterministically-failing tool (e.g. permission
  denied, schema violation).
- **Dropped-text retry cap.** A turn that ends with text blocks but
  no `send_message` call (`dropped_text=True`) increments
  `Engine._dropped_text_retries` — a *cross-turn* counter that
  shares the `PYCLAUDIR_TOOL_ERROR_MAX_COUNT` ceiling with the
  tool-error breaker. Below the cap the engine injects a corrective
  `<error>Use send_message</error>`; at the cap it calls
  `classify_cc_failure` on the text blocks, surfaces a targeted
  message (e.g. "model unavailable — fix `PYCLAUDIR_MODEL`") to the
  user, and resets. Catches CC-native diagnostics (invalid model,
  auth failure, quota) that would otherwise loop silently.
- **Crash-loop terminal notification.** When the crash budget
  (`Config.crash_limit` crashes in `Config.crash_window_seconds`,
  defaults 10 / 600s) is exhausted, `CcWorker._supervise_loop` fires
  the `on_giveup` callback *before* raising `CrashLoop` — so owner
  + any active chats get a clear "I'm shutting down, operator needs
  to intervene" message (classified where possible) instead of the
  supervisor task dying silently.
- **Failure classifier.** `pyclaudir/cc_failure_classifier.py` is the
  single authoritative mapping from CC stderr / text blocks to
  user-facing messages. Used by the engine's post-turn stderr sweep,
  the dropped-text handler, the on_crash hook, and the on_giveup
  hook. Add a new failure mode = append one `CcFailurePattern`.
- **Instruction tools are owner-only (any chat).** Two tools —
  `read_instructions` and `append_instructions` — expose
  `prompts/project.md` (and only that file) to the bot. system.md is
  git-tracked, so it's intentionally not exposed; all owner-driven
  customisations accumulate in project.md, which is concatenated
  after system.md to form the full prompt. No code-level permission
  check exists — the owner-only rule is enforced by the system
  prompt. Code rails that DO enforce: the file path is hardcoded,
  the size cap (128 KiB), atomic write, and a timestamped backup
  before every append. Revert is `mv <backup> prompts/project.md &&
  docker compose restart pyclaudir`. Edits take effect on the next
  CC spawn, not mid-session, which gives the operator a natural
  review window.
- **Skills are operator-curated playbooks.** Markdown files under
  `skills/<name>/SKILL.md` that describe multi-step agent workflows.
  Exposed read-only via `list_skills` / `read_skill`. A skill is
  invoked when a `<reminder>` envelope contains `<skill
  name="X">run</skill>` — the system prompt teaches the bot to
  trust `<skill>` tags only inside that envelope, so a user typing
  one in chat does nothing. The first skill is `self-reflection`: a
  daily loop that stress-tests lessons from `learnings.md` and
  proposes promotions to `project.md`, gated on explicit owner
  approval via the instruction-edit tools above.

If you weaken any of these, the security tests will fail loudly. They
are load-bearing — keep them.

## Manual end-to-end checklist

Once configured, you should be able to:

1. DM the bot, see the bot reply via `send_message`.
2. Drop `data/memories/user_preferences.md` containing "Alice prefers
   Russian", ask "what do you know about me?", watch it call
   `list_memories` → `read_memory` and reply in Russian.
3. Send 5 messages in 2 seconds, see them batched into one turn
   (debounce).
4. Send a 6th message *while* it's mid-turn, see it injected.
5. `sqlite3 data/pyclaudir.db 'SELECT direction, text FROM messages ORDER BY timestamp DESC LIMIT 10;'`
6. Drop `pyclaudir/tools/echo.py` (above), restart, and watch the bot
   gain the new tool with zero other code changes.
7. `kill -9 $(pgrep -f 'claude --print')`, watch the worker respawn
   within seconds and resume the conversation.
8. Ask the bot to run a shell command — it should refuse, because it
   has no `Bash` tool and its system prompt tells it to.
9. Run `uv run python -m pytest tests/test_security_invariants.py`
   and see all 8 invariants pass.

## Repo layout

```
pyclaudir/
├── pyproject.toml
├── README.md
├── docs/
│   ├── README.md               # index of what's in docs/
│   ├── documentation.md        # this file — full technical manual
│   ├── deployment.md           # VPS + CD setup walkthrough
│   └── reference-architectures.md  # Claudir / Anthropic plugin notes
├── Dockerfile
├── docker-compose.yml
├── plugins.json                # operator-edited capability config (gitignored)
├── plugins.json.example        # template for plugins.json
├── access.json                 # DM policy + allowed users/chats (gitignored, hot-reloaded)
├── access.json.example         # template for access.json
├── prompts/
│   ├── system.md               # generic pyclaudir system prompt (shipped)
│   ├── project.md              # project-specific overlay (gitignored)
│   └── project.md.example      # template for project.md
├── skills/                     # agent skills (playbooks, shipped)
│   ├── README.md               #   directory index + skill-mode notes
│   ├── self-reflection/        # invoked-mode: daily reflection loop
│   │   ├── SKILL.md            #     playbook the bot reads + follows
│   │   └── README.md
│   └── render-style/           # reference-mode: render_html style guide
│       ├── SKILL.md            #     tokens + 3 HTML skeletons
│       └── README.md
├── data/                       # gitignored
│   ├── pyclaudir.db            # SQLite (messages, users, tool_calls, ...)
│   ├── session_id              # CC session id for --resume
│   ├── memories/               # the agent's working memory
│   ├── attachments/            # inbound photos/docs the dispatcher saves
│   ├── renders/                # outbound PNGs from render_html
│   ├── prompt_backups/         # auto-backups before append_instructions writes
│   └── cc_logs/                # raw CC stdout/stderr capture
├── scripts/
│   ├── sync-memories.sh        # rsync helper for server ↔ local sync
│   └── prune-backups.sh        # archive stale prompt backups (keep newest 50)
├── pyclaudir/
│   ├── __main__.py             # entrypoint + log setup
│   ├── access.py               # hot-reloadable access.json gate
│   ├── config.py
│   ├── db/{database.py,messages.py,reminders.py,migrations/}
│   ├── telegram_io.py
│   ├── engine.py               # debouncer, queue, inject, control loop
│   ├── cc_worker.py            # subprocess + raw capture + crash recovery
│   ├── cc_schema.py            # ControlAction JSON schema (flat — see §5.15)
│   ├── cc_failure_classifier.py # CC stderr/text → user-facing message map
│   ├── mcp_server.py           # FastMCP host + tool auto-discovery
│   ├── memory_store.py         # path-hardened markdown store
│   ├── attachments_store.py    # path-hardened read of data/attachments/
│   ├── render_store.py         # writable PNG store under data/renders/
│   ├── instructions_store.py   # path-hardened read+append of project.md
│   ├── skills_store.py         # path-hardened read of skills/
│   ├── secrets_scrubber.py     # redacts tokens before persistence
│   ├── rate_limiter.py
│   ├── transcript.py           # [RX]/[TX]/[CC.*] log helpers
│   ├── models.py
│   ├── scripts/
│   │   ├── trace.py            # CC session JSONL replay/follow renderer
│   │   └── validate_skills.py  # validate skills/ against the Agent Skills spec
│   └── tools/
│       ├── base.py             # BaseTool, ToolContext, Heartbeat
│       ├── now.py
│       ├── send_message.py
│       ├── reply_to_message.py
│       ├── edit_message.py
│       ├── delete_message.py
│       ├── add_reaction.py
│       ├── create_poll.py
│       ├── stop_poll.py
│       ├── read_attachment.py  # read a Telegram photo/doc by path under data/attachments/
│       ├── send_memory_document.py # send a memory file as a Telegram document
│       ├── render_html.py      # HTML → PNG via headless Chromium (network blocked)
│       ├── send_photo.py       # send a render as an inline Telegram photo
│       ├── memory.py           # list/read/write/append memory (read-before-write)
│       ├── instructions.py     # read/append project.md (owner-only by prompt policy)
│       ├── skills.py           # list/read agent skill playbooks under skills/
│       ├── create_poll.py      # send poll / quiz
│       ├── stop_poll.py
│       ├── query_db.py
│       └── reminder.py         # set/list/cancel reminders
└── tests/
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
    ├── test_rate_limits_dm_only.py    # DM-only, owner-exempt dispatcher-level limiter
    ├── test_instructions_store.py     # allowlist, size cap, read-before-write, backup
    ├── test_instructions_tools.py     # store rails + write-allowlist refusals at the tool layer
    ├── test_skills_store.py           # Agent Skills spec conformance + path hardening
    ├── test_skills_tools.py           # list_skills / read_skill surface
    ├── test_auto_seed_reminder.py     # mandatory self-reflection reminder + cancel gate
    ├── test_secrets_scrubber.py       # credential redaction at persistence boundary
    ├── test_liveness.py                # wedged-mid-turn subprocess detection
    ├── test_reply_chain.py             # multi-hop reply expansion
    ├── test_transcript.py              # tagged log formatting
    └── test_query_db.py
```
