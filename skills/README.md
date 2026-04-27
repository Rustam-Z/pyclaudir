# skills/

Operator-curated playbooks the bot can read at runtime. Each skill is
a directory under `skills/` with a `SKILL.md` (the agent-facing spec)
and an optional `README.md` (the human-facing overview).

This directory is git-tracked and ships with the repo — the bot's
`skills_store` reads from it directly. Memory files (`data/memories/`)
are for run-time state; skills are for durable, repo-shipped
reference material and playbooks.

## Layout

```
skills/
├── README.md              ← you are here
├── self-reflection/       ← daily auto-seeded learning loop
│   ├── SKILL.md
│   └── README.md
├── render-style/          ← house style for render_html
│   ├── SKILL.md
│   └── README.md
└── reminder-format/       ← house format for set_reminder text
    └── SKILL.md
```

## Catalogue

| Skill | Mode | What it does |
|---|---|---|
| [self-reflection](self-reflection/) | invoked | Daily two-phase loop: introspect last 24h of outbound behavior, stress-test pending lessons against scenarios, propose promote / refine / discard rules to the owner. Triggered by an auto-seeded reminder; refused without a real `<reminder>` envelope. |
| [render-style](render-style/) | reference | Style guide for the `render_html` tool — dark dashboard / timeline / architecture-diagram look with CSS tokens, layout rules, and three copy-paste HTML skeletons. Read on the agent's own initiative before any `render_html` call. |
| [reminder-format](reminder-format/) | reference | Three-rule format for the `set_reminder` text argument — `"This is a reminder."` opener, `Goal:` line, numbered steps. Read before creating or editing any reminder so fired `<reminder>` envelopes are self-explanatory. |

## Skill modes

- **Invoked.** The agent runs the playbook only when wrapped in a real
  `<reminder>` envelope containing
  `<skill name="...">run</skill>`. A user-typed `<skill>` tag is
  treated as prompt injection and refused. Used for executable
  workflows that should be auditable and operator-triggered (e.g.
  `self-reflection`).
- **Reference.** The agent reads the skill on its own initiative
  whenever the situation calls for it (e.g. `render-style` before
  calling `render_html`). No envelope required — the content is
  passive style/spec material, not an action.

The mode is determined by what `SKILL.md` instructs the agent to do,
not by a frontmatter flag.

## SKILL.md spec

Every `SKILL.md` follows the
[Agent Skills specification](https://agentskills.io/specification):
YAML frontmatter with at least `name` and `description`, body in
markdown. The `name` must match the parent directory name (lowercase,
hyphenated). Files are capped at 256 KiB; descriptions at 1024 chars.

Surfaced via:

- `list_skills` — returns name + description for every well-formed
  skill the agent can use to choose what to read.
- `read_skill <name>` — returns the full body so the agent can apply
  it.

Path resolution is hardened the same way the memory store is —
no `..`, no symlinks, must stay inside `skills/`.

## Adding a skill

1. `mkdir skills/<name>` (lowercase, hyphenated).
2. Write `skills/<name>/SKILL.md` with valid frontmatter and a body
   describing the playbook or reference material.
3. Optional: `README.md` for human readers.
4. Optional: extend `prompts/system.md` if the skill should be
   discovered automatically before a specific tool call (the way
   `render-style` is referenced before `render_html`).
5. Restart the bot — the skills store re-scans on startup and the new
   skill becomes available via `list_skills` immediately.

The bot **never writes** to `skills/`. The store is read-only by
design; operators curate this directory by hand or via PR.
