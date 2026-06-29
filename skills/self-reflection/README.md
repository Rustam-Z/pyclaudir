# self-reflection skill

Daily loop that drives the bot's own learning:

- **Phase A — introspect.** Looks at the last 24h of outbound
  behavior (recent messages, their reactions, tool-call patterns)
  and writes any new candidate lessons into `learnings.md`. This
  complements the "on correction" rule in `system.md`: users aren't
  the only source of learning signal, and the bot can catch some
  drift itself.
- **Phase B — process.** Picks up every `[pending]` entry in
  `learnings.md` (both phase-A's fresh additions and anything
  written earlier via the on-correction rule), stress-tests each
  against 10-20 hypothetical scenarios, scores fit, and proposes
  promote / refine / discard to the owner. Each promote/refine
  candidate names a **suggested target** — `prompts/project.md` for a
  durable behavioral rule (via `instruction_append`, applied on
  restart) or a memory file for a fact/context (via `memory_append`,
  live immediately). The owner approves and can redirect the target
  in their reply ("send 2 to memory"). The moment an entry resolves
  (promote/refine/discard) its prose is compacted to a one-line
  tombstone — the rule now lives in `project.md` and the full
  reasoning in `self/reflections/`, so keeping the body would only
  grow `learnings.md` toward its 64 KiB cap.
- **Phase C — sweep.** Safety net after Phase B: compacts any
  resolved entry still carrying multi-line prose (e.g. left behind by
  an older skill version), regardless of age. Keeps `learnings.md`
  bounded so `memory_read` never truncates.

Phases run back-to-back in one invocation, triggered by a
single auto-seeded recurring reminder (midnight UTC every day by
default — `HAMROH_SELF_REFLECTION_CRON` overrides). The reminder
is mandatory — attempts to cancel it are
refused at the tool layer, and if it ever goes missing (manual SQL,
DB corruption, etc.) the startup hook re-seeds it. Learning does
not stop.

The bot reads `SKILL.md` via the `skill_read` MCP tool when a
`<reminder>` envelope arrives containing
`<skill name="self-reflection">run</skill>`.

See `SKILL.md` for the full playbook.
