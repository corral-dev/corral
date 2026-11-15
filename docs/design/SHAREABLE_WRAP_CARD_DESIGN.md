# Shareable activity card (`corral wrap` → `corral flex` / agentflex)

Status: superseded by agentflex (§10). The first Python `corral wrap` was removed from Corral on 2026-11-04; `corral flex` launches the standalone [agentflex](https://github.com/corral-dev/agentflex) binary (installed, else `npx -y agentflex@latest`). §3–§5 and §8–§9 record the retired prototype's measurements and are historical; §1–§2, §6–§7 and §10 hold the decisions that still bind.

## 1. Decision and why

A shareable usage card ships as a Corral entry point — not a new TUI product, not a SessKit feature:

- Corral is the product people keep using; a shared card should lead new users into it rather than into a single-purpose tool.
- SessKit keeps its boundary: it interprets native history and exposes normalized data; presentation belongs to downstream products.
- The growth rationale and channel plan stay in the private growth guide (`~/.config/agentsync/docs/GITHUB_STAR_GROWTH_GUIDE.md` §3.9, item 4).

## 2. Ownership

| Layer | Owns |
|---|---|
| SessKit | Session scan, typed activity events (`load_activity`), usage snapshots (`load_usage`), real event timestamps, native error evidence |
| Corral | Period selection, active-interval policy, aggregation, card layout, colors, branding footer, optional export |

The aggregation reads SessKit's normalized output only and adds no parser; a missing fact is added upstream first (SessKit contract and live checks), then consumed from the published version.

## 3. Data feasibility (owner's machine, 2026-11-04, retired prototype)

Read-only 7-day probe through SessKit's public API: scan 4,325 sessions in 16 s with 379 active (OpenCode 222, Pi 62, Claude 47, Codex 46); `load_activity` 377 available / 2 unsupported in 3.6 s (38,764 tool calls, 8,271 assistant messages, 1,741 user messages); `load_usage` 364 available / 13 empty / 2 unsupported in 10.5 s. A cold run is ~30 s, so the card reused Corral's scan and derived caches, showed progress while reading, and never presented a partial read as a complete total.

## 4. Metric definitions (still the agreed semantics)

All values come from real parsed output; unsupported or unreadable sessions are counted and disclosed, never silently treated as zero.

- **Sessions:** sessions with at least one activity event inside the period, from Corral's listing — which drops sessions whose working directory no longer exists (99 such sessions on 2026-11-04, test-harness temporary directories, excluded on purpose).
- **Agent share:** percentage of those sessions per runtime.
- **Tool calls:** `tool_call` events inside the period.
- **Tokens:** SessKit usage records carry no timestamp, so in-period tokens use `summarize_usage` with a `UsageSelection` of the message ids whose activity events fall inside the period; a session whose whole history lies inside the period contributes its full summary. Unattributable records are disclosed as uncovered, never guessed. No monetary amount (owner decision, §7).
- **Peak parallel sessions:** the maximum number of sessions active at the same moment, built from event timestamps while merging consecutive events whose gap is at most 5 minutes (Corral policy constant). Using the first-to-last span is wrong: idle sessions left open across days inflated the probe to 20.

## 5. Layout and motion (retired prototype; owner requirement 2026-11-04: animated, eye-catching)

Motion reveals real values; it never fakes data or delays exit. One fixed 72-column grid (19 rows, 21 since §10.7; fits an 80×24 terminal) fed both the terminal frame and the export: forced warm-black card background regardless of terminal theme, text `#EDE6DE`, dim `#8A817A`, dragon red `#BA1F14` (logo), gold `#D9A441`, runtime colors from the theme. While the card played, the whole alternate screen was painted with the export page color `#0C0A09` (owner feedback 2026-11-08: a light terminal theme framed the dark card in white); the normal screen was restored unchanged on exit.

Storyboard phases: loading (dim border, logo decode noise, values climbing from real partial counts, gold light travelling as read progress), border sweep 0.0–0.5 s, logo cell resolution 0.2–0.9 s, count-up 0.6–1.8 s with 0.12 s stagger and 0.15 s gold flash, agent bar 1.2–2.0 s, heatmap diagonal wave 1.6–2.8 s, peak blink/typing 2.6–3.2 s, finale beams 2.9–3.6 s, idle shimmer every 4 s. Keys `s` save, `r` replay, `q`/`Esc` quit; `TEXTUAL_ANIMATIONS=none`, `--no-anim` and non-TTY render the final frame.

Engine choice: Corral drew the storyboard itself over the cell grid (1.1 ms per frame) borrowing vocabulary from mature effect libraries rather than embedding one — [TerminalTextEffects](https://github.com/ChrisBuilds/terminaltexteffects) cost ~20 ms per frame on a 72×19 card, ran long (Beams 367 frames, Decrypt 1,615) and emitted cursor-addressed ANSI needing re-parsing; [tachyonfx](https://github.com/ratatui/tachyonfx) supplied the vocabulary (coalesce, sweep-in, diagonal pattern, fade) and TTE Beams the finale; the Go/Rust engines surveyed were not embeddable.

Image export: `s` or `--png PATH` wrote the final frame at 1200×630 from the same grid, using an embedded BSD-2-Clause [Spleen](https://github.com/fcambus/spleen) 16×32 bitmap subset and a standard-library `zlib` PNG encoder (no new dependency). Pillow (ten native libraries per user) and an SVG-plus-rasterizer path (needs Cairo or a headless browser; social platforms reject SVG) were rejected; Spleen ships a native 16×32 size that fits the grid without scaling.

## 6. Card content constraints

- Local only: nothing uploaded, no network call.
- Built for sharing: no prompts, message text, session titles, file paths or project names on the default card.
- Footer: one line linking to the public Corral repository.
- Terminal rendering follows the terminal UI knowledge base and the global UI standards; visual acceptance needs an inspected fresh capture.

## 7. Product decisions (owner, 2026-11-04)

1. **Tokens yes, money no** — native token totals with their coverage; no recorded or estimated monetary amount.
2. **Default period: last 7 days**; `--month` (30 days) and `--all` (all retained history) are explicit. Claude keeps about 30 days of transcripts, so longer periods disclose each runtime's coverage.
3. **Output: terminal card plus image export** — the terminal card is the default, and an export option writes a 1200×630 social image from the same aggregated values, never a separately computed set.

## 8. Market context (searched 2026-11-04)

Claude Code "wrapped" cards already exist ([claude-code-wrapped](https://github.com/kschrader/claude-code-wrapped), [agent-wrapped](https://github.com/lfgcampos/agent-wrapped), [wrapped-mod](https://github.com/Ron-Nefyodov/wrapped-mod), [Year in Code](https://news.ycombinator.com/item?id=46401120) built on ccusage), and [ccusage](https://github.com/ccusage/ccusage) drove screenshot sharing through [a show-and-tell thread](https://github.com/ryoppippi/ccusage/discussions/26). Corral's differentiation is cross-runtime coverage of five agents and parallel-session work, so those metrics lead the card. The two inspected references (cloned 2026-11-04) are static; Corral reused only their label/value column and heatmap layout idea, no code, and differentiated with motion.

## 9. Implementation notes and verification (2026-11-04, retired prototype)

- **Worker processes:** per-session reads in a `ProcessPoolExecutor` (291 sessions: 4.5 s versus 17.6 s sequential). Textual replaces `sys.stderr` with a capture whose `fileno()` is -1, and a multiprocessing resource tracker first started inside the app fails with `ValueError: bad value(s) in fds_to_keep`; `wrap_stats.prepare_workers()` starts the tracker before the TUI (`WorkerPoolTests`). Spawned workers re-import the entry module, so scripts calling `collect` need a `__main__` guard.
- **Scan:** Corral's listing at a 1,000-per-runtime limit, widened ×4 for a runtime whose oldest returned session is still inside the period. Cold derived cache makes the first scan 10–25 s; warm runtime scans take under 1 s.
- **Frame timing (headless, real data):** 25.5 s load with 729 loading frames, maximum gap 0.12 s; 122 reveal frames, maximum gap 44 ms.
- **Real terminal (iTerm, locked session with wake loop):** sequential frames after `r` showed every phase, `s` wrote the PNG with its absolute path in the hint line, and the PNG equalled the final terminal frame; identical consecutive captures occurred while the locked display blinked off, and the headless timing shows the app kept rendering.
- **Exported file mode** follows the user's umask (mkstemp's 0600 is widened), since the image is meant to be shared.

## 10. Owner corrections, round 2 (2026-11-04)

These supersede §1 (distribution), §3 (performance), §4–§5 (card content) and §7 item 3 where they conflict.

### 10.1 Zero-friction distribution and speed

- **Run without installing Corral** with a single command such as `npx <name>`; `corral <subcommand>` keeps working for Corral users.
- **Statistics complete within 1 second** for the default 7-day period (the first version took 5–25 s).
- **Install-time background precomputation was considered and rejected:** `npx` has no install phase, pnpm 10 and Bun block dependency lifecycle scripts by default, npm users often pass `--ignore-scripts`, and a package that reads AI conversation history from an install hook looks like a supply-chain attack. Use an incremental per-file summary cache instead (only bytes or rows added since the last run are parsed); Corral's background scan worker may keep it warm for Corral users, and the cold first run must still meet 1 s.

Measured 2026-11-04 over the 7-day window: 160 recent JSONL files, 670 MB (Claude 256 MB, Codex 59 MB, Pi 318 MB) plus OpenCode rows in a 1.5 GB SQLite database. The first version spent scan 7.2 s, activity 3.5 s, usage 10.4 s single-threaded, parsing every file twice. A native SIMD multi-threaded literal scan (ripgrep) covered the same 670 MB in 0.04–0.06 s warm and a time-filtered SQLite query takes milliseconds, so a native multi-core engine with window pruning (JSONL is append-ordered: read backwards until timestamps leave the window) fits inside 1 s. The competitor ccusage ships a native binary through npm platform packages (`optionalDependencies` per OS/arch, checked with `npm view`), the same pattern as Biome.

### 10.2 Card content

Remove the large `CORRAL` logo (it reads as an advertisement; the card states its own purpose and the GitHub address stays as the only low-key promotion in the bottom-right border), remove the coverage disclosure from the card (it stays in `--json`), replace TOOL CALLS with average tokens per day, and remove the absolute date range and the `Mon-Sun` heatmap caption.

### 10.3 Name

The owner questioned "wrap" (it reads as "to wrap", not a Spotify Wrapped-style recap) and asked for a cooler word. **Decision: `flex`** — standalone command `npx agentflex` (npm name free on 2026-11-04), Corral subcommand `corral flex`; `corral wrap` is retired. Hero: the large block-font element is the token total with a small "tokens" label; the title reads `CODING WEEK` with the period label on the right.

### 10.4 Engine placement (decision)

A 1 s implementation runnable through `npx` needs a native engine (npx cannot run Python), and SessKit's hard rule forbids a second parser in consumers. **Decision: a multi-core Rust engine inside SessKit**, verified against the Python output on real histories, consumed by a standalone Rust card program (terminal animation, PNG export) distributed through npm platform packages like ccusage. The card program is downstream presentation and does not live in SessKit.

### 10.5 Platforms (owner requirement 2026-11-04)

`npx agentflex` supports macOS, Linux and Windows (x64 and arm64 per npm platform package). History discovery uses each runtime's actual Windows location, never a POSIX path joined by hand. Windows acceptance runs on the Windows 11 test VM (infra guide: `win` / `win-test`, QGA fallback while its SSH is down) with real assistant histories created there, plus a `windows-latest` CI job in the standalone repository. This does not change Corral's own decision not to support Windows ([Windows compatibility](WINDOWS_COMPATIBILITY_DESIGN.md)); `corral flex` remains a macOS/Linux entry point running the same engine.

### 10.6 Implementation status

- Engine `~/Codes/SessKit/crates/sesskit-stats` (Rust, rayon, window pruning): owner's 7-day window in 0.36–0.52 s versus 15–25 s for the Python version; parity with the Python reference was in progress.
- Card program `~/Codes/AgentFlex` (private Forgejo `Max/agentflex` until the public repository existed): Rust, crossterm frame diffing, the same 72×19 grid for terminal and PNG, embedded Spleen subset and `flate2` PNG encoding. Content follows §10.2 and the hero decision, with a square-pixel 6×7 bitmap font for the token total; verified in a real iTerm window by sequential replay captures.
- Distribution scaffolding: npm main package with a Node launcher plus six platform packages, release/CI workflows for macOS, Linux (static musl) and Windows (x64, arm64). Publishing needs the owner's npm login and a public GitHub repository.
- `corral flex` (2026-11-04, `src/corral/flex_cmd.py`) execs the installed `agentflex` binary when on PATH, else `npx --yes agentflex@latest`, passing arguments through; with neither it prints one install hint and exits 1. The Python `corral wrap` modules, tests, font asset and README section were removed in the same change. A Homebrew `agentflex` formula for Corral's tap remains a follow-up.
- Public repository https://github.com/corral-dev/agentflex (MIT) with English and Chinese READMEs and a demo from fictional data.
- Released 0.2.0 on 2026-11-08 (model leaderboard, dark terminal stage); release procedure in AgentFlex `AGENTS.md`. Open release follow-ups: publication runs only through GitHub Actions (no local publish path independent of CI, as the open-source guide requires), and the workflows' actions target Node 20, which GitHub now forces onto Node 24 with a deprecation warning.

### 10.7 Model leaderboard (owner decision 2026-11-08)

- **Both rows stay:** an agent share bar (sessions per agent) and a model share bar below it.
- **Ranked by tokens:** a model's share is its part of the hero token total, attributed per usage record by the native record's model and time, so model shares add up to the hero number. Cache reads dominate the total and some gateways report no cache, so long-context models rank higher; the owner accepted this for consistency with the hero.
- **No reasoning effort:** Claude and Codex record effort per turn, but Pi records only changes and OpenCode records none unless a variant is picked, so effort is omitted rather than shown as mostly unknown.
- **Display:** top three models plus one grey "other" segment, warm palette following the hero gradient (agent colors stay on the agent bar). Names are shortened for display only — provider prefix (`x/`), bracket suffix (`[1m]`), date suffix and a leading `claude-` dropped, a trailing `-N-M` version read as `-N.M`, identical display names merged, records without a model counted as `unknown`, longest names truncated with `…` until the legend fits.
- **Grid:** the card grows from 19 to 21 rows (still fits 80×24 with the key hint): agents rows 10–11, models rows 12–13, heatmap rows 15–18, axis 19. The PNG keeps 1200×630 by drawing 16×28 cells, dropping the top four blank rows of the 32-row Spleen cell (the few glyphs inked there shift down to fit).
- **Engine:** per-model window tokens come from `sesskit-stats` (`SessionStats.window_models`), never parsed here.

### 10.8 Simplified Chinese TUI (owner request 2026-11-08)

- **Locale:** the card follows the process locale — `LC_ALL`, `LC_MESSAGES`, `LANG`, `LANGUAGE`, then the OS preferred language (`sys-locale`). `zh` shows the Chinese card, every other language keeps English; `LANG=zh_CN.UTF-8` forces Chinese for acceptance and on English-primary Macs, `LANG=en_US.UTF-8` forces English.
- **Vocabulary:** the card's own words stay English (`CODING WEEK` / `CODING MONTH` / `CODING SO FAR`, `tokens`, `AGENTS`); everything else is translated the way Chinese developer tools write it: 会话数, 日均 tokens, 峰值并发, 模型, 活跃时段, 最忙, 周一–周日, 其他, 未知. Loading (正在扫描历史记录 / 正在读取 N / M 个会话), the empty state, key hints (保存图片 / 重播 / 退出), the save note (已保存到 …), save errors and the small-terminal hint follow the same language.
- **One grid:** terminal and PNG remain the same card in the same language, so a Chinese run exports a Chinese PNG; double-width characters occupy a lead cell plus a `CONT` cell every renderer skips.
- **PNG glyphs:** Spleen has no CJK, so a subset of the OFL-1.1 Fusion Pixel 12px Mono zh_Hans (2× scaled to 24×24 inside the 32×28 double cell) is embedded as `assets/fusion-pixel-12px-subset.json`, generated by `scripts/build-cjk-subset.py` with its license beside the data.
- **Verification (2026-11-08):** real iTerm runs in Chinese and English (card, loading, key hints, save note), the small-terminal hint, both PNGs, the empty period, `cargo test` and clippy. On the Windows 11 zh-CN test VM (both `caozc` and `agenttest`) an unforced run detects zh-CN and renders the Chinese card, `LANG=en_US.UTF-8` forces English, and both PNGs were inspected on the Mac.

<!-- 该文档整理/压缩于 2026-11-15 -->
