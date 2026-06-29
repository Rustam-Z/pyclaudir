# Real end-to-end tests

These tests drive the **real bot over the real Telegram API**: a Telegram
*user* account sends a message, the live bot (a real `claude` subprocess)
replies, and we assert on the result and measure how long it took.

A bot can't message or read another bot, so the "sender" must be a normal
user account. We use [Telethon](https://docs.telethon.dev) for that.

The whole suite is **opt-in**. Without the e2e credentials in the root `.env`
(or without the `claude` CLI), every test **skips** — a plain `pytest` stays
green.

The bot under test runs with the **same** root config as the real app —
`.env`, `plugins.json`, and `access.json` — so there is no separate test
config. Only its *data* dir is isolated to a temp folder. One consequence: e2e
uses the same `TELEGRAM_BOT_TOKEN` as your app, so a run will stop a locally
running bot to claim the token.

## What you need (one-time)

| Thing | How to get it |
| --- | --- |
| Your **bot's `@username`** | The handle of the bot configured by `TELEGRAM_BOT_TOKEN` in `.env`. Goes in `E2E_BOT_USERNAME`. |
| **User API creds** | <https://my.telegram.org> → API development tools → `api_id` + `api_hash` for the account that will play the tester. |
| A **session string** | Generated once with the snippet below (so tests log in without a phone prompt). |
| A **test group** | A group with the tester account and the bot in it. Add its numeric id to `access.json`'s `allowed_chats` — that's where the suite reads it from (add several to round-robin across them). |

Then:

1. **DM the bot once** from the tester account (send `/start`). Telegram won't
   deliver bot→user messages to someone who never opened the chat.
2. The harness always **@mentions** the bot in group messages, so the bot's
   privacy mode can stay on. (If you prefer, disable it via BotFather
   `/setprivacy → Disable`.)

### Generate the session string

Run the helper once, in your own terminal:

```bash
.venv/bin/python tests/e2e/support/make_session.py
```

It asks for your `api_id`/`api_hash` (if not already in `.env`), then your phone
number, the login code, and your 2FA password (if set). It writes
`E2E_TG_SESSION` and `HAMROH_OWNER_ID` into the root `.env` for you —
preserving every other line — then you fill in `E2E_BOT_USERNAME` by hand. (The
test group is not an env var; it comes from `access.json` — see below.) Treat
the session string like a password: it grants full access to that account.

## Environment variables

All of these live in the root `.env`. The app vars (`TELEGRAM_BOT_TOKEN`,
`HAMROH_MODEL`, `HAMROH_EFFORT`, …) are the ones the bot already uses; the
suite adds only the tester-client vars below.

| Var | Meaning |
| --- | --- |
| `E2E_TG_API_ID` / `E2E_TG_API_HASH` | Tester account's API creds. |
| `E2E_TG_SESSION` | The session string from above. |
| `E2E_BOT_USERNAME` | The bot's username (with or without `@`). |
| `HAMROH_OWNER_ID` | The tester account's **own** numeric user id — the app's owner. The bot treats it as owner, so DMs and owner commands pass. |

The test group is **not** an env var: it comes from `access.json`'s
`allowed_chats` (the `-100…` form for supergroups). With several entries the
group tests round-robin across them; with none, a group test errors.

These are read from the root `.env` automatically (no manual `export`), or from
the real environment, which takes precedence. The file is gitignored.

The bot is launched in a throwaway data directory, so your real `data/` is
untouched. It uses the real root `access.json` (the owner is always allowed in
DMs; the test group must be in `allowed_chats`). To override an env value for
the SUT without editing `.env`, add it to `SUT_ENV_OVERRIDES` in
`support/config.py` — the single override point.

## Running

```bash
# everything (skips cleanly if E2E_* unset)
pytest -m e2e

# skip the slow reminder-fire test (~90s)
pytest -m "e2e and not slow"

# one file
pytest tests/e2e/test_memory_e2e.py -m e2e
```

Each run starts one bot subprocess for the whole session and reuses it; tests
stay independent by using a unique token per test. Before any test runs, the
harness waits for the bot to be 100% ready: it waits for the `hamroh is live`
log line, then sends one warm-up DM and waits for the reply, which forces the
`claude` CLI to finish loading its tools/MCP servers. The bot's own log lines
(`[RX]`/`[TX]` traffic, `hot-path … t_ms=…` timing) stream live — the harness
forwards them through the `hamroh.sut` logger and `log_cli` (set in
`pyproject.toml`) prints them. On failure, the last 400 lines are also dumped.

Every reply spawns a real `claude` turn, so a run costs real model tokens and
takes real wall-clock time. The bot uses whatever `HAMROH_MODEL` /
`HAMROH_EFFORT` your `.env` sets. To keep e2e cheap and fast without changing
`.env`, set `SUT_ENV_OVERRIDES` in `support/config.py` (e.g. a cheap model and
`HAMROH_EFFORT="low"`).

## Speed eval

The eval runs as part of the suite: `test_eval_e2e.py` sends each scenario in
`support/models.py` across DM and group, logs a per-(feature, chat) table of
pass rate, p50/p95 latency, and mean tool time per turn, and fails only if the
correctness pass-rate drops below `E2E_EVAL_MIN_PASS` (default 0.9). Latency is
reported, not gated — a single sample is too noisy. Raise the run count for
trustworthy percentiles:

```bash
E2E_EVAL_RUNS=5 pytest -m e2e
```

The `tool_s` column (sum of `tool_calls.duration_ms`) versus the turn latency is
the attribution signal: when a turn takes seconds but its tools take a fraction
of one (e.g. memory read/write), the time is Claude inference + Telegram
round-trips, not tool I/O.

## Response-time limits

On top of the eval's aggregate reporting, every DM/group feature test
**hard-asserts** that its observable lands within a per-kind limit (the
`MAX_*` constants in `support/config.py`). Reply latency is judged on the
first chunk (`assert_reply_within`); other observables are timed with
`measured(...)` and checked with `assert_within`:

| Observable | Limit | Tests |
| --- | --- | --- |
| text answer | 5s | basic, context, reply-linkage |
| adds an emoji reaction | 5s | reactions |
| an owner command acks (`/pause`, `/resume`, `/health`, `/audit`, `/access`, `/allow`, `/deny`, `/policy`) | 5s | pause, owner-readouts, access-management |
| every reply to a 3-message burst | 10s | burst |
| `/usage` (shells out to `claude --print /usage`) | 10s | usage |
| the bot process exits after `/kill` | 10s | kill |
| writes/reads a memory file | 15s | memory |
| reads a skill / schedules a reminder | 30s | skills, reminders (scheduled) |
| `/reset_session` respawns the engine | 30s | reset-session |
| renders an image | 60s | render |
| a scheduled reminder fires | 160s | reminders (fires, delayed by design) |

These are a forcing requirement, not a description of today's speed: a plain
text turn currently runs ~5–8s, so the 5s text tests can go red until the bot
gets faster. The lone exception is the access test — it asserts the bot stays
**silent** in an unauthorized group, so there is no reply to time (its 8s
silence window is the time bound).

## Layout

```
tests/e2e/
├── conftest.py              fixtures + skip-gate (must live here for pytest)
├── README.md
├── test_*.py               the actual tests
└── support/                all machinery — not collected as tests
    ├── config.py           env loading + E2EConfig + child_env/overrides + budgets (MAX_*)
    ├── harness.py          launches the bot subprocess and waits until ready
    ├── state.py            read-only DB + file inspection of the running bot
    ├── models.py           Conversation, Reply value objects + eval Scenario dataset
    ├── data.py             unique test tokens + recall prompts
    ├── client.py           Telethon client: send a message, time/collect the reply
    ├── assertions.py       assert_within / assert_reply_within
    ├── waits.py            generic measure/poll utilities
    ├── eval.py             latency + correctness eval core
    └── make_session.py     one-time login to capture your session string
```
