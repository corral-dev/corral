# Shareable wrap card design (`corral wrap`)

Status: direction and product decisions approved by the owner on 2026-11-04;
implementation in progress.

## 1. Decision and why

A shareable usage card ("wrap") ships as a Corral subcommand. It is not a new
TUI product and it is not a SessKit feature.

- Corral is the product people keep using; a shared card should lead new users
  into it rather than into a single-purpose tool.
- SessKit keeps its boundary: it interprets native history and exposes
  normalized data. Presentation belongs to downstream products.
- The growth rationale and channel plan stay in the private growth guide
  (`~/.config/agentsync/docs/GITHUB_STAR_GROWTH_GUIDE.md` §3.9, item 4).

## 2. Ownership

| Layer | Owns |
|---|---|
| SessKit | Session scan, typed activity events (`load_activity`), usage snapshots (`load_usage`), real event timestamps, native error evidence |
| Corral | Period selection, active-interval policy, aggregation, card layout, colors, branding footer, optional export |

The aggregation reads SessKit's normalized output only; it adds no parser.
If a needed fact is missing from SessKit's normalized output, add it upstream
first (SessKit contract and live checks), then consume the published version.

## 3. Data feasibility (owner's machine, 2026-11-04)

Read-only probe over the last 7 days with SessKit's public API:

- Scan: 4,325 sessions in 16 s; 379 active in the week (OpenCode 222, Pi 62,
  Claude 47, Codex 46).
- `load_activity`: 377 available, 2 unsupported, 3.6 s total; 38,764 tool calls,
  8,271 assistant messages, 1,741 user messages.
- `load_usage`: 364 available, 13 empty, 2 unsupported, 10.5 s total.
- A cold run is about 30 s. The card should reuse Corral's existing scan and
  derived caches, show progress while reading, and never present a partial
  read as a complete total.

## 4. Metric definitions

All values come from real parsed output. Unsupported or unreadable sessions
are counted and disclosed on the card (for example "2 sessions not readable"),
never silently treated as zero.

- **Sessions:** sessions with at least one activity event inside the period.
- **Agent share:** percentage of those sessions per runtime.
- **Tool calls:** `tool_call` events inside the period.
- **Tokens:** sum of SessKit usage summaries for in-period records; coverage
  gaps reported as such. Monetary amounts only when recorded natively; no
  price lookup or estimate.
- **Peak parallel sessions:** the maximum number of sessions that were active
  at the same moment. A session's active intervals are built from its event
  timestamps, merging consecutive events whose gap is at most 5 minutes
  (Corral policy constant). Using the first-to-last event span is wrong: idle
  sessions left open across days inflated the probe's result to 20.

## 5. Card content constraints

- Local only: nothing is uploaded, no network call.
- Built for sharing: no prompts, message text, session titles, file paths or
  project names on the default card.
- Footer: one line linking to the public Corral repository.
- Terminal rendering follows the terminal UI knowledge base and the global UI
  standards; visual acceptance needs an inspected fresh capture.

## 6. Product decisions (owner, 2026-11-04)

1. **Tokens yes, money no.** The card shows native token totals with their
   coverage. It shows no monetary amount, recorded or estimated.
2. **Default period: last 7 days.** `--month` (30 days) and `--all` (all
   retained history) are explicit options. Claude keeps about 30 days of
   transcripts by default, so longer periods disclose each runtime's coverage.
3. **Output: terminal card plus image export.** The terminal card is the
   default; an export option writes a 1200×630 social image built from the
   same aggregated values, never a separately computed set.

## 7. Market context (searched 2026-11-04)

Many Claude Code "wrapped" cards already exist, for example
[claude-code-wrapped](https://github.com/kschrader/claude-code-wrapped),
[agent-wrapped](https://github.com/lfgcampos/agent-wrapped),
[wrapped-mod](https://github.com/Ron-Nefyodov/wrapped-mod) (SVG export) and
[Year in Code](https://news.ycombinator.com/item?id=46401120) (built on
ccusage). [ccusage](https://github.com/ccusage/ccusage) now reports several
agents and drove screenshot sharing through
[a show-and-tell thread](https://github.com/ryoppippi/ccusage/discussions/26).
Corral's distinguishing content is cross-runtime coverage of five agents and
parallel-session work, so those metrics lead the card.
