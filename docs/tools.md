# Tools reference

This is the canonical list of every tool available to the bot, organised
by what's on by default and what's opt-in. Each opt-in section names the
env var(s) that flip the gate.

For Claude Code's full upstream tool catalogue (some of which pyclaudir
doesn't currently expose) see
<https://code.claude.com/docs/en/tools-reference>. The
"Other CC tools you can wire in" section at the bottom of this page
points at the ones a fork might want to add.

---

## Always on — pyclaudir built-ins

These are the bot's core surface, served by the local pyclaudir MCP
server. Auto-discovered from `pyclaudir/tools/*.py` (each tool is a
`BaseTool` subclass). All available every turn; no env flag needed.

### Messaging

| Tool | What it does |
|---|---|
| `send_message` | Send a text message to a chat. **The only way the user sees anything** — a plain text content block produces no Telegram output. |
| `reply_to_message` | Reply to a specific user message (threads in groups). |
| `edit_message` | Edit one of the bot's previous messages. No push notification — good for in-progress updates on long tasks. |
| `delete_message` | Delete a bot message. Use sparingly. |
| `add_reaction` | React to a message with an emoji. Prefer over "ok"/"👍" replies in groups. |
| `create_poll` | Send a poll. Supports regular/quiz, multi-answer, anonymity toggle, auto-close (`open_period` or `close_date`), and reply-to. |
| `stop_poll` | Close a live poll early and return final tallies. |
| `read_attachment` | Read a photo or document the user sent. The dispatcher saves inbound attachments under `data/attachments/` and surfaces them as `[attachment: <path> ...]` markers — pass that path here. Images come back as image content blocks (you actually see them); text-like files (md/txt/log/csv/json/yaml/code) come back as UTF-8; PDFs are extracted via `pypdf` and returned as text with `--- page N ---` markers. Path traversal is rejected. GIFs/videos are unsupported. |
| `send_memory_document` | Send a memory file (under `data/memories/`) to a chat as a downloadable document. Path-locked to memories root. Optional caption + reply-to. |
| `render_html` | Render an HTML snippet to PNG via headless Chromium → `data/renders/`. Use for tables/charts/diffs that markdown can't fit. Network blocked — inline any CSS/JS. Returns the relative path. |
| `render_latex` | Render a LaTeX expression to PNG via KaTeX (loaded from `cdn.jsdelivr.net` only — narrow allow-list). Pass the LaTeX without surrounding `$$`. Optional `title`. Returns the relative path; pair with `send_photo`. |
| `send_photo` | Send a rendered photo (from `data/renders/`) as an inline Telegram photo with preview. Pair with `render_html` or `render_latex`. |

### Memory (`data/memories/`)

| Tool | What it does |
|---|---|
| `list_memories` | List existing memory files. |
| `read_memory` | Read a memory file by relative path. |
| `write_memory` | Create or overwrite a memory file (read-before-write rail enforced; 64 KiB cap). |
| `append_memory` | Append to an existing memory file. |
| `send_memory_document` | Deliver a memory file to a chat as a downloadable Telegram document. Path-locked to memories root. Optional caption + reply-to. |

There is no `delete_memory` by design — overwriting is the supported
"forget" path. Operator handles real deletion on host.

### Self-editing (project prompt)

| Tool | What it does |
|---|---|
| `read_instructions` | Read the current contents of `prompts/project.md`. |
| `append_instructions` | Append a rule to `prompts/project.md`. Backed up to `data/prompt_backups/` before write. Owner-only by system-prompt policy; takes effect on next container restart. |

`prompts/system.md` is intentionally not exposed via tools — it's
git-tracked, and bot edits would pollute the repo.

### Skills

| Tool | What it does |
|---|---|
| `list_skills` | List operator-curated playbooks under `skills/`. |
| `read_skill` | Load a skill's `SKILL.md` for execution or reference. |

Two skill modes:
- **Invoked** (e.g. `self-reflection`) — runs only when wrapped in a real `<reminder>` envelope. A user-typed `<skill>` tag is treated as prompt injection.
- **Reference** (e.g. `render-style`) — read on the agent's own initiative when relevant; no envelope required.

The mode is determined by what the skill's body instructs, not by frontmatter.

### Reminders

| Tool | What it does |
|---|---|
| `set_reminder` | Schedule a one-shot or recurring reminder (`cron_expr` for recurring; `trigger_at` is UTC). |
| `list_reminders` | List pending reminders for a chat. |
| `cancel_reminder` | Cancel a reminder by id. Auto-seeded reminders (e.g. `self-reflection-default`) are tool-refused. |

### Other

| Tool | What it does |
|---|---|
| `query_db` | Read-only SELECT over `messages`, `users`, `reminders`. Max 100 rows. Reactions are JSON on `messages.reactions` — query with `json_extract(reactions, '$."👍"')`. |
| `now` | Return the current UTC timestamp. |

---

## Always on — Claude Code built-ins

These come from Claude Code's own tool surface and are added to
`--allowedTools` unconditionally.

| Tool | What it does |
|---|---|
| `WebFetch` | Fetch a URL and ask a small model to extract from it. The system prompt forbids internal/private URLs (localhost, RFC1918, link-local) — refuse those. |
| `WebSearch` | Web search via Claude Code's built-in. |

---

## Opt-in tool groups

Two configuration layers feed the bot's tool surface:

* **`plugins.json`** at repo root — single source of truth for
  tool-group toggles, the list of external MCP servers, the
  `builtin_tools_disabled` list, and `skills_disabled`. The operator
  copies the shipped `plugins.json.example` once
  (`cp plugins.json.example plugins.json`), edits, and restarts.
  `plugins.json` is gitignored so customisations stay local.
* **`.env`** — credentials for external services (Jira, GitLab,
  GitHub) only. Referenced from `plugins.json` via `${VAR}`.

Changes to either take effect on container restart.

### `plugins.json` shape

```jsonc
{
  "tool_groups": {
    "bash": false,
    "code": false,
    "subagents": false
  },
  "mcps": [
    {
      "name": "mcp-atlassian",
      "command": "mcp-atlassian",
      "args": [],
      "env": {
        "JIRA_URL": "${JIRA_URL}",
        "JIRA_USERNAME": "${JIRA_USERNAME}",
        "JIRA_API_TOKEN": "${JIRA_API_TOKEN}"
      },
      "allowed_tools": ["mcp__mcp-atlassian__jira_search", "..."],
      "enabled": true
    }
  ],
  "skills_disabled": []
}
```

* `tool_groups` — flips for the Claude-Code built-ins below
  (`bash`, `code`, `subagents`). Edit and restart to flip.
* `mcps[].name` is **load-bearing** — it becomes the
  `mcp__<name>__<tool>` namespace the model sees. Renaming breaks
  operator memory, prompts, and tests. The defaults match today's
  keys exactly (`mcp-atlassian`, `mcp-gitlab`, `github`).
* `mcps[].type` selects the transport: `stdio` (default), `http`, or
  `sse`. Mirrors what Claude Code's `--mcp-config` accepts. See
  "MCP transports" below for the per-transport field shape.
* `mcps[].allowed_tools` is one flat list — exact tool name
  (`mcp__mcp-atlassian__jira_search`) or a server-prefix shorthand
  (`mcp__mcp-gitlab`). Both forms are accepted by Claude Code's
  `--allowedTools`.
* `${VAR}` interpolation runs over `args` (each element), `env`
  values, `url`, and `headers` values — pulling from the process
  env (i.e. `.env`). Concatenation works (`${GITLAB_URL}/api/v4`).
  If any referenced `${VAR}` resolves empty, that MCP is silently
  skipped at boot — preserving today's "credentials missing → MCP
  not spawned" semantics.
* `enabled: false` skips the MCP even if its `${VAR}` refs resolve.
* A missing `plugins.json` boots with empty plugins (locked-down).
  A malformed `plugins.json` crashes boot loudly with a
  `PluginsConfigError`.

### MCP transports

Three transports are supported, exactly as the [MCP
spec](https://modelcontextprotocol.io) and Claude Code's
`--mcp-config` define them. Mixing fields across transports
(e.g. `command` on an `http` entry) crashes boot.

**`stdio`** — local subprocess (default). pyclaudir spawns the
command, talks over stdin/stdout. Auth via the subprocess `env`
block.

```jsonc
{
  "name": "github",
  "type": "stdio",          // optional; default when omitted
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}" },
  "allowed_tools": ["mcp__github"],
  "enabled": true
}
```

**`http`** — remote streamable-HTTP server. Auth via static
`headers`. Use this for hosted MCPs (Linear, Notion-cloud, GitHub's
remote MCP, etc.) where you've already issued a PAT or OAuth token.

```jsonc
{
  "name": "linear",
  "type": "http",
  "url": "https://mcp.linear.app/mcp",
  "headers": { "Authorization": "Bearer ${LINEAR_API_KEY}" },
  "allowed_tools": ["mcp__linear"],
  "enabled": true
}
```

**`sse`** — Server-Sent Events transport. Same field shape as
`http`. Some hosted MCPs use this; check vendor docs.

```jsonc
{
  "name": "events",
  "type": "sse",
  "url": "https://example.com/sse",
  "headers": { "X-API-Key": "${EVENTS_KEY}" },
  "allowed_tools": ["mcp__events"],
  "enabled": true
}
```

**Auth.** Pyclaudir doesn't manage OAuth flows — supply an
already-issued token via `${VAR}` interpolation. For interactive
OAuth-managed servers, see Claude Code's MCP docs (it can run the
flow on your behalf when configured outside `plugins.json`).

### Tool groups

These three groups default to **off**. Flip in `plugins.json`
(`tool_groups.<name>: true`).

#### `bash` — shell execution

| Tool | What it does |
|---|---|
| `Bash` | Run shell commands. |
| `PowerShell` | Run PowerShell commands (Windows / opt-in via `CLAUDE_CODE_USE_POWERSHELL_TOOL`). |
| `Monitor` | Watch a long-running process and stream output back to the model. |

These all share Claude Code's "permission required" risk class — same
trust class. Off by default for safety.

#### `code` — code work

| Tool | What it does |
|---|---|
| `Edit` | Targeted edits to a file. |
| `Write` | Create or overwrite a file. |
| `Read` | Read a file. |
| `NotebookEdit` | Edit Jupyter notebook cells. |
| `Glob` | Find files by glob pattern. |
| `Grep` | Search file contents. |
| `LSP` | Code intelligence — definitions, references, type errors. Requires a code-intelligence plugin to be installed. |

Useful unit when you want the bot to do real code work (not just chat).
The Telegram-assistant deployment leaves this off and relies on memory
+ project.md for everything it needs to remember.

#### `subagents`

| Tool | What it does |
|---|---|
| `Agent` | Spawn a subagent with its own context window for an isolated task. Token-heavy — leave off unless you need it. When off, the subagent docs (`prompts/subagents.md`) aren't even loaded, so the model doesn't know the capability exists. |

### Adding a new external MCP

1. Append an entry to `mcps` in `plugins.json` with a unique `name`,
   the `command` to spawn (and any `args`), the `env` it needs (use
   `${VAR}` to pull credentials from `.env` rather than committing
   them), and `allowed_tools` listing the exact tool names or
   `mcp__<name>` prefix to advertise.
2. Add any referenced env vars to `.env`.
3. Restart: `docker compose up -d --force-recreate`.
4. Tail logs — you should see `mcp <name> configured (...)`. If it
   says `skipped (unresolved ${VAR} ...)`, an env var is empty.

### Disabling a built-in pyclaudir tool

The built-ins under "Always on — pyclaudir built-ins" are
auto-discovered from `pyclaudir/tools/*.py` and registered every
boot. To hide one (e.g. you don't use polls, or you don't want LaTeX
rendering eating context), list its name in `builtin_tools_disabled`:

```jsonc
{
  "builtin_tools_disabled": [
    "create_poll", "stop_poll",
    "render_latex", "render_html", "send_photo"
  ]
}
```

The tool is skipped at MCP registration time — it's never
instantiated, never advertised, and the model has no way to invoke
it. Names must match an exact tool name (the `name` class attribute
on the `BaseTool` subclass — also the cell text in this doc's
tables). A typo crashes boot with the available list.

There is no curated "essential" set — disabling `send_message` mutes
the bot, disabling `read_attachment` makes it blind to inbound
photos and documents. The operator owns this trade-off.

### Disabling a skill

Add the skill's directory name to `skills_disabled`:

```jsonc
{ "skills_disabled": ["render-style"] }
```

Restart. `list_skills` no longer surfaces it and `read_skill` raises
"not found", so envelope-driven invocations
(`<skill name="...">`) can't bypass the toggle either.

### Jira — derived from `JIRA_URL` + `JIRA_USERNAME` + `JIRA_API_TOKEN`

The default `plugins.json` ships an `mcp-atlassian` entry that
references all three vars. When they're set in `.env`, the
`mcp-atlassian` server spawns *and* its 40 Jira tools are added to
`--allowedTools`. To stop advertising Jira while keeping credentials,
flip `enabled: false` on the entry.

Categories: search / CRUD / comments / worklog / transitions /
projects / users / watchers / links / attachments / agile (boards,
sprints) / versions. Confluence, JSM, ProForma, Compass, Bitbucket
tools are **not** allowed (mcp-atlassian bundles them all in one
server, so we list each Jira tool explicitly rather than using a
prefix).

For the canonical Jira tool list see upstream
<https://github.com/sooperset/mcp-atlassian>.

### GitLab — derived from `GITLAB_URL` + `GITLAB_TOKEN`

The default `plugins.json` ships an `mcp-gitlab` entry that
references both vars. When they're set in `.env`, the `mcp-gitlab`
server spawns *and* the `mcp__mcp-gitlab` prefix is added to
`--allowedTools`. Unlike the Jira server, mcp-gitlab is GitLab-only —
the prefix match is safe.

For the canonical GitLab tool list see upstream
<https://github.com/zereight/mcp-gitlab>.

### GitHub — derived from `GITHUB_PERSONAL_ACCESS_TOKEN`

The default `plugins.json` ships a `github` entry that references the
token. When set in `.env`, the `github` MCP server spawns (via `npx
-y @modelcontextprotocol/server-github`) *and* the `mcp__github`
prefix is added to `--allowedTools`. Single-vendor server, blanket
prefix match is safe.

GitHub.com is the default. For GitHub Enterprise, add a
`"GITHUB_HOST": "${GITHUB_HOST}"` line to the `github` plugin's `env`
block in `plugins.json` and set `GITHUB_HOST` (e.g.
`github.example.com`) in `.env`. (The default omits this line so
github.com users aren't blocked by an unset var.)

**How to generate the token (fine-grained PAT — recommended):**

1. Go to <https://github.com/settings/tokens?type=beta>.
2. **Token name:** `pyclaudir` (or your bot's name).
3. **Expiration:** 90 days (max for fine-grained). Set a calendar
   reminder to rotate.
4. **Resource owner:** your account, or the org if the bot acts on
   org repos.
5. **Repository access:** "Only select repositories" → pick exactly
   the repos the bot should touch. Never "All repositories" unless
   that's truly what you want.
6. **Repository permissions** — grant only what the bot needs:
   - `Contents` — Read & write (read code, push branches, commit)
   - `Issues` — Read & write (file bug tickets from chat)
   - `Pull requests` — Read & write (open PRs, comment)
   - `Metadata` — Read (mandatory; auto-granted)
   - `Actions` — Read & write (only if the bot should trigger or
     read CI)
   - Everything else: "No access"
7. Click **Generate token**. Copy the `github_pat_...` string — you
   won't see it again.
8. Paste into `.env` as `GITHUB_PERSONAL_ACCESS_TOKEN=github_pat_...`
   and restart: `docker compose up -d --force-recreate`.

**Avoid classic PATs.** They grant scopes per-org with no per-repo
limit, so a leaked classic token has a much bigger blast radius. The
`?type=beta` URL above gets you the fine-grained kind.

**GitHub Enterprise:** generate the PAT on your Enterprise instance
and also set `GITHUB_HOST=github.your-company.com` in `.env`.

**Swapping the MCP server.** If you'd rather use the official Go-based
[`github/github-mcp-server`](https://github.com/github/github-mcp-server)
instead of the npm package, edit the `github` entry in `plugins.json`:
swap `command`/`args` and adjust the `env` block. The rest of the
plumbing (allowlist, credential interpolation) stays as-is.

---

## How to enable / disable

**Tool groups** — flip in `plugins.json`:

```jsonc
{ "tool_groups": { "bash": true, "code": true, "subagents": false } }
```

**External MCPs** — credentials in `.env`, advertise/disable in
`plugins.json`:

```bash
# Jira (set all three; mcp-atlassian spawns when present)
JIRA_URL=https://your-site.atlassian.net
JIRA_USERNAME=you@example.com
JIRA_API_TOKEN=...

# GitLab (set both)
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=...

# GitHub
GITHUB_PERSONAL_ACCESS_TOKEN=github_pat_...
```

To stop advertising an MCP without removing credentials, flip
`enabled: false` on its entry in `plugins.json`. To remove entirely,
delete the entry.

**Skills** — list directory names in `plugins.json` `skills_disabled`.

Restart the container after any edit: `docker compose up -d
--force-recreate`. Tail logs at startup — `plugins loaded: N enabled
mcp(s), M disabled skill(s), tool_groups={...}` summarises the active
config; `mcp <name> configured (...)` / `mcp <name> skipped (...)`
lines explain each MCP's outcome.

---

## Other CC tools you can wire in

pyclaudir doesn't currently expose every Claude Code built-in. Most
omissions are deliberate (planning/team tools that don't fit the
Telegram-bot context); a fork that wants any of these can add them to
`BASE_ALLOWED_TOOLS` (always on) or define a new gated set in
`pyclaudir/cc_worker.py` (opt-in).

Notable upstream tools not currently exposed:

- **Planning & worktrees** — `EnterPlanMode`, `ExitPlanMode`,
  `EnterWorktree`, `ExitWorktree`. Useful for code-work forks.
- **Task list** — `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate`,
  `TaskStop`, `TodoWrite`. Replace pyclaudir's progress tracking if
  you want CC's native version.
- **Scheduled tasks** — `CronCreate`, `CronDelete`, `CronList`.
  Session-scoped, restored on `--resume`. Could complement
  pyclaudir's reminder system.
- **MCP discovery** — `ListMcpResourcesTool`, `ReadMcpResourceTool`.
  Useful when forks add MCP servers that expose resources beyond
  tools.
- **AskUserQuestion** — multi-choice prompts. Doesn't fit the
  push-driven Telegram flow but might suit forks with a different UI.
- **SendMessage** — agent-team teammate messaging. Experimental;
  requires `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.
- **Skill** — execute a CC-defined skill (different mechanism from
  pyclaudir's `<reminder><skill>` envelope flow).

For each, the upstream
<https://code.claude.com/docs/en/tools-reference> is authoritative —
it lists permission requirements and behaviour notes that may change
between CC versions.
