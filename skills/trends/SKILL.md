---
name: trends
description: On-demand global research playbook covering tech, startup, AI, finance, future, and economy. Sweeps X / Reddit / HN / Business Insider / LinkedIn / big-tech earnings / AI-lab blogs, then synthesises a two-minute digest with money-making angles.
license: MIT
compatibility: Requires WebFetch, WebSearch, telegram_send_message.
metadata:
  pyclaudir-invocation: '<skill name="trends">run</skill>'
---

# Skill: trends

You are running the **trends playbook**. Goal: a single focused digest
a human can read in under two minutes that flags money-making
opportunities and surfaces real inflections across tech, startup, AI,
finance, future, and economy.

## Scope

Six themes. Pull facts; don't editorialise the sources, do editorialise
the synthesis.

- **Tech + product moves** — launches, deprecations, big platform
  shifts (Apple/Microsoft/Google APIs, browser/runtime changes).
- **Startups + VC** — named funding rounds ≥ $50M, notable seed bets
  with named founders, M&A, fund launches.
- **AI labs** — OpenAI, Anthropic, Google DeepMind, Meta (FAIR), and
  Chinese labs (DeepSeek, Qwen/Alibaba, Zhipu, Moonshot, Baidu). Model
  releases, papers, pricing changes, policy posts.
- **Finance + markets** — big-tech earnings deltas (AAPL, MSFT, GOOGL,
  AMZN, META, NVDA, TSLA), rate moves, market inflections, crypto if
  news-worthy.
- **Future / regulation** — EU / US / China laws that shift the playing
  field; energy, chips, climate when consequential.
- **Economy** — macro prints with real effect: CPI surprises, jobs
  shocks, GDP revisions. Skip routine noise.

Sources to sweep:

- **Hacker News** — front page + top "Show HN" of the day.
- **Business Insider** — tech + markets headlines.
- **Reddit** — r/technology, r/MachineLearning, r/startups,
  r/wallstreetbets top-of-day. Note when something jumped a subreddit
  it doesn't usually live in.
- **X (Twitter)** — what's trending in tech / VC / AI today. Names of
  shipped products, viral threads, notable researcher posts.
- **LinkedIn** — senior-leader posts going viral; hiring shifts (mass
  layoffs or mass hires at named companies).
- **Big-tech earnings calls** — when one is recent. Surface the deltas
  ("ad rev +X%", "cloud growth slowed to Y%"), not the marketing spin.

## Research procedure

1. **Determine the lookback window.** Call
   `reminder_list(chat_id=<firing chat>)` and find the reminder that
   just fired (match by `text` containing this skill's invocation tag).
   Read its `cron` field and map cadence → window:
   - Daily cadence (e.g. `0 9 * * *`, hourly variants) → **1 day**
   - Weekly cadence (cron with a weekday slot like `* * * * MON`,
     or `@weekly`) → **7 days**
   - Monthly cadence → **30 days**
   - One-shot (no cron) or unclear → default **1 day**

   Filter every rolling source (HN, Reddit, X, LinkedIn, news sites,
   lab blogs) to items inside this window. Older items are noise.
2. **Plan first, fetch second.** Pick the 6–10 sources most likely to
   carry signal in the window (earnings week? AI lab event? rate
   decision?). Don't sweep all 20+ — you'll run out of turn.
3. **Send a heads-up.** This is a long task — the §Long tasks rule in
   `system.md` applies. `telegram_send_message` to the target `chat_id`: e.g.
   *"Pulling the trends digest — ~2 min."* Not "On it".
4. **Fetch in parallel.** Issue multiple `WebFetch` / `WebSearch` calls
   in a single turn — the harness runs them concurrently, so 8 fetches
   finish in roughly the time of 1. Don't fetch serially. Use
   `WebSearch` for "what's trending in X today" type queries and
   `WebFetch` for known URLs (HN, lab blogs, earnings transcripts). For
   each source, capture 2–4 concrete data points with names, numbers,
   dates inside the lookback window. Skip anything you can't attribute
   to a fetched URL — no training-memory facts in the digest.
5. **Cite with exact URLs.** Every claim carries the *exact* URL of the
   specific item it came from — the article, post, paper, or thread. No
   bare domain links, no homepage links, no "via @handle". If a source
   doesn't expose a per-item permalink for the fact, drop the bullet —
   don't paper over it.
6. **Refuse internal URLs.** Same rule as system.md §Capabilities: no
   localhost, RFC1918, `.local`. If a user-suggested source is internal,
   drop it and note why.

## Synthesis

Three sections. Keep each tight — this is a two-minute read.

1. **Top of the day** (1–3 lines). The single most consequential thing
   across all sources today. Why it matters in one clause. Carries its
   primary URL inline as `[source](exact-url)`.
2. **Signals** (~5–8 bullets). One bullet per distinct signal. Format:
   `• <one-clause headline> — <why it matters / what to do>.
   ([source](exact-url))` — `exact-url` is the specific article / post
   / permalink, never a homepage or handle.
3. **Money-making angles** (1–3 bullets). Concrete: a market entering
   or leaving, a pricing shift, an arbitrage, a hiring trend that
   implies a tool-gap. Each angle names *who* could act and *how*, and
   links the underlying signal with `([source](exact-url))`.

Tone: §Tone in `system.md`. Concrete nouns and numbers, no
"significant", no emoji decoration.

## Delivery

`telegram_send_message` to the `chat_id` from the triggering. Markdown
formatting per system.md §Outgoing message formatting (bullets `•`,
flow `→`, no headers, no tables).

Send **exactly one** `telegram_send_message` per run. No follow-ups, no
continuations. If the draft is approaching 4096 chars, tighten before
sending: drop the lowest-signal bullets first, shorten clauses, merge
near-duplicates. Never split into a second message. Per §Keep outputs
tight in `system.md`, 4096 is a ceiling, not a target — aim well under
it.

## Must follow

- Every bullet carries an **exact URL** to the specific source item —
  no bare domain links, no homepage links, no uncited facts.
- **One** `telegram_send_message` per run. Trim, don't split.

## Don'ts

- Don't fabricate. If a source didn't load, say so and skip the bullet
  — never invent a number to fill the slot.
- Don't include Uzbekistan-scene items — that's the `trends-uzbekistan`
  skill. Keep this digest global.
- Don't write to memory unless the owner asks. The digest is ephemeral
  by design.
- Don't auto-schedule a follow-up. The owner triggers the next run.
