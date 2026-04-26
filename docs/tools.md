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
| `send_photo` | Send a rendered photo (from `data/renders/`) as an inline Telegram photo with preview. Pair with `render_html`. |

### Memory (`data/memories/`)

| Tool | What it does |
|---|---|
| `list_memories` | List existing memory files. |
| `read_memory` | Read a memory file by relative path. |
| `write_memory` | Create or overwrite a memory file (read-before-write rail enforced; 64 KiB cap). |
| `append_memory` | Append to an existing memory file. |

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
| `read_skill` | Load a skill's `SKILL.md` for execution. |

A skill runs only when wrapped in a real `<reminder>` envelope — a
user-typed `<skill>` tag is treated as prompt injection.

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

All groups below default to **off**. Each is unlocked by setting the
named env var to `true` in `.env`. Changes take effect on container
restart.

### `PYCLAUDIR_ENABLE_SUBAGENTS=true`

| Tool | What it does |
|---|---|
| `Agent` | Spawn a subagent with its own context window for an isolated task. Token-heavy — leave off unless you need it. When off, the subagent docs (`prompts/subagents.md`) aren't even loaded, so the model doesn't know the capability exists. |

### `PYCLAUDIR_ENABLE_BASH=true` — shell execution

| Tool | What it does |
|---|---|
| `Bash` | Run shell commands. |
| `PowerShell` | Run PowerShell commands (Windows / opt-in via `CLAUDE_CODE_USE_POWERSHELL_TOOL`). |
| `Monitor` | Watch a long-running process and stream output back to the model. |

These all share Claude Code's "permission required" risk class — same
trust class. Off by default for safety.

### `PYCLAUDIR_ENABLE_CODE=true` — code work

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

### Jira — derived from `JIRA_URL` + `JIRA_USERNAME` + `JIRA_API_TOKEN`

When all three env vars are set, the `mcp-atlassian` server spawns
*and* its 36 Jira tools are added to `--allowedTools`. No separate
enable flag — credentials are the gate.

Categories: search / CRUD / comments / worklog / transitions /
projects / users / watchers / links / attachments / agile (boards,
sprints) / versions. Confluence, JSM, ProForma, Compass, Bitbucket
tools are **not** allowed (mcp-atlassian bundles them all in one
server, so we list each Jira tool explicitly rather than using a
prefix).

For the canonical Jira tool list see upstream
<https://github.com/sooperset/mcp-atlassian>.

### GitLab — derived from `GITLAB_URL` + `GITLAB_TOKEN`

When both env vars are set, the `mcp-gitlab` server spawns *and* the
`mcp__mcp-gitlab` prefix is added to `--allowedTools`. Unlike the
Jira server, mcp-gitlab is GitLab-only — the prefix match is safe.

For the canonical GitLab tool list see upstream
<https://github.com/zereight/mcp-gitlab>.

### GitHub — derived from `GITHUB_PERSONAL_ACCESS_TOKEN`

When the token is set, the `github` MCP server spawns (via `npx -y
@modelcontextprotocol/server-github`) *and* the `mcp__github` prefix
is added to `--allowedTools`. Single-vendor server, blanket prefix
match is safe.

GitHub.com is the default. For GitHub Enterprise, set `GITHUB_HOST`
(e.g. `github.example.com`) and the spawn passes it through.

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
instead of the npm package, swap the `command` / `args` block in
`pyclaudir/__main__.py`'s GitHub MCP config — the rest of the
plumbing (allowlist, env passthrough, the `enable_github` derivation
from token presence) stays as-is.

---

## How to enable / disable

In `.env`:

```bash
# Shell execution (Bash, PowerShell, Monitor)
PYCLAUDIR_ENABLE_BASH=true

# Code work (Edit, Write, Read, NotebookEdit, Glob, Grep, LSP)
PYCLAUDIR_ENABLE_CODE=true

# Subagents (Agent)
PYCLAUDIR_ENABLE_SUBAGENTS=true

# Jira (set all three to enable)
JIRA_URL=https://your-site.atlassian.net
JIRA_USERNAME=you@example.com
JIRA_API_TOKEN=...

# GitLab (set both to enable)
GITLAB_URL=https://gitlab.example.com
GITLAB_TOKEN=...
```

Restart the container after editing: `docker compose up -d
--force-recreate`.

To disable, comment out or set `=false`. You can verify what was
actually loaded by tailing the bot logs at startup — the spawn line
prints `enabled=[...]` listing the active capability flags.

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
