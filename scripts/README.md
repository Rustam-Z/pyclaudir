# scripts/

Operator helpers — shell scripts run by hand on the host (not by the
bot). Python scripts the bot uses live in `pyclaudir/scripts/`.

| Script | What it does |
|---|---|
| `sync-memories.sh` | rsync `data/memories/` and `prompts/project.md` between local machine and remote server. Pull / push / dry-run modes. |
| `prune-backups.sh` | Trim `data/prompt_backups/` to the newest 50 backups. Safe to run anytime; safe to schedule via cron. |

Each script self-documents with `--help` (or its header comment).
Make executable once: `chmod +x scripts/*.sh`.
