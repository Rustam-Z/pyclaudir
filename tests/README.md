# Tests

Two suites: fast **unit** tests (no network), and opt-in **e2e** tests that drive
the real bot over the real Telegram API.

## Unit tests

Fully mocked — no Telegram, no network, no `claude`. Fast; run them anytime:

```bash
uv run -m pytest tests/unit
```

## E2E tests

Drive a real `hamroh` bot from a real Telegram user account (Telethon) and
assert on what the bot actually does — DB rows, files, media, latency. Opt-in:
without credentials they **skip**, so a plain `pytest` stays green.

One-time setup (a test bot + a tester-account session) is in
[`e2e/README.md`](e2e/README.md).

```bash
uv run -m pytest -m e2e                  # all e2e tests
uv run -m pytest -m "e2e and not slow"   # skip the ~2-min reminder-fire test
```

Each run boots one throwaway bot (test token, temp data dir), drives it, then
kills it. Every reply is a real `claude` turn, so runs cost tokens and time.

### What's covered (in a DM and in a group)

- **Response** — a DM and a group message get a reply
- **Auth / no-auth** — an unauthorized group is silently ignored + logged
- **Burst** — several quick messages are all answered, none dropped
- **Reactions** — the bot adds an emoji reaction (👍) to a message
- **Reply / context** — reply linkage is recorded; facts carry across turns
- **Memory** — a note is written to disk, recalled, and survives `/reset_session`
- **Reminders** — scheduled with the right time, and actually fires (`@slow`)
- **Skills** — the bot reads a skill via its skills tools
- **Render** — a diagram comes back as a photo + a PNG lands on disk

### Owner commands (the tester account is the owner)

- **/pause · /resume** — muting stops replies; resume answers again, session stays warm
- **/health · /audit · /access** — read-only status readouts
- **/allow · /deny · /policy** — mutate `access.json` (state restored after each test)
- **/usage** — relays Claude Code's own usage report
- **/reset_session** — respawns Claude; history + memories survive
- **/kill** — the bot process exits (runs against a throwaway bot)

### Speed eval

The eval runs inside the e2e suite (`test_eval_e2e.py`): each scenario across DM
and group, logging pass rate, p50/p95 latency, and tool time per feature. Raise
the run count for trustworthy percentiles:

```bash
E2E_EVAL_RUNS=5 uv run pytest -m e2e
```
