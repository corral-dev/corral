# Shareable wrap card design (`corral wrap`)

Status: implemented 2026-11-04 (`src/corral/wrap_stats.py`, `wrap_card.py`,
`wrap_png.py`, `wrap_cmd.py`, `ui/wrap_app.py`; tests `tests/test_wrap.py`).
Verification evidence is in §9.

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

- **Sessions:** sessions with at least one activity event inside the period,
  taken from Corral's listing. That listing drops sessions whose working
  directory no longer exists; on 2026-11-04 those were 99 sessions in deleted
  temporary directories (test harness runs), so they are excluded on purpose.
- **Agent share:** percentage of those sessions per runtime.
- **Tool calls:** `tool_call` events inside the period.
- **Tokens:** SessKit usage records carry no timestamp, so in-period tokens
  use `summarize_usage` with a `UsageSelection` of the message ids whose
  activity events fall inside the period. A session whose whole history lies
  inside the period contributes its full summary. Records that cannot be
  attributed are disclosed as uncovered, never guessed. No monetary amount
  (owner decision, §7).
- **Peak parallel sessions:** the maximum number of sessions that were active
  at the same moment. A session's active intervals are built from its event
  timestamps, merging consecutive events whose gap is at most 5 minutes
  (Corral policy constant). Using the first-to-last event span is wrong: idle
  sessions left open across days inflated the probe's result to 20.

## 5. Layout and motion (owner requirement 2026-11-04: animated, eye-catching)

The owner requires the terminal card to move: a designed, eye-catching motion
sequence, not a static printout. Motion reveals the real values; it never
fakes data or delays exit.

### 5.1 Grid

One fixed cell grid of 72 columns × 19 rows (fits an 80×24 terminal) is the
single source for the terminal frame and the exported image. Card background
is a forced warm black regardless of terminal theme, so screenshots look the
same everywhere. Palette: text `#EDE6DE`, dim `#8A817A`, dragon red
`#BA1F14` (logo, as in the splash), gold `#D9A441` (light and highlights),
runtime colors from the theme.

| Rows | Content |
|---|---|
| 0, 18 | Rounded border; the bottom border embeds `github.com/corral-dev/corral` |
| 2–4 | `corral` block logo (splash font) left; `WRAP`, date range and period right |
| 6–8 | Four stat columns: label, large value, dim detail (sessions / tool calls / tokens / peak parallel) |
| 10–11 | Agent share stacked bar and legend |
| 13–17 | Activity heatmap: 7 days × 24 hours in half-block cells (two days per row), hour axis below |

A terminal smaller than the card shows a one-line resize hint instead of a
clipped card.

### 5.2 Storyboard

Time 0 is when the data is complete. Frames target 30 fps inside the card
widget only (the full-screen snapshot limit of the dragon overlay does not
apply to a 72×19 widget).

| Phase | Time | Motion |
|---|---|---|
| Loading | real duration | Dim border; logo cells flicker as decoding noise; stat values climb live from real partial counts; a gold light travels along the bottom border as read progress |
| Border | 0.0–0.5 s | A gold head runs clockwise around the border, leaving it lit |
| Logo | 0.2–0.9 s | Each logo cell resolves from `░▒▓` noise to its final glyph at a random moment, flashing white once |
| Numbers | 0.6–1.8 s | Values count up with ease-out, 0.12 s stagger; each lands with a 0.15 s gold flash |
| Agents | 1.2–2.0 s | Bar grows left to right with a bright leading edge; legend items fade in one by one |
| Heatmap | 1.6–2.8 s | Cells pop in a diagonal wave (hour + 2 × day), one bright frame then settle |
| Peak | 2.6–3.2 s | The peak-hour cell blinks gold; the peak detail types in |
| Beams | 2.9–3.6 s | Two gold light beams sweep across card rows and light the characters they pass (finale inspired by TTE Beams) |
| Idle | after | A diagonal shimmer crosses the logo every 4 s; the timer sleeps between shimmers |

Keys: `s` save image, `r` replay, `q` / `Esc` quit. Reduced motion
(`TEXTUAL_ANIMATIONS=none`), `--no-anim` and non-TTY output render the final
frame directly.

### 5.3 Motion engine selection (researched 2026-11-04)

Corral draws the storyboard with its own frame function over the cell grid,
borrowing effect vocabulary from mature terminal effect libraries instead of
embedding one:

- [TerminalTextEffects](https://github.com/ChrisBuilds/terminaltexteffects)
  (Python, MIT, zero dependencies, about 40 effects) was evaluated on the real
  colored card in an isolated environment. It preserves foreground and
  background colors, but costs about 20 ms per frame on a 72×19 card, runs long
  (Beams 367 frames, Decrypt 1,615 frames), emits cursor-addressed ANSI strings
  that would need re-parsing into Textual segments, and cannot express
  data-driven motion (count-up, bar growth, heatmap wave). Corral's frame
  function measured 1.1 ms per frame.
- [tachyonfx](https://github.com/ratatui/tachyonfx) (Rust/Ratatui) supplies
  the vocabulary: coalesce (logo decode), sweep-in (agent bar), diagonal
  pattern (heatmap wave), fade (legend). TTE Beams inspires the finale.
- Other surveyed engines ([sysc-Go](https://github.com/Nomadcxx/sysc-Go),
  [tui-vfx](https://lib.rs/crates/tui-vfx)) are Go/Rust and not embeddable in
  Corral's Python TUI.

### 5.4 Image export

`s` or `--png PATH` writes the final frame at 1200×630: the 72×19 grid
rendered at 16×32 pixels per cell, centered. No new dependency: an embedded
glyph subset of the BSD-2-Clause [Spleen](https://github.com/fcambus/spleen)
16×32 bitmap font (license retained beside the data) and a standard-library
`zlib` PNG encoder. Rejected alternatives: Pillow (Homebrew's Pillow pulls
ten native libraries for every Corral user); Textual/Rich SVG screenshot plus a
rasterizer (CairoSVG needs native Cairo, a headless browser is far heavier, and
social platforms do not accept SVG). Spleen was chosen over other permissive
bitmap fonts because it ships a native 16×32 size, which fits the 72×19 grid
into 1200×630 without scaling. Default path is
`corral-wrap-YYYYMMDD.png` in the current directory.

## 6. Card content constraints

- Local only: nothing is uploaded, no network call.
- Built for sharing: no prompts, message text, session titles, file paths or
  project names on the default card.
- Footer: one line linking to the public Corral repository.
- Terminal rendering follows the terminal UI knowledge base and the global UI
  standards; visual acceptance needs an inspected fresh capture.

## 7. Product decisions (owner, 2026-11-04)

1. **Tokens yes, money no.** The card shows native token totals with their
   coverage. It shows no monetary amount, recorded or estimated.
2. **Default period: last 7 days.** `--month` (30 days) and `--all` (all
   retained history) are explicit options. Claude keeps about 30 days of
   transcripts by default, so longer periods disclose each runtime's coverage.
3. **Output: terminal card plus image export.** The terminal card is the
   default; an export option writes a 1200×630 social image built from the
   same aggregated values, never a separately computed set.

## 8. Market context (searched 2026-11-04)

Many Claude Code "wrapped" cards already exist, for example
[claude-code-wrapped](https://github.com/kschrader/claude-code-wrapped),
[agent-wrapped](https://github.com/lfgcampos/agent-wrapped),
[wrapped-mod](https://github.com/Ron-Nefyodov/wrapped-mod) (SVG export) and
[Year in Code](https://news.ycombinator.com/item?id=46401120) (built on
ccusage). [ccusage](https://github.com/ccusage/ccusage) now reports several
agents and drove screenshot sharing through
[a show-and-tell thread](https://github.com/ryoppippi/ccusage/discussions/26).
Corral's distinguishing content is cross-runtime coverage of five agents and
parallel-session work, so those metrics lead the card. The inspected
references (claude-code-wrapped, agent-wrapped; cloned 2026-11-04) are static;
Corral reuses only their label/value column and activity-heatmap layout idea,
no code, and differentiates with motion.

## 9. Implementation notes and verification (2026-11-04)

- **Worker processes.** Per-session reads run in a `ProcessPoolExecutor`
  (291 sessions: 4.5 s versus 17.6 s sequential). Textual replaces
  `sys.stderr` with a capture whose `fileno()` is -1, and a multiprocessing
  resource tracker first started inside the app fails with
  `ValueError: bad value(s) in fds_to_keep` (found in the real terminal run).
  `wrap_stats.prepare_workers()` starts the tracker before the TUI; regression
  `WorkerPoolTests`. Spawned workers re-import the entry module, so any script
  that calls `collect` needs a `__main__` guard (the installed entry points
  have one).
- **Scan.** Corral's listing with a 1,000-per-runtime limit, widened ×4 for a
  runtime whose oldest returned session is still inside the period. A cold
  derived cache makes the first scan 10–25 s; warm runtime scans take under
  1 s. The loading frame says "scanning history" until totals are known.
- **Frame timing (headless, real data).** 25.5 s load with 729 loading frames,
  maximum gap 0.12 s (the UI keeps animating while reading); 122 reveal frames,
  maximum gap 44 ms.
- **Real terminal (iTerm, locked session with wake loop).** Sequential frames
  after `r` showed the border light, logo decode with white flashes, count-up,
  agent bar sweep with leading edge, heatmap diagonal wave, peak typing and the
  finale beam, then the idle logo shimmer; `s` wrote the PNG and the hint line
  showed its absolute path; the PNG equals the final terminal frame. Identical
  consecutive captures occurred while the locked display blinked off; the
  headless timing above shows the app itself kept rendering.
- **Exported file mode** follows the user's umask (mkstemp's 0600 is widened),
  since the image is meant to be shared.
