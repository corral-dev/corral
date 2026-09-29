# Public presentation

## Product identity

The approved Corral icon is the colorful paper-cut unicorn on a blue background. Use the current iOS master at `../../ios/Corral/Assets.xcassets/AppIcon.appiconset/AppIcon.png` in the product workspace. The green arrow and the orange/sage horse artwork are retired; remove their files and generated packages rather than retaining competing masters. Do not replace the unicorn with a newly generated interpretation.

The README display asset is `docs/screenshots/corral-unicorn.png`: a compact rounded clip of the approved master. Keep the application master square and opaque. Do not embed the 1024 master in an SVG for GitHub.

## README contract

Keep English and Simplified Chinese READMEs aligned. Lead with the visitor's need in their language — lost yesterday's chat, and one list of coding-agent sessions — then a real product demonstration and installation. The GitHub About description and topics must use the same need words (`lost` / `conversations` / `coding-agent sessions` / `session history` / `session manager` later in the sentence, never as the first hook). Explain finding sessions, keeping agents running, working side by side, and handing work to another assistant. Keep exhaustive command lists, internal architecture, implementation rules, and historical fixes in the linked guides. Do not add keyword dumps, speculative claims, unavailable download links, or repeated requests for stars. State iPhone availability and self-hosted relay requirements honestly.

Use current, sanitized product captures. The README hero demonstration is `docs/screenshots/demo.gif`, generated from the real terminal UI by `docs/screenshots/capture.py` with isolated sample conversations; keep it full-width on the first screen. A historical animated capture such as `demo-list.gif` must not return; it showed command output and local paths. Static `list.png` remains the still, also first-screen and full-width; fold `search.png`. Phone README shots are the session list and conversation, not the machine picker. Check both rendered languages, images and links after changes. Pushing files does not update GitHub About or topics — patch those with the API. Do not claim default-search rank moved until a later Best Match remeasure (index lag).

Capture with Homebrew Python plus cairo, and put both `cli/src` and SessKit `src` on `PYTHONPATH`. pipx Python often lacks the screenshot libraries; do not commit unused SVG rasterizers or embed the 1024 master in an SVG. After search, opening a split with Space then Enter is flaky in the harness — `capture.py` opens the split in code. Drop an unstable beat rather than fake UI.

Do not claim title generation launches installed assistant CLIs. The public privacy text must match the configured language-model gateway.

## Structural references

- [Sesh](https://github.com/joshmedeski/sesh): concise identity and task-oriented feature summaries; adopt these patterns, not its configuration-heavy page length.
- [Lazygit](https://github.com/jesseduffield/lazygit): show the actual terminal product early and link deeper usage; do not copy sponsor blocks or badge volume.

README quality improves the explanation offered to visitors; it does not demonstrate increased discovery or stars. Measure those separately.

The conversion page order that other flagships should copy lives in the global star-growth guide §3.5.1; this file keeps Corral-only identity, capture, and retired-asset rules.

## Review follow-up (2026-09-12)

Closed in-tree after the bilingual rewrite:

- `install.sh` curl fallback SessKit pin matches `scripts/sesskit_dep.py` (bump both together).
- `demo.gif` / `list.png`: Twemoji fruit embed + bold-safe mono font; footer shows Ctrl-only Advanced/Delete once. Re-inspect the GIF final split frame after every recapture.
- Redundant pre-Install pitch paragraph removed from both READMEs.

Still open:

- Prove `ios-sessions.png` / `ios-chat.png` against the live iPhone app (or replace them) before treating them as current evidence.

Capture pitfalls: do not rely on Color Emoji font swaps under Cairo; do not empty `group_emoji` to hide tofu; missing `docs/screenshots/emoji/*.png` must fail the capture.

<!-- 该文档整理/压缩于 2026-09-29 -->
