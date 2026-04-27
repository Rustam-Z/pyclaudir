---
name: reminder-format
description: House format for the text passed to set_reminder. Read before calling set_reminder, and again before editing a reminder (cancel + re-create). Three rules — open with "This is a reminder", state the goal in one line, then list numbered steps the future-self should run when the reminder fires. Keeps fired <reminder> envelopes self-explanatory and actionable.
license: MIT
compatibility: Requires the set_reminder tool.
---

# Skill: reminder-format

Reference skill (no `<reminder>` envelope required). Read it before
calling `set_reminder` — and again before editing a reminder (which
means `cancel_reminder` + a fresh `set_reminder`). The format applies
to the `text` argument; cron and `trigger_at` are unaffected.

The reminder text is a self-prompt: when the timer fires, this exact
string lands in your input as `<reminder>…</reminder>` and re-orients
a fresh turn. Past-you is briefing future-you. Write it that way.

## The three rules

1. **Opening line.** The first line of `text` is exactly
   `This is a reminder.` — literal, including the period. It anchors
   future-you immediately, before any envelope parsing.
2. **Goal line.** One short sentence right after the opener, prefixed
   `Goal:`. State *why* this reminder exists — the outcome past-you
   wanted.
3. **Steps.** A numbered list (`1.`, `2.`, …) of the concrete actions
   future-you should take when it fires. One line per step, imperative
   voice ("send X", "check Y", "DM the owner if Z"). No nesting, no
   sub-bullets.

## Example

```
This is a reminder.
Goal: follow up with Alice on the GitLab MR she opened yesterday.
1. Read the MR status via WebFetch.
2. If still open, DM Alice asking if she needs review help.
3. If merged, send a thumbs-up reaction in the original chat.
```

## Don'ts

- No markdown headers, no prose paragraphs in place of steps.
- Don't skip the opener even for one-liners — the literal phrase is
  the cue.
