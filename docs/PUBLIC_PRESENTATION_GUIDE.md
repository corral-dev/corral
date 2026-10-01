# Public presentation

## Product identity

Owner decision (2026-10-01): Corral's unified brand mark is the selected yellow-green cowboy hat, identified by the installed `Corral Contrast 04` comparison app. Preserve its silhouette, internal color boundaries, proportions, flat graphic style, white background, centered placement, and approximately 10% horizontal margins. The selected palette is green `#256B4C` and warm yellow `#F3BF73`; do not infer another preferred palette or redraw the hat into a different shape. Produce one independent refined master with clean curves and crisp edges, then replace every active Corral brand surface, including the English and Chinese README and iOS app icon. This explicitly supersedes the former blue-background unicorn identity and the unfinished redesign brief.

The canonical production master is `brand/AppIcon-1024.png`. The iOS catalog at `../../ios/Corral/Assets.xcassets/AppIcon.appiconset/AppIcon.png` consumes the same master. README presentation uses `docs/screenshots/corral-icon.png`, a compact rounded display derivative. Keep the application master square and opaque; do not embed the 1024 master in an SVG for GitHub. Retired arrow, horse and unicorn artwork must not remain active brand alternatives. Assistant-provider logos retain their own official identities.

All presentation derivatives come from that master. `brand/render.py` deterministically prepares the 1024px app asset, rounded README display image and social-preview card. Relay documentation uses the same rounded image. Keep the source artwork and its refinement prompt in `brand/`; do not rely on machine-local generation paths. Use the existing sanitized terminal capture in the social card without replacing the live product demonstration in the README.

Brand verification (2026-10-01): the independent master passed 48px and 24px contrast checks (6.48:1 and 5.25:1 luminance spans). The iOS app master is byte-identical to `brand/AppIcon-1024.png`; CLI and relay README display images are byte-identical. The full signed iOS Release build and 233 portable regressions passed. iOS 1.0.79 (89) was installed and version-readback verified on iPhone Max, and the signed artifact was uploaded to AppShelf. Both GitHub README variants were visually verified in the browser. The captured phone screen did not show Corral, so Home Screen icon appearance remains unverified. The social card file is published, but the GitHub custom social-preview setting remains unchanged because the browser is not authenticated.

## README contract

Keep English and Simplified Chinese READMEs aligned. Positioning (owner decision, 2026-09-30): Corral is a workspace for running many coding agents from one terminal, which you can pick up from your phone — not a history-search or "resume yesterday's chat" tool. The hook is "Run all your coding agents from one terminal — and pick them up from your phone"; history search is a supporting capability and must not lead the tagline, About description, or feature list. Follow the hook with a real product demonstration and installation. The GitHub About description leads with the same positioning; topics may keep search terms such as `session-manager` / `session-history`. Feature order: see who needs you, work side by side, keep agents running, hand work to another assistant, continue from the phone, then find past conversations. The phone promise must sit next to an honest availability note (no public iPhone download; away-from-LAN use needs a self-hosted relay).

Product parts (owner decision, 2026-09-30, an explicit exception to "do not expose unfinished features"): the README lists Corral's three parts in one short table between the still image and Install, each with its real status — the terminal app (available), the iPhone and Mac app (iPhone in development with no public download; Mac planned), and Corral Ideas (planned; design in `docs/design/WEB_TASK_BUTLER_DESIGN.md`). Unfinished parts get a one-line description and a status only: no install commands, download links, screenshots, or commands such as `corral ideas` until they ship. Update the status cell in both languages when a part ships. Keep exhaustive command lists, internal architecture, implementation rules, and historical fixes in the linked guides. Do not add keyword dumps, speculative claims, unavailable download links, or repeated requests for stars. State iPhone availability and self-hosted relay requirements honestly.

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
