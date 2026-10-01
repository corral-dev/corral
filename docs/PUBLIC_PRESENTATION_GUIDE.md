# Public presentation

## Product identity

Owner decision (2026-10-01, supersedes the plain cream-yellow background): use the independently refined Floral 04 identity as Corral's unified brand mark. It combines the approved forest-green and warm-yellow cowboy hat with a cream-yellow background covered by dense small scalloped flowers in pale sage and golden yellow. Preserve the accepted refinement's full composition, hat geometry, palette and flower scale; do not reuse the old solid-background optical shift. Synchronize the production master, both README languages, social preview, relay presentation and iOS app icon. The former arrow, horse and blue-background unicorn identities remain retired.

The canonical production master is `brand/AppIcon-1024.png`. The iOS catalog at `../../ios/Corral/Assets.xcassets/AppIcon.appiconset/AppIcon.png` consumes the same master. README presentation uses `docs/screenshots/corral-icon.png`, a compact rounded display derivative. Keep the application master square and opaque; do not embed the 1024 master in an SVG for GitHub. Retired arrow, horse and unicorn artwork must not remain active brand alternatives. Assistant-provider logos retain their own official identities.

All presentation derivatives come from that master. `brand/render.py` deterministically prepares the 1024px app asset, rounded README display image and social-preview card. Relay documentation uses the same rounded image. Keep the accepted floral refinement as `brand/source.png`, the original independent hat refinement as `brand/refined-hat.png`, and the current floral refinement prompt in `brand/`; do not rely on machine-local generation paths. Use the existing sanitized terminal capture in the social card without replacing the live product demonstration in the README.

Previous plain cream-yellow asset verification (2026-10-01; superseded by Floral 04): 48px and 24px contrast checks passed (6.01:1 and 4.63:1 luminance spans). The iOS master is byte-identical to the canonical CLI master; CLI and relay README display images are byte-identical. The master preserves the accepted installed `Corral BG 01` composition and pixels. Signed Release iOS 1.0.82 (92) was installed and version-readback verified on iPhone Max, and uploaded to AppShelf. The cream-yellow icon was captured and inspected on the actual iPhone Home Screen. Both rendered GitHub README languages were visually verified. Physical-device delivery and Home Screen appearance are separate acceptance layers, both completed for this revision.

The earlier white-background release was installed as iOS 1.0.79 (89). The social card file is published through the repository; the GitHub custom social-preview setting was not updated because the browser was not authenticated.

Floral 04 production asset verification (2026-10-01): the 1024px master is square and opaque. The 48px and 24px contrast checks passed with luminance spans of 8.08:1 and 7.81:1. The iOS icon is byte-identical to the canonical master; the relay README image is byte-identical to the CLI README image. Signed Release iOS 1.0.88 (98) was installed on iPhone Max with version readback and launch confirmed, and uploaded to AppShelf release 150. GitHub published README icon bytes match the accepted derivative. Physical Home Screen appearance remains unverified while the phone is locked; the in-app browser could not reload GitHub, so this revision does not claim rendered README screenshot acceptance.

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
