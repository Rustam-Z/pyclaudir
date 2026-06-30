---
name: Committed memories — how to add files
description: How to create git-tracked memory files in this folder (frontmatter, layout, commit flow).
---

# Committed memories

This folder holds **durable, git-tracked memories** for the bot. Unlike the
runtime store at `data/memories/` (which is gitignored and lives only on the
server's Docker volume), everything here is committed into the repo — so it
survives a volume loss, a server rebuild, or a move to a new machine, and it
has full git history.

The bot reads this folder **as memory**, exactly like the runtime store:
`memory_list`, `memory_search`, and `memory_read` span both. It is **read-only
to the bot** — the bot never writes here. You (the operator) curate these files
by hand and commit them with `git`.

Nothing in here is loaded into the system prompt. Memories are read on demand,
the same as the runtime store.

## How to create a memory file

1. Create a `.md` file anywhere under this folder (subfolders are fine).
2. Start it with the required frontmatter block — `name` and `description`:

   ```markdown
   ---
   name: <short human-friendly label>
   description: <one-line summary used to find this memory without reading it>
   ---

   <body — the actual remembered content>
   ```

   The `description` is what `memory_list` shows, so write it so the bot can
   decide whether a file is relevant **without** reading the whole thing.

3. Keep each file under **64 KiB** (same cap as the runtime store).
4. Commit and push:

   ```bash
   git add memories/
   git commit -m "memories: add <what you added>"
   git push
   ```

## Layout (mirror the runtime store)

```
memories/
├── README.md                       # this file
├── docs/{topic}-{YYYY-MM-DD}.md    # one-off reports / audits
└── notes/
    ├── {topic}.md                  # cross-session reference notes
    └── references.md               # misc references, IDs, lookups
```

## Runtime vs committed — which is which

| | `data/memories/` (runtime) | `memories/` (this folder) |
|---|---|---|
| Tracked in git | No (gitignored) | Yes |
| Survives volume loss | No | Yes |
| Bot can write | Yes | No (operator-curated) |
| Read as memory | Yes | Yes |

If the same relative path exists in both, the **runtime** copy wins (it's the
live one). Use this folder for knowledge you want to keep permanently in the
repo; let the bot's own day-to-day notes live in the runtime store.
