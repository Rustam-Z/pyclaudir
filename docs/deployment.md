# Deployment Guide

This guide covers deploying pyclaudir to a VPS (Contabo, Hetzner,
DigitalOcean, etc.) using Docker, and setting up a continuous deployment
workflow.

## Prerequisites

- A VPS with SSH access
- A GitHub repo with your pyclaudir code
- A Telegram bot token (from @BotFather)
- A Claude Code account (for API authentication)

## Initial server setup (one-time)

```bash
# SSH into your server
ssh root@your-server-ip

# Install Docker
curl -fsSL https://get.docker.com | sh

# Install Node.js + Claude Code CLI and authenticate
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
npm install -g @anthropic-ai/claude-code
claude   # interactive login — creates ~/.claude/

# Clone your private repo (SSH auth — add server's public key to GitHub first)
#   On server: ssh-keygen -t ed25519 (if no key exists)
#   Copy ~/.ssh/id_ed25519.pub → GitHub Settings → SSH keys
git clone git@github.com:your-user/pyclaudir.git ~/pyclaudir
cd ~/pyclaudir

# Configure
cp .env.example .env
vim .env   # set TELEGRAM_BOT_TOKEN, PYCLAUDIR_OWNER_ID, etc.
cp prompts/project.md.example prompts/project.md
vim prompts/project.md   # customize identity, integrations, team info

# Build and start
docker compose up -d --build

# Verify it's running
docker compose ps
docker compose logs -f   # should see "pyclaudir is live"
```

DM your bot on Telegram to confirm it replies.

### Enabling capabilities

The bot ships with a tight default surface — Telegram messaging,
memory tools, reminders, and read-only web access only. Shell,
code-editing, subagents, Jira, and GitLab are **all off by default**.
Each group is unlocked by a single env var (or, for Jira/GitLab,
the presence of credentials):

```bash
# In .env
PYCLAUDIR_ENABLE_BASH=true        # Bash, PowerShell, Monitor
PYCLAUDIR_ENABLE_CODE=true        # Edit, Write, Read, NotebookEdit, Glob, Grep, LSP
PYCLAUDIR_ENABLE_SUBAGENTS=true   # Agent
```

For the per-tool list and trade-offs, see [tools.md](tools.md).
Restart the container after toggling: `docker compose up -d
--force-recreate`.

## Update workflow

### Manual (SSH)

Every time you push changes to GitHub:

```bash
ssh root@your-server-ip 'cd ~/pyclaudir && git pull && docker compose up -d --build'
```

Or step by step:

```bash
ssh root@your-server-ip
cd ~/pyclaudir
git pull
docker compose up -d --build
docker compose logs -f   # verify it started correctly
```

### Automatic (GitHub Actions)

Create `.github/workflows/deploy.yml` in your repo:

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.SERVER_IP }}
          username: root
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd ~/pyclaudir
            git pull
            docker compose up -d --build
```

Then add these secrets to your GitHub repo (Settings → Secrets and
variables → Actions):

| Secret | Value |
|--------|-------|
| `SERVER_IP` | Your VPS IP address |
| `SSH_PRIVATE_KEY` | Contents of `~/.ssh/id_ed25519` (generate with `ssh-keygen -t ed25519` and add the public key to the server's `~/.ssh/authorized_keys`) |

Every push to `main` will automatically deploy to your server.

**Note:** Since the repo is private, the server needs SSH access to
GitHub for `git pull` to work. Make sure the server's SSH key
(`~/.ssh/id_ed25519.pub`) is added as either:

- A **deploy key** on the repo (Settings → Deploy keys) — scoped to
  this repo only, recommended
- Or an **SSH key** on your GitHub account (Settings → SSH keys) —
  grants access to all your repos

## The `data/` directory

The `data/` directory is created automatically on first run. It contains:

- `data/memories/` — bot's persistent memory files (the only part worth
  migrating between deployments)
- `data/pyclaudir.db` — SQLite database (messages, users, reminders,
  tool call logs) — starts fresh on new servers
- `data/session_id` — Claude Code session ID for conversation continuity
- `data/attachments/` — inbound photos/docs the dispatcher saved
- `data/renders/` — outbound PNGs from `render_html`
- `data/cc_logs/` — raw Claude Code subprocess logs

Headless Chromium for `render_html` is pre-installed in the Docker
image (`playwright install --with-deps chromium`) — no per-host
provisioning step needed.

**First deployment:** nothing to do — the bot creates everything.

**Migrating from another server:** only copy memories:

```bash
scp -r old-server:~/pyclaudir/data root@new-server:~/pyclaudir/data
```

Don't copy `session_id` or `pyclaudir.db` to a new server — stale
session IDs cause CC subprocess crashes, and the database will rebuild
naturally from new messages.

## Syncing memories and config

Use the included sync script to keep memory files and project config
in sync between your local machine and the server:

```bash
# Pull latest memories from server
./scripts/sync-memories.sh pull root@your-server-ip

# Push updated project.md to server
./scripts/sync-memories.sh push root@your-server-ip

# Both (pull memories, then push project.md)
./scripts/sync-memories.sh sync root@your-server-ip
```

After pushing `project.md`, restart for changes to take effect:

```bash
ssh root@your-server-ip 'cd ~/pyclaudir && docker compose restart'
```

## Common operations

```bash
# View live logs
ssh root@your-server-ip 'cd ~/pyclaudir && docker compose logs -f'

# Shell into the container
ssh root@your-server-ip 'cd ~/pyclaudir && docker compose exec pyclaudir bash'

# Restart without rebuilding
ssh root@your-server-ip 'cd ~/pyclaudir && docker compose restart'

# Stop the bot
ssh root@your-server-ip 'cd ~/pyclaudir && docker compose down'

# Check status
ssh root@your-server-ip 'cd ~/pyclaudir && docker compose ps'
```

## Troubleshooting

### Telegram conflict error

```
Conflict: terminated by other getUpdates request
```

Another instance is polling the same bot token. Make sure only one is
running — check both local (`pkill -f 'python -m pyclaudir'`) and
Docker (`docker compose down`).

### CC subprocess crashes (rc=1, empty stderr)

Common causes:

- **Stale session ID** — delete `data/session_id` and restart. This
  happens after renaming the project folder or moving to a new server.
- **MCP server not reachable** — the `--strict-mcp-config` flag makes
  Claude exit if any MCP server in the config fails to connect. Check
  that `uvx` and `npx` are available inside the container.

### Claude Code auth expired

SSH into the server and re-authenticate:

```bash
ssh root@your-server-ip
claude   # follow the login flow
cd ~/pyclaudir && docker compose restart
```
