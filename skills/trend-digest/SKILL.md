---
name: trend-digest
description: On-demand research playbook. Sweeps WORLD (tech, finance, AI labs, X / Reddit / HN / Business Insider / LinkedIn / big-tech earnings) and UZBEKISTAN (local startups, VC, fintech, gov tech, Uzbek TG channels and venture sites, hackathons) sources, then synthesizes a digest aimed at spotting money-making trends and Uzbekistan-relevant moves.
license: MIT
compatibility: Requires WebFetch, WebSearch, send_message.
metadata:
  pyclaudir-invocation: '<skill name="trend-digest">run</skill>'
---

# Skill: trend-digest

You are running the **trend-digest playbook**. Goal: a single
focused digest human can read in under two minutes that
flags (a) money-making opportunities in tech/finance/AI, and
(b) Uzbekistan-specific moves worth acting on.

## Scope

Two buckets, in this order. Pull facts; don't editorialise the
sources, do editorialise the synthesis.

### WORLD

- **Tech + finance digest, world news with future impact.** Big
  moves only — funding rounds ≥ $100M, regulation, market
  inflections. Skip celebrity drama and gadget reviews.
- **X (Twitter) trends.** What's trending in tech / VC / AI
  Twitter today. Names of shipped products, viral threads,
  notable researcher posts.
- **Reddit trends.** r/technology, r/MachineLearning,
  r/startups, r/wallstreetbets top-of-day. Note when something
  jumped a subreddit it doesn't usually live in.
- **Business Insider.** Tech + markets headlines.
- **LinkedIn trends.** Senior-leader posts going viral, hiring
  shifts (mass layoffs, mass hires at named companies).
- **Big AI labs.** OpenAI, Anthropic, Google DeepMind, Meta
  (FAIR), and Chinese labs (DeepSeek, Qwen/Alibaba, Zhipu,
  Moonshot, Baidu). Model releases, papers, pricing changes,
  policy posts.
- **Hacker News.** Front page + top "Show HN" of the day.
- **Big-tech earnings calls.** When one is recent: AAPL, MSFT,
  GOOGL, AMZN, META, NVDA, TSLA. Surface the deltas — "ad rev
  +X%", "cloud growth slowed to Y%" — not the marketing spin.

### UZBEKISTAN

- **Venture, tech, finance digest.** Local funding rounds,
  M&A, new market entries.
- **Local startups.** New VC funds, new startups (especially
  ones with named founders worth knowing).
- **Fintech updates.** Payments, neobanks, lending,
  crypto-adjacent.
- **Government tech initiatives.** New laws, IT Park news,
  digital-economy programs.
- **Telegram channels (primary signal).** Sweep these:
  `@stanbsse`, `@uzbekretail`, `@uzbekvc`, `@uzbekfintech`,
  `@uzbbanking`, `@skartariss`, `@kurbanoffnet`. Also `spot.uz`.
  Use WebFetch on `https://t.me/s/<channel>` for public
  channels — that's the web-readable preview.
- **Uzbek venture / VC sites.** UzVC, IT Park portfolio pages,
  any disclosed-deal trackers.
- **Local hackathons.** Upcoming or just-closed, with prize
  pool and theme.

## Research procedure

1. **Plan first, fetch second.** Before opening WebFetch,
   pick the 6–10 sources most likely to carry signal *today*
   given the date and what's known to be happening (earnings
   week? AI lab event? Uzbek budget season?). Don't try to
   sweep all 20+ sources — you'll run out of turn.
2. **Send a heads-up.** This is a long task — the §Long
   tasks rule in `system.md` applies. `send_message` to the
   target `chat_id`: e.g. *"Pulling today's trend-digest
   digest — ~2 min."* Not "On it".
3. **Fetch.** Use `WebSearch` for "what's trending in X
   today" type queries and `WebFetch` for known URLs (TG
   previews, HN, lab blogs). For each source, capture: 2–4
   concrete data points with names, numbers, dates. Skip
   anything you can't attribute to a fetched URL — no
   training-memory facts in the digest.
4. **Cite.** Every claim in the digest carries a source —
   either a URL you actually fetched, or "via @channel"
   for TG. No bare numbers.
5. **Refuse internal URLs.** Same rule as system.md
   §Capabilities: no localhost, RFC1918, `.local`. If a
   user-suggested source is internal, drop it and note why.

## Synthesis

The digest has four sections. Keep each tight — this is a
two-minute read.

1. **Top of the day** (1–3 lines). The single most
   consequential thing across all sources today. Why it
   matters in one clause.
2. **WORLD** (bulleted, ~5–8 bullets). One bullet per
   distinct signal. Format: `• <one-clause headline> — <why
   it matters / what to do>. (source)`
3. **UZBEKISTAN** (bulleted, ~3–6 bullets). Same format.
   Empty if nothing new — say so explicitly, don't pad.
4. **Money-making angles** (1–3 bullets). Concrete: a market
   entering / leaving, a pricing shift, an arbitrage, a hiring
   trend that implies a tool-gap. Each angle names *who*
   could act and *how*.

Tone: §Tone in `system.md`. Concrete nouns and numbers, no
"significant", no emoji decoration.

## Delivery

`send_message` to the `chat_id` from the triggering.
Markdown formatting per system.md §Outgoing message
formatting (bullets `•`, flow `→`, no headers, no tables).

If the synthesis would exceed Telegram's 4096-char ceiling,
split at section boundaries: send "Top of the day + WORLD"
first, then "UZBEKISTAN + Money-making angles" as a follow-up.
Don't truncate mid-bullet.

## Don'ts

- Don't fabricate. If a source didn't load, say so and skip
  the bullet — never invent a number to fill the slot.
- Don't include the violence / social-media monitoring item
  — that's a separate future feature, not part of this skill.
- Don't write to memory unless the owner asks. The digest is
  ephemeral by design.
- Don't auto-schedule a follow-up. The owner triggers the
  next run when they want one.
