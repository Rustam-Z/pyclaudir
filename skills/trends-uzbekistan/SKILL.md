---
name: trends-uzbekistan
description: On-demand Uzbekistan research playbook. Sweeps local startups, VC, fintech, gov tech, hackathons, and Uzbek Telegram channels (uzbekvc, uzbekfintech, uzbbanking, skartariss, stanbsse, spot.uz). Synthesises a two-minute digest with money-making angles for the Uzbek market.
license: MIT
compatibility: Requires WebFetch, WebSearch, telegram_send_message.
metadata:
  hamroh-invocation: '<skill name="trends-uzbekistan">run</skill>'
---

# Skill: trends-uzbekistan

You are running the **trends-uzbekistan playbook**. Goal: a single
focused digest a human can read in under two minutes that flags local
moves worth acting on for an Uzbek operator/investor.

## Scope

Pull facts; don't editorialise the sources, do editorialise the
synthesis.

- **Venture, tech, finance.** Local funding rounds, M&A, new market
  entries.
- **Local startups.** New VC funds, new startups (especially ones
  with named founders worth knowing).
- **Fintech.** Payments, neobanks, lending, crypto-adjacent.
- **Government tech.** New laws, IT Park news, digital-economy
  programs.
- **Telegram channels (primary signal).** Sweep these: `@stanbsse`,
  `@uzbekretail`, `@uzbekvc`, `@uzbekfintech`, `@uzbbanking`,
  `@skartariss`, `@kurbanoffnet`. Also `spot.uz`. Use WebFetch on
  `https://t.me/s/<channel>` for public channels — that's the
  web-readable preview.
- **Uzbek venture / VC sites.** UzVC, IT Park portfolio pages, any
  disclosed-deal trackers.
- **Local hackathons.** Upcoming or just-closed, with prize pool and
  theme.

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

   Filter every rolling source to items inside this window. For
   Telegram channels especially, only include messages whose
   `https://t.me/<channel>/<message_id>` timestamp falls in the window
   — older posts are noise even if they're still visible on the
   preview page.
2. **Sweep every Telegram channel; pick the rest.** **All** Telegram
   channels listed in §Scope (`@stanbsse`, `@uzbekretail`, `@uzbekvc`,
   `@uzbekfintech`, `@uzbbanking`, `@skartariss`, `@kurbanoffnet`, plus
   `spot.uz`) must be fetched every run — they're the primary signal
   and the list is short enough to fan out in one parallel turn (step
   4). On top of those, pick 2–4 secondary sources (UzVC, IT Park,
   hackathon pages, news sites) most likely to carry signal in the
   window.
3. **Send a heads-up.** This is a long task — the §Long tasks rule in
   `system.md` applies. `telegram_send_message` to the target `chat_id`: e.g.
   *"Pulling the Uzbekistan digest — ~2 min."* Not "On it".
4. **Fetch in parallel.** Issue multiple `WebFetch` / `WebSearch` calls
   in a single turn — the harness runs them concurrently, so 8 fetches
   finish in roughly the time of 1. Don't fetch serially. Use
   `WebFetch` on known TG previews and Uzbek news pages, `WebSearch`
   for fresh queries. For each source, capture 2–4 concrete data points
   with names, numbers, dates inside the lookback window. Skip
   anything you can't attribute to a fetched URL — no training-memory
   facts.
5. **Cite with exact URLs.** Every claim carries the *exact* URL of the
   specific item — the per-message Telegram permalink
   (`https://t.me/<channel>/<message_id>`, visible in the
   `https://t.me/s/<channel>` preview), the article URL, the deal
   page. No bare channel handles, no homepage links, no "via @channel".
   If a preview doesn't expose a per-item permalink for the fact, drop
   the bullet — don't paper over it with the channel root.
6. **Refuse internal URLs.** Same rule as system.md §Capabilities: no
   localhost, RFC1918, `.local`.

## Synthesis

Three sections. Keep each tight — this is a two-minute read.

1. **Top of the day** (1–3 lines). The single most consequential local
   thing today. Why it matters in one clause. Carries its primary URL
   inline as `[source](exact-url)`.
2. **Local moves** (~3–6 bullets). One bullet per distinct signal.
   Format: `• <one-clause headline> — <why it matters / what to do>.
   ([source](exact-url))`. If nothing new today, say so explicitly —
   don't pad with stale items. Uzbek news is lumpier than global.
3. **Money-making angles for Uzbekistan** (1–3 bullets). Concrete: a
   market entering or leaving, a regulatory window opening, a hiring
   trend that implies a tool-gap, an arbitrage between Tashkent and a
   nearby market. Each angle names *who* could act and *how*, and
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

- **Sweep every Telegram channel listed in §Scope on every run.** No
  exceptions — they're the primary signal. Parallel fetches make the
  full sweep cheap.
- Every bullet carries an **exact URL** to the specific source item —
  no bare channel handles, no homepage links, no uncited facts.
- **One** `telegram_send_message` per run. Trim, don't split.

## Don'ts

- Don't fabricate. If a source didn't load, say so and skip the
  bullet — never invent a number to fill the slot.
- Don't include global / non-Uzbek items — that's the `trends` skill.
  Keep this digest local.
- Don't write to memory unless the owner asks. The digest is ephemeral
  by design.
- Don't auto-schedule a follow-up. The owner triggers the next run.
