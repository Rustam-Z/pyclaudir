# render-style skill

House style for the `render_html` tool. Reference material the agent
reads on its own initiative — no `<reminder>` envelope, no scheduled
trigger.

## What it is

A style spec + three copy-paste HTML skeletons for the dark
dashboard / architecture-diagram look the operator settled on:

- **Dashboard** — left-aligned title, stat tiles with colored accent
  bars, horizontal bar rows, verdict block. For metrics, scorecards,
  calibration reports.
- **Timeline / pipeline** — centered title, numbered colored circles,
  step cards with optional timing pills and callout bands. For
  lifecycles, flows, ordered processes.
- **Architecture diagram** — centered title, tier labels, panels with
  colored left borders, connector arrows with labels, persona pills.
  For systems, components, tier maps.

Shared tokens: deep-navy bg (`#0e1828`), `--card`, semantic color
palette (green = good, blue = neutral, red = bad, amber = pending,
purple = command/owner, cyan = IO, gray = N/A). System fonts only —
the rendering browser has all outbound network blocked.

## How the agent uses it

1. Bot decides a reply needs a visual (table, chart, diagram, flow).
2. `read_skill render-style` — pulls in tokens, layout rules, the
   skeleton library.
3. Picks the matching layout mode; copies the skeleton; substitutes
   real content into the placeholders.
4. `render_html` → PNG under `data/renders/`.
5. `send_photo` → delivers the image inline to the chat.

The pointer to read this skill before rendering lives in
`prompts/system.md` next to the `render_html` capability note.

## Why a skill (not a memory file)

Skills are git-tracked under `skills/`; memory files live under
`data/memories/` which is gitignored. Style guides should ship with
the repo, survive `data/` resets, and be visible to anyone reviewing
the codebase. Skills are also discoverable through `list_skills` so
the bot can find them without operator priming.

## Editing

Update `SKILL.md` directly. Keep the YAML frontmatter
(`name`, `description`, `license`, `compatibility`) intact — the
skills loader rejects malformed files at startup.

When adding a new layout mode:

1. Add a "Mode-specific rules" subsection.
2. Add a `Skeleton N — <name>` block with a complete `<!DOCTYPE html>`
   document.
3. Verify it renders cleanly via the real pipeline (the project's
   `tests/test_render_html.py` end-to-end test is a good reference for
   the wiring).
4. Update the `description` frontmatter to mention the new mode.

See `SKILL.md` for the spec.
