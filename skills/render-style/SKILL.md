---
name: render-style
description: House style for the render_html tool — dark dashboard / architecture-diagram look with stat tiles, bar rows, numbered timeline steps, two-panel grids, status pills, connector arrows, callout bands, and verdict blocks. Read this skill before calling render_html so the output is visually consistent. Provides CSS tokens, layout rules, color semantics, and three copy-paste HTML skeletons to adapt.
license: MIT
compatibility: Requires the render_html and send_photo tools.
---

# Skill: render-style

Reference skill (no `<reminder>` envelope required). Read it before
calling `render_html` and adapt the closest skeleton — don't redesign.

There are three layout modes; pick the one that fits the content:

- **Dashboard** — left-aligned title, stat tiles, bar rows, verdict.
  For metrics, scorecards, calibration reports.
- **Timeline / pipeline** — centered title, numbered steps in colored
  circles, each step a card. For lifecycles, flows, ordered processes.
- **Architecture diagram** — centered title, two-panel grid or stacked
  tiers, inline pills, connector arrows with labels, footer line. For
  systems, components, tier maps.

All three share the same tokens, fonts, and section-header style.

## Tokens (paste these CSS vars verbatim)

```
--bg:#0e1828;          /* page background */
--card:#1a2332;        /* tile / card / track background */
--card2:#141d2c;       /* nested panel background (slightly darker) */
--line:rgba(255,255,255,.06); /* dividers, tile borders */
--tx:#ffffff;          /* primary text, big numbers */
--tx2:#9ba8bd;         /* body / secondary */
--tx3:#6b7a91;         /* uppercase labels, captions */
--blue:#3b82f6;        /* neutral / info / partial / step 1 */
--green:#10b981;       /* correct / positive / safe / step 3 */
--red:#ef4444;         /* wrong / negative / stop / crash */
--amber:#f59e0b;       /* pending / warning / step 4 */
--purple:#8b5cf6;      /* command / owner / auth / step 5 */
--cyan:#06b6d4;        /* IO / pipeline / step 2 */
--gray:#6b7280;        /* N/A, unverifiable */
```

Color semantics:

- **green** — good outcome, safe path, "correct"
- **blue** — neutral, info, "partial", first step in a sequence
- **red** — bad outcome, "wrong", "stop", crash branch
- **amber** — pending, warning, "early", interrupt callout
- **purple** — command / owner / auth tier, end of sequence
- **cyan** — IO, network, pipeline middles
- **gray** — unknown, N/A, unverifiable

Use one semantic color per element. Don't mix accents on a single
tile/card/step.

## Shared layout rules

- Render at **width 1280, height 800**. Full-page captures overflow.
  Content max-width 1200px, centered, 40px page padding.
- Body font: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
  Mono for code/identifiers: `ui-monospace, "SF Mono", Menlo, monospace`.
- Section header (when used): 12px, uppercase, letter-spacing `.12em`,
  color `--tx3`, with a 1px `--line` underline. Margins 32px top, 20px
  bottom. **Never** style real `<h2>`/`<h3>` for sections — use `.sec`.
- Numbers carry their unit: `67%`, `$1.2M`, `+18%`, `<1ms`, `≤300ms`.
- Inline code in prose: `<code>` styled with `--card` bg, mono font,
  small horizontal padding, 4px radius. Use for identifiers, file names,
  function calls — anything verbatim from the system.
- Inline pills/chips: small rounded badges (8px radius), 11px uppercase
  tracked, semi-transparent fill of the semantic color (use
  `rgba(...,.15)` or `color-mix`). Status, role, timing tags.

## Mode-specific rules

### Dashboard

- Title left-aligned, h1 34px bold, subtitle line below in `--tx2` 14px
  with bullet separators (`•`).
- Stat tiles: `--card` bg, 12px radius, 24×28px padding, **4px colored
  accent bar** on the left edge via `::before` (inset 12px top/bottom,
  not full-height).
- Bar row: 160px label column + flex track + colored fill with white
  bold count inside; minimum fill 60px so single-digit counts read.
- Verdict block at the bottom: full-width tile, centered, "VERDICT"
  header in `--tx3` tracked caps, body 18px bold.

### Timeline / pipeline

- Title centered, h1 30-34px bold, subtitle centered in `--tx2`.
- Each step row: a large colored circle (44-56px, white digit, bold,
  centered) on the left; step card on the right with title + body.
- Step card: `--card` bg, 1px `--line` border, 12px radius, padding
  18×22px. **No** left accent bar — the circle is the accent.
- Optional **timing pill** floated right of the step title:
  `<span class="pill cyan">≤300ms</span>` etc.
- Optional **callout band** inside a step (red-outlined warning, etc.):
  `<div class="callout red">…</div>`.
- Steps stacked vertically with 14-16px gap. Numbers run 1, 2, 3 — colors
  cycle blue → cyan → green → amber → purple → red, etc.
- Optional **bottom row** of small uniform cards (4-up grid) for
  shared-state / glossary items. Each is `--card` bg with title + mono
  type signature in `--tx3`.

### Architecture diagram

- Title centered, h1 30-34px bold, subtitle centered.
- Either **two side-by-side panels** (`grid-template-columns:1fr 1fr`,
  ~24px gap) or **vertical tiers** (single column, with arrow rows
  between).
- Panel: `--card` bg, 12px radius, 1px `--line` border, padding 22×24px.
  Panel title in 18-20px bold; tag pills (e.g. "IN HARNESS") inline.
- Tier label above each tier: small uppercase tracked label like
  `TIER 0 — COMMAND` in `--tx3`.
- **Connector arrow** between tiers/panels: a thin vertical line in
  `--line` with a small label badge centered on it (e.g. "monitors &
  restarts"). Use `.arrow` + `.arrow .label`.
- **Persona/role row** at the bottom of a tier: inline dot+name pills
  (`<span class="dotpill purple"><i></i>Nodira — CEO</span>`).
- **Footer line** centered in `--tx3` 12px with em-dash separators.

## Skeleton 1 — Dashboard (calibration / scorecard)

```html
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root{--bg:#0e1828;--card:#1a2332;--card2:#141d2c;--line:rgba(255,255,255,.06);
    --tx:#fff;--tx2:#9ba8bd;--tx3:#6b7a91;
    --blue:#3b82f6;--green:#10b981;--red:#ef4444;--amber:#f59e0b;
    --purple:#8b5cf6;--cyan:#06b6d4;--gray:#6b7280}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--tx);
    font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:40px}
  .wrap{max-width:1200px;margin:0 auto}
  h1{font-size:34px;font-weight:700;margin-bottom:8px}
  .sub{color:var(--tx2);font-size:14px;border-bottom:1px solid var(--line);
    padding-bottom:20px;margin-bottom:32px}
  .sec{font-size:12px;font-weight:600;color:var(--tx3);text-transform:uppercase;
    letter-spacing:.12em;border-bottom:1px solid var(--line);
    padding-bottom:10px;margin:32px 0 20px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:24px 28px;position:relative;overflow:hidden}
  .tile::before{content:"";position:absolute;left:0;top:12px;bottom:12px;
    width:4px;border-radius:2px;background:var(--accent,var(--blue))}
  .tile.green{--accent:var(--green)}.tile.blue{--accent:var(--blue)}
  .tile.red{--accent:var(--red)}.tile.amber{--accent:var(--amber)}
  .tile.purple{--accent:var(--purple)}.tile.cyan{--accent:var(--cyan)}
  .lbl{font-size:11px;color:var(--tx3);text-transform:uppercase;
    letter-spacing:.14em;margin-bottom:14px}
  .num{font-size:64px;font-weight:700;line-height:1}
  .note{color:var(--tx2);font-size:13px;margin-top:14px}
  .bar{display:flex;align-items:center;gap:16px;margin-bottom:10px}
  .bar .name{width:160px;color:var(--tx2);font-size:14px}
  .bar .track{flex:1;background:var(--card);border-radius:8px;height:36px;
    overflow:hidden;border:1px solid var(--line)}
  .bar .fill{height:100%;display:flex;align-items:center;padding:0 12px;
    color:#fff;font-weight:700;border-radius:8px;background:var(--c,var(--blue))}
  .bar.green .fill{--c:var(--green)}.bar.blue .fill{--c:var(--blue)}
  .bar.red .fill{--c:var(--red)}.bar.amber .fill{--c:var(--amber)}
  .bar.gray .fill{--c:var(--gray)}
  .verdict{background:var(--card);border:1px solid var(--line);
    border-radius:12px;padding:28px;text-align:center;margin-top:24px}
  .verdict .h{font-size:11px;color:var(--tx3);letter-spacing:.18em;
    text-transform:uppercase;margin-bottom:14px}
  .verdict .t{font-size:18px;font-weight:700;line-height:1.5;color:var(--tx)}
</style></head><body><div class="wrap">

  <h1>Title goes here</h1>
  <div class="sub">subtitle • metadata • n=… • source</div>

  <div class="grid2">
    <div class="tile blue"><div class="lbl">strict accuracy</div>
      <div class="num">67%</div><div class="note">fully correct only (8 / 12)</div></div>
    <div class="tile green"><div class="lbl">directional accuracy</div>
      <div class="num">100%</div><div class="note">correct + partial, zero hard misses</div></div>
  </div>

  <div class="sec">score distribution (n=12)</div>
  <div class="bar green"><div class="name">Correct</div>
    <div class="track"><div class="fill" style="width:80%">8</div></div></div>
  <div class="bar blue"><div class="name">Partially correct</div>
    <div class="track"><div class="fill" style="width:40%">4</div></div></div>
  <div class="bar red"><div class="name">Wrong</div>
    <div class="track"><div class="fill" style="width:6%">0</div></div></div>

  <div class="verdict"><div class="h">verdict</div>
    <div class="t">One- or two-sentence call. Bold, centered, no period spam.</div></div>

</div></body></html>
```

## Skeleton 2 — Timeline / pipeline (lifecycle, flow)

```html
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root{--bg:#0e1828;--card:#1a2332;--card2:#141d2c;--line:rgba(255,255,255,.06);
    --tx:#fff;--tx2:#9ba8bd;--tx3:#6b7a91;
    --blue:#3b82f6;--green:#10b981;--red:#ef4444;--amber:#f59e0b;
    --purple:#8b5cf6;--cyan:#06b6d4;--gray:#6b7280}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--tx);
    font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:40px}
  .wrap{max-width:1100px;margin:0 auto}
  .head{text-align:center;margin-bottom:32px}
  .head h1{font-size:32px;font-weight:700;margin-bottom:8px}
  .head .sub{color:var(--tx2);font-size:14px}
  .step{display:grid;grid-template-columns:64px 1fr;gap:18px;margin-bottom:14px;align-items:start}
  .step .circle{width:48px;height:48px;border-radius:50%;display:flex;
    align-items:center;justify-content:center;color:#fff;font-weight:700;
    font-size:20px;background:var(--c,var(--blue));margin-top:4px;
    box-shadow:0 0 0 4px rgba(255,255,255,.04)}
  .step.blue .circle{--c:var(--blue)}.step.cyan .circle{--c:var(--cyan)}
  .step.green .circle{--c:var(--green)}.step.amber .circle{--c:var(--amber)}
  .step.purple .circle{--c:var(--purple)}.step.red .circle{--c:var(--red)}
  .step .body{background:var(--card);border:1px solid var(--line);
    border-radius:12px;padding:16px 20px;border-left:3px solid var(--c,var(--blue))}
  .step.blue .body{--c:var(--blue)}.step.cyan .body{--c:var(--cyan)}
  .step.green .body{--c:var(--green)}.step.amber .body{--c:var(--amber)}
  .step.purple .body{--c:var(--purple)}.step.red .body{--c:var(--red)}
  .step .ttl{display:flex;justify-content:space-between;align-items:center;
    gap:10px;margin-bottom:6px}
  .step .ttl h3{font-size:17px;font-weight:700;color:var(--tx)}
  .step .txt{color:var(--tx2);font-size:14px}
  .pill{display:inline-block;font-size:11px;font-weight:600;text-transform:uppercase;
    letter-spacing:.08em;padding:3px 9px;border-radius:8px;border:1px solid currentColor}
  .pill.blue{color:var(--blue);background:rgba(59,130,246,.12)}
  .pill.cyan{color:var(--cyan);background:rgba(6,182,212,.12)}
  .pill.green{color:var(--green);background:rgba(16,185,129,.12)}
  .pill.amber{color:var(--amber);background:rgba(245,158,11,.12)}
  .pill.red{color:var(--red);background:rgba(239,68,68,.12)}
  .pill.purple{color:var(--purple);background:rgba(139,92,246,.12)}
  code,.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;
    background:var(--card2);padding:1px 6px;border-radius:4px;font-size:13px;color:var(--tx)}
  .callout{margin-top:10px;padding:10px 14px;border-radius:8px;
    border:1px solid var(--c,var(--red));background:rgba(239,68,68,.08);font-size:13px}
  .callout.amber{--c:var(--amber);background:rgba(245,158,11,.08)}
  .callout .h{font-size:11px;font-weight:700;letter-spacing:.12em;
    text-transform:uppercase;color:var(--c,var(--red));margin-bottom:4px}
  .glossary{margin-top:28px;background:var(--card2);border:1px solid var(--line);
    border-radius:12px;padding:20px}
  .glossary .h{font-size:12px;font-weight:600;color:var(--tx3);
    text-transform:uppercase;letter-spacing:.12em;text-align:center;margin-bottom:16px}
  .glossary .row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
  .glossary .cell{background:var(--card);border:1px solid var(--line);
    border-radius:8px;padding:12px;text-align:center}
  .glossary .cell .n{font-weight:700;color:var(--tx);margin-bottom:4px}
  .glossary .cell .t{font-family:ui-monospace,"SF Mono",Menlo,monospace;
    font-size:12px;color:var(--tx3)}
</style></head><body><div class="wrap">

  <div class="head">
    <h1>Claude Code Subprocess Lifecycle</h1>
    <div class="sub">Persistent AI session managed via stdin/stdout JSON streaming</div>
  </div>

  <div class="step blue"><div class="circle">1</div><div class="body">
    <div class="ttl"><h3>Spawn</h3></div>
    <div class="txt">Build claude command with all flags. Set <code>PR_SET_PDEATHSIG</code>
    (Linux: child dies if parent dies). Capture stdin/stdout/stderr pipes.</div>
  </div></div>

  <div class="step cyan"><div class="circle">2</div><div class="body">
    <div class="ttl"><h3>Initialize</h3><span class="pill cyan">≤300ms</span></div>
    <div class="txt">Send first message ("Session started/resumed"). Wait for System
    message → validate tool list (security check). Wait for first Result.</div>
  </div></div>

  <div class="step amber"><div class="circle">4</div><div class="body">
    <div class="ttl"><h3>Engine Processing</h3><span class="pill amber">≤1s</span></div>
    <div class="txt">Drains pending queue, formats as XML, sends to CC. If CC already
    active → <b>injects mid-turn</b> via sync channel.</div>
    <div class="callout"><div class="h">Owner interrupt (! prefix)</div>
    Bypasses all queues → direct stdin write to CC within 1 second.</div>
  </div></div>

  <div class="step purple"><div class="circle">5</div><div class="body">
    <div class="ttl"><h3>Claude Code Turn</h3><span class="pill purple">~2.5s</span></div>
    <div class="txt">AI processes message, calls MCP tools (<code>send_message</code>,
    <code>query_db</code>, <code>get_image</code>…), returns control action.</div>
  </div></div>

  <div class="glossary"><div class="h">Shared State: Survives CC Restarts</div>
    <div class="row">
      <div class="cell"><div class="n">PID</div><div class="t">Arc&lt;AtomicI32&gt;</div></div>
      <div class="cell"><div class="n">Heartbeat</div><div class="t">Arc&lt;AtomicU64&gt;</div></div>
      <div class="cell"><div class="n">Inject Sender</div><div class="t">Arc&lt;Mutex&lt;Sender&gt;&gt;</div></div>
      <div class="cell"><div class="n">Pending Injects</div><div class="t">Arc&lt;Mutex&lt;Vec&gt;&gt;</div></div>
    </div>
  </div>

</div></body></html>
```

## Skeleton 3 — Architecture diagram (tiers, panels, connectors)

```html
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  :root{--bg:#0e1828;--card:#1a2332;--card2:#141d2c;--line:rgba(255,255,255,.06);
    --tx:#fff;--tx2:#9ba8bd;--tx3:#6b7a91;
    --blue:#3b82f6;--green:#10b981;--red:#ef4444;--amber:#f59e0b;
    --purple:#8b5cf6;--cyan:#06b6d4;--gray:#6b7280}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--tx);
    font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:40px}
  .wrap{max-width:900px;margin:0 auto}
  .head{text-align:center;margin-bottom:28px}
  .head h1{font-size:30px;font-weight:700;margin-bottom:6px}
  .head .sub{color:var(--tx2);font-size:14px}
  .tier{margin-bottom:6px}
  .tier .label{font-size:11px;color:var(--tx3);font-weight:700;
    letter-spacing:.14em;text-transform:uppercase;margin-bottom:8px}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:18px 22px;border-left:4px solid var(--c,var(--blue))}
  .panel.blue{--c:var(--blue)}.panel.green{--c:var(--green)}
  .panel.purple{--c:var(--purple)}.panel.red{--c:var(--red)}
  .panel.amber{--c:var(--amber)}.panel.cyan{--c:var(--cyan)}
  .panel .ttl{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .panel .ttl h2{font-size:18px;font-weight:700}
  .panel .ico{font-size:20px}
  .panel .desc{color:var(--tx2);font-size:14px}
  .pill{display:inline-block;font-size:11px;font-weight:600;text-transform:uppercase;
    letter-spacing:.08em;padding:3px 9px;border-radius:8px;border:1px solid currentColor}
  .pill.blue{color:var(--blue);background:rgba(59,130,246,.12)}
  .pill.green{color:var(--green);background:rgba(16,185,129,.12)}
  .pill.amber{color:var(--amber);background:rgba(245,158,11,.12)}
  .pill.red{color:var(--red);background:rgba(239,68,68,.12)}
  .pill.purple{color:var(--purple);background:rgba(139,92,246,.12)}
  .arrow{display:flex;flex-direction:column;align-items:center;margin:6px 0}
  .arrow .line{width:1px;height:18px;background:var(--line)}
  .arrow .label{font-size:11px;color:var(--tx3);background:var(--card2);
    border:1px solid var(--line);padding:3px 10px;border-radius:6px;margin:2px 0}
  .roles{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px}
  .dotpill{display:inline-flex;align-items:center;gap:8px;font-size:13px;
    background:var(--card2);border:1px solid var(--line);border-radius:999px;padding:4px 12px}
  .dotpill i{width:8px;height:8px;border-radius:50%;background:var(--c,var(--purple));display:inline-block}
  .dotpill.blue i{--c:var(--blue)}.dotpill.green i{--c:var(--green)}
  .dotpill.amber i{--c:var(--amber)}.dotpill.red i{--c:var(--red)}
  .dotpill.purple i{--c:var(--purple)}.dotpill.cyan i{--c:var(--cyan)}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  .footer{text-align:center;color:var(--tx3);font-size:12px;margin-top:24px;
    padding-top:16px;border-top:1px solid var(--line)}
  code{font-family:ui-monospace,"SF Mono",Menlo,monospace;
    background:var(--card2);padding:1px 6px;border-radius:4px;font-size:13px;color:var(--tx)}
</style></head><body><div class="wrap">

  <div class="head">
    <h1>Claudir: Three-Tier Architecture</h1>
    <div class="sub">Separation of concerns by access level</div>
  </div>

  <div class="tier"><div class="label">tier 0 — command</div>
    <div class="panel purple">
      <div class="ttl"><span class="ico">🛡️</span><h2>Owner + Supervisor</h2>
        <span class="pill purple">unrestricted</span></div>
      <div class="desc">Owner controls via Telegram (<code>bot_xona</code> / DMs). Supervisor
      is raw Claude Code in terminal for manual intervention.</div>
    </div></div>
  <div class="arrow"><div class="line"></div><div class="label">can start / stop / approve</div><div class="line"></div></div>

  <div class="tier"><div class="label">tier 1 — operations</div>
    <div class="panel green">
      <div class="ttl"><span class="ico">⚙️</span><h2>Mirzo</h2>
        <span class="pill green">full permissions</span></div>
      <div class="desc">CTO bot. Bash, Edit, Write via CC. Owner DMs only. Monitors Tier 2,
      delegates to agent teams.</div>
    </div></div>
  <div class="arrow"><div class="line"></div><div class="label">monitors &amp; restarts</div><div class="line"></div></div>

  <div class="tier"><div class="label">tier 2 — public-facing</div>
    <div class="panel red">
      <div class="ttl"><span class="ico">🤖</span><h2>Nodira &amp; Dilya</h2>
        <span class="pill red">sandboxed</span></div>
      <div class="desc">Public chatbots. WebSearch only via CC. MCP tools validated by
      harness. No code execution.</div>
      <div class="roles">
        <span class="dotpill blue"><i></i>Nodira — CEO</span>
        <span class="dotpill amber"><i></i>Dilya — Newcomer</span>
      </div>
    </div></div>

  <div class="footer">Same Rust binary (claudir) — Different configs — Shared SQLite communication</div>

</div></body></html>
```

## Don'ts

- Don't use external fonts/CSS/JS — network is blocked. System fonts only.
- Don't use multiple accent colors per tile/card/step. One semantic
  color, one job.
- Don't add gradients or shadows beyond what's in the skeleton, or emoji
  decoration in body text. Emoji icons in panel titles are OK.
- Don't write headers as `<h2>`/`<h3>` for sections. Use `.sec` (or
  `.head h1` for centered diagram titles).
- Don't omit units on numbers. `67%`, `≤300ms`, never bare.
- Don't mix layout modes in one render — pick dashboard, timeline, or
  diagram and commit.
- Don't repeat the calibration colors when the content suggests
  pipeline ordering — for sequences use the cycle blue → cyan → green
  → amber → purple → red.
