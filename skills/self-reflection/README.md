# self-reflection skill

Daily two-phase loop that drives the bot's own learning:

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
  promote / refine / discard to the owner via DM. On explicit owner
  approval, appends the rule to `prompts/project.md` via the
  owner-DM-gated `append_instructions` tool.

Both phases run back-to-back in one invocation, triggered by a
single auto-seeded recurring reminder (22:00 Tashkent / 17:00 UTC
by default). The reminder is mandatory — attempts to cancel it are
refused at the tool layer, and if it ever goes missing (manual SQL,
DB corruption, etc.) the startup hook re-seeds it. Learning does
not stop.

The bot reads `SKILL.md` via the `read_skill` MCP tool when a
`<reminder>` envelope arrives containing
`<skill name="self-reflection">run</skill>`.

See `SKILL.md` for the full playbook.
