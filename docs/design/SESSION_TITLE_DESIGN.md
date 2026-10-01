# Shared session titles

## Confirmed product requirement

iOS and TUI must use the same session-title generation logic, generation channel, and resulting title for the same host and session. iOS must not independently generate a competing title.

**Title quality requirement (2026-09-12):** a title must identify the session's task without requiring the reader to open the conversation. Standalone insults, `Task`, `实现`, and a closing `Session recap` command label do not satisfy this requirement for a substantive work session.

**Review gaps closed (2026-09-12):** SessKit extracts Corral handoff digest **before** the 300-character list clip (`clip_user_excerpt`); local titles skip digest headings and `User:` / `Assistant:` role labels; short real requests are no longer dropped as "emotion clauses"; insufficient retries fingerprint bounded task input (not file size); pure insults are rejected on fallback, cached-success, and model-result paths; parsed-but-invalid model output records `failure_reason=invalid`, not `transport`. Confirmed-valid cached successes still stay stable. The investigation below remains the evidence record. Requires SessKit ≥ 0.1.3.

## Independent implementation review (2026-09-12)

Scope of the original review: title changes included in `b9562eb`, reviewed at `a1562d8`, installed command 0.24.194. Findings below were reproducible then. **Remediation landed in SessKit 0.1.3 + Corral 0.24.197.** Do not restore assistant CLI generation or read every transcript on list first-paint. This Mac still has no Corral gateway key; local fallback is what the list shows until a key exists. After changing list excerpt extraction, bump Corral `_PARSER_VERSION` or the local performance cache keeps the old 300-character wrapper.

1. **P1 — Handoff context extraction ran after truncation. Fixed.** SessKit `clip_user_excerpt` / `split_handoff_text` extract `Task:` plus digest **before** the 300-character list window, then peel nested pickups whose earlier wrapper was flattened into `[Original request]`. Corral `_prompt_item` and `_compact_title` consume that payload (digest heading and `User:` / `Assistant:` labels stripped). Raising the slice to 400 cannot recover bytes already lost; tests cover `Handoff.render_prompt()`, `make_session_info` after clip, and nested flattened wrappers. Verify native history → scanner → actual title request after install. New handoff digests peel before flattening so the next pickup does not nest leftover intro text.
2. **P2 — Eight-character clause heuristic deleted real tasks. Fixed.** Leading clauses are skipped only when empty after emotion/`Task:` unwrap, or low-value/secondary. `_compact_title('修复登录失败，不要改动其他功能')` keeps `修复登录失败`; `_compact_title('修复闪退，保留现有界面布局')` keeps `修复闪退`.
3. **P2 — Insufficient retries tracked file size. Fixed.** Fingerprint is `v{TITLE_CACHE_VERSION}:in:{sha256[:16]}` of bounded `inherited_task` / `user_request` / `later_user` / `native_title`. Tool-log `size_bytes` / assistant growth does not requeue; equal-size request edits do. Cache version stays 4.
4. **P2 — Pure insults passed as successes. Fixed.** Empty-after-emotion-prefix is not restored; `_is_low_value_title` rejects compact insult forms. Fallback, cached success, and mocked model `你他妈的` all lose to a real request when one exists.
5. **P2 — Invalid diagnostics collapsed to transport. Fixed.** `generate_titles_batch` returns `_BatchRaw` with `kind` in `{ok, invalid, transport}`. Parsed-but-unusable titles (`Task`) and malformed JSON persist as `failure_reason=invalid`.

Validation of the original gaps used 17 title tests plus isolated store tests. Follow-up tests cover clip-through-`make_session_info`, the two short-clause strings, size-only vs request-change fingerprints, insult rejection on three paths, and invalid vs transport. No real model-generation quality claim: the installed environment still has no configured Corral gateway key.

## Status and scope

**Host implementation delivered (2026-09-11):** shared `TitleState` + `poll_title_updates`, remote title-only list/metadata events with additive `revision`, remote path injects the same title daemon launcher as the TUI, and generation transport is the shared OpenAI-compatible LLM gateway (`titlegen.py`). Existing successful cache entries are preserved (no mass regeneration).

**Title-quality slice delivered (2026-09-12):**

1. Temporary titles prefer the scanner fallback (already the first real request after scanner scoring), then the raw first user message, then native title, over later confirmations, wrap-up commands, and insults. Shortest-candidate ranking is gone. Emotional prefixes are stripped when later text still names the task. Corral handoff wrappers (`Task: …` plus the pickup boilerplate) are not titles.
2. Generation input is bounded task context (`user_request`, optional `later_user`, `assistant`, `native_title` as auxiliary, `inherited_task` as untrusted). The prompt must keep `PROMPT_MARKER`, must not call native titles the user's intent, and must allow `__INSUFFICIENT__` instead of inventing a title.
3. `__INSUFFICIENT__` is stored as `generation_state=insufficient` and retries only when the content fingerprint changes. Missing gateway configuration is `failure_reason=missing_config` and retries as soon as a virtual key is present; transport failures keep the six-hour cooldown.
4. Do not restore assistant CLI generation. Do not read every transcript database on the list's first-paint path.

Still out of this host slice: iOS identity-store merge / stale-revision rejection on the phone (additive `revision` is already on the wire), end-to-end iPhone vs TUI screen acceptance, and provisioning a Corral virtual key on a machine that has none. Parent release verifies and ships. Without a key, the improved local fallback is what the list shows during cooldown.

**Do not treat a sidebar row like `cli 生成 s0` as proof that title generation still launches Claude, and do not say the empty card generated that title.** The 2026-09-12 Mac case was two independent leftovers sharing `claude:s0`: (1) a test-hosted empty pane whose cwd was Corral’s own CLI tree `~/Codes/Corral/cli` (the list shows only the last path segment `cli`; no history, so no preview); (2) a successful cache entry whose text is the unit-test template `生成` + id. Cache lookup is runtime+id and does not require messages. Generation transport is the gateway; the retired “use whichever assistant recently succeeded” CLI rotation must not come back. Isolation of old CLI noise remains `PROMPT_MARKER` filtering, not a private history room. `CORRAL_ISOLATE_MANAGED_HOSTS` protects tests from the live TUI, not the live TUI from tests.

**【Ruling · 2026-09-12】** Code vs three complaints was audited against the live Mac (no product change). Keep the gateway; do not restore CLI rotation; do not add a private history room for the current channel. This Mac still has no virtual key, so the list shows local fallbacks plus cache — not new model titles. Public `PRIVACY.md` already matches the gateway. Optional ops only: provision a key, or scrub test keys `claude:s0`…`s29` from the real cache without deleting real session titles.

This scope covers Corral titles on one owning host, not independently generated native assistant UI titles or deduplication of copied histories across hosts.

**Outage and repair (2026-09-23):** the Mac showed first-sentence fallbacks almost everywhere. Root cause was not the fallback ranking but a starved pipeline: no gateway virtual key on this Mac (512 `missing_config` failures), so no model title had ever been produced here. Repaired without a pipeline refactor: created LiteLLM team `corral` with key `corral-mac2` (`$20/30d`, same budget as the canary teams), wrote `~/.config/corral/llm-gateway.json` (mode 600) with a temporary `model: openrouter-chat` override, and scrubbed the 30 `claude:sN` test-pollution entries from the real cache (backup in `/tmp`). Three gateway-transport bugs surfaced once calls flowed and were fixed in source with regression tests: (1) `_assistant_text` only accepted string `content`, list-content answers became `transport` failures; (2) `max_tokens` 256→2048, routed reasoning models otherwise burn the budget and return `finish=length` with null content (~$0.0006/call); (3) `_BATCH_SIZE` 5→1 and `_MAX_PARALLEL_BATCHES` 5→2 (large batches overflow the thinking budget; 5-way parallelism hangs, 2-way completes in ~3s), plus per-chunk best-effort reuse of the least-recently-failed candidate so one unlucky probe no longer poisons the whole run (the pinned single-probe-then-stand-down behavior for a deterministically dead preferred generator is kept). `TITLE_CACHE_VERSION` 4→7 requeued the wrongful failure marks; accepted successes were never regenerated. Open infra items (gateway-side, not Corral): DeepSeek upstream key rejected (`****044c` invalid, kills `budget-chat`/`default-chat`) and Gemini denied at project level (kills `gemini-chat`); only `openrouter-chat` is healthy, hence the local override until the upstreams are rotated. Verify by watching `titles.json` success count grow and TUI rows flip from first-sentence fallbacks to model titles live via `TitleState.poll`.

## Findings and evidence

- iOS `SessionPresentation.displayTitle` displays the received `SessionSummary.title`, falling back to the key when empty. Both list rows and detail headers use this presentation path. The inspected path does not generate an AI title.
- Host TUI and remote now share title file polling through `SessionStore.title_state` / `poll_title_updates`. Title-only cache writes advance a monotonic `title_revision` and notify remote subscribers without waiting for history mtime.
- Remote `SessionHub._refresh_loop` emits `sessions` (full `list_snapshot`, including `revision`) when title keys change, and `session:{key}` metadata events for open detail watches.
- Production remote construction (`RemoteDaemon` / default `RemoteService` hub) injects `default_title_spawn_fn` → `corral._spawn_title_daemon`. Bare `SessionHub()` leaves spawn unset so tests stay side-effect free.
- Generation uses gateway aliases from live `/v1/models`, preferring `budget-chat` when present. Config: `~/.config/corral/llm-gateway.json` with `base_url`, `api_key`, optional `model`. Env overrides: `CORRAL_LLM_GATEWAY_URL`, `CORRAL_LLM_GATEWAY_KEY`, `CORRAL_TITLE_MODEL`. Default base URL: `http://the suzhou node (address: see AgentSync INFRA_KNOWLEDGE_BASE):18081/v1`. **Maintainer note:** this Mac had no provisioned Corral virtual key in env/keychain/`~/.config/corral/`; generation stays unavailable until a virtual key is written to that config (or `CORRAL_LLM_GATEWAY_KEY`). Title sync/propagation of existing cache entries does not require a key.

## Local title-quality investigation (2026-09-12)

**Status: diagnosis recorded 2026-09-12; title-quality slice implemented the same day.** The installed Mac command at investigation time reported 0.24.191 and loaded this checkout through its pipx interpreter. A read-only `corral list --limit 300 --compact` returned 730 sessions: Cursor 277, Codex 236, OpenCode 189, Pi 20, Claude 8; no Kimi rows were returned. Inspected six deterministic samples per represented runtime plus targeted anomalies, comparing displayed titles, cache state, scanner excerpts, and selected native history metadata. Counts describe this bounded scan, not all history or a measured bad-title rate. Historical cache entries do not retain generator/model/request provenance.

### Confirmed failure paths

| Symptom | Evidence and mechanism |
|---|---|
| An insult replaces a meaningful task title | A Cursor sample had a meaningful native title and an initial question about assembling annotation requests. Its final user prompt started with an insult followed by the actual request. `_compact_title` split at punctuation and kept the insult; `_temporary_title` chose it because it was shorter than every meaningful candidate. An independent Codex sample and OpenCode samples show the same shortest-candidate problem. |
| `Session recap` replaces the original task | Four Cursor rows displayed this title. Their final prompt was `/doc-update`; `_normalize_title` translated that command through `session.title.cmd.doc_update`, and the resulting label won the shortest-candidate comparison. This text is Corral's command label, not evidence of a model hallucinating a summary. English labels can appear on Chinese conversations via this fallback path. |
| `Task` after switching assistants | Two Codex samples and one Claude sample began with Corral's handoff text `Task: ...`. `_compact_title` split at the colon, retained `Task` (four characters), and accepted it as meaningful. One chain starts at the Cursor `实现` sample and carries that weak title into the Claude handoff. This is a shared handoff/presentation defect, not proof that either native parser misidentified the runtime. |
| `实现`, `执行`, and similar context-free replies | The Cursor `实现` sample had a native title describing a CI problem, but its first prompt was only `实现`; shorter text displaced the useful native title. Two OpenCode `执行` samples had clear initial tasks about application code and restoring an installed utility, yet the final confirmation won. Excluding only a few known small-talk strings is insufficient. |
| Old `空白会话` / `新建会话` remains on a substantive conversation | Two Codex records were accepted as successful cached titles even though current excerpts clearly described icon variants and fixing application exit. The validator rejects some synonymous empty-session phrases but accepts these. Accepted successes ignore later content growth. The original generation input, model, and failure timeline are not retained, so the exact historical origin is unproven. A separate `回复正常` sample really was a response-check task; short titles alone are not sufficient evidence of corruption. |
| No automatic recovery from weak fallback titles | The actual installed interpreter reported no configured gateway key, no default config file, and zero available generators. In the 730-row scan, 266 rows had failed cache records, 432 had non-failed records, and 32 had no matching record. The complete cache snapshot contained 857 entries, including 287 failures; it also contains entries outside the scan and test-looking identities, so do not use its total as the number of real sessions. Missing configuration enters `_persist_failed_sessions` without a model request. Failed records impose a six-hour cooldown; retrying cannot help while configuration remains absent. This verifies the current invoking environment, not the unseen environment of every already-running process. |

Failed-cache titles are not necessarily what the user sees: `resolve_initial_title` recomputes the local fallback during cooldown. In the insult sample, the persisted failed title was still meaningful, while the displayed title had already degraded to the latest insult. Inspecting `titles.json` alone would miss this case.

### Runtime-specific input limitations

- Cursor supplies native metadata title as its first fallback candidate, with first/last prompts from `prompt_history.json` (newest first). It leaves the assistant excerpt empty in the list path. Before 2026-09-12 the batch prompt called `preferred_title` the "best user intent"; current prompts label `native_title` as auxiliary metadata. The `实现` sample has little task information in its three plain prompt-history entries, although its native title supplies useful context.
- Codex and Claude can receive Corral handoff wrappers as their first apparent user request. The wrapper must not become the task; a short excerpt can be consumed by boilerplate before reaching inherited context.
- OpenCode provides useful initial task and assistant excerpts in the inspected examples. Their loss occurred in shared fallback selection, so a blanket OpenCode parser rewrite is not supported by this evidence.
- All 20 returned Pi rows had non-failed cache entries; six sampled titles were interpretable. This sample neither establishes universal Pi correctness nor supports blaming Pi for the reported symptoms.

### Remediation boundaries and acceptance

At investigation time, generation readiness meant that a temporary candidate passed a small lexical rejection list, not that the task was understandable. `Task` and `实现` passed this check. A result then became stable without a recorded input-quality assessment. The input prompt used only `preferred_title`, first user, last user, and last assistant excerpts, each capped at 300 characters. Intermediate planning context was absent; raw handoff text could consume the user excerpt. Repair must separate "not enough task information yet" from a transport failure or a valid final title. A later substantive request should allow a previously insufficient candidate to be reconsidered; a trailing confirmation, command, or emotional remark should not replace an established task. This does not require regenerating after every message or waiting until the whole session ends.

Delivered behavior matching those boundaries:

1. Restore the configured shared-gateway route and expose distinct failure causes (missing configuration vs transport/invalid). Current missing-config failures retry when a virtual key appears. Do not restore assistant CLI generation.
2. Rank candidates by task information and source, not minimum length. Preserve useful intent after emotional prefixes; treat closing commands and context-dependent confirmations as secondary. Strip handoff framing before compacting. Do not solve this by filtering only the four reported strings.
3. Give bounded title input explicit provenance and meaningful task context from fields already on the session. When first/last excerpts are insufficient, the model may return `__INSUFFICIENT__`; do not read all transcript databases on the list's first-paint path.
4. Repair only demonstrably invalid accepted cache records (wrapper fragments, empty-session placeholders, bare command labels, context-free action words). Keep valid stable titles. Merely raising `TITLE_CACHE_VERSION` does not invalidate accepted successes.
5. Recheck targeted real sessions after install: insult suffix, `/doc-update` suffix, `Task:` handoff, `实现`/`执行`, cached `空白会话`/`Session recap`, and missing gateway key. Confirm actual displayed task meaning through the installed command.

## Proposed ownership

Keep this feature in the Corral host domain. SessKit remains responsible for parsing native histories and supplying title candidates; it should not own Corral model credentials, scheduling, or presentation policy. Relay continues to transport ciphertext only.

1. **Shared title repository:** host-side `TitleState` used by TUI and remote. Owns resolved text via the durable title file, generation state, and revision. Keep existing successful titles; do not invalidate them because the backend changes.
2. **One generation worker per host/user:** both TUI and remote signal missing work through the same spawn launcher. Retain detached execution, process-level exclusion, batch limits, failure cooldown, and no-task-information filtering. Recheck persisted success under the lock before requesting a model. Provider routing and fallback remain gateway responsibilities.
3. **Independent title updates:** title completion notifies remote subscribers without waiting for history mtime. Use additive monotonic `revision` on list snapshots and metadata events; retain the string `title` for old clients.
4. **One iOS session record per host and canonical session:** still the phone-side follow-on — merge list/search/detail through one identity store and reject stale revisions.
5. **Offline and recovery:** display the last successfully synchronized title immediately while offline. Never regenerate on the phone.

A successful title stays stable as conversation content grows. Re-generation of existing **valid** successes is outside this proposal; demonstrably invalid cached strings may be replaced. Preserve the existing language rule: infer title language from user conversation content; do not translate titles based on phone UI language.

## Delivery sequence and acceptance

1. ~~Extract the host title repository/coordinator, fix remote consumption and generation startup, and wire title-only notifications together.~~ Done on host.
2. ~~Replace the legacy generation transport with the gateway adapter while preserving cache identity and success/failure semantics.~~ Done on host.
3. ~~Fix fallback ranking, handoff/command stripping, generation prompt provenance, insufficient-vs-failure states, and targeted invalid-cache repair.~~ Done on host.
4. Merge iOS metadata through one identity-based store and support updates for visible details/search results independently of normal-list membership. (Phone follow-on; host already emits additive `revision`.)
5. Verify at least five real sessions, then the affected iPhone screen and TUI side by side for the same host/session. Include cold cache with only remote running; generated result with unchanged history; open detail and search results; provisional identity migration; two TUI windows plus a phone (no duplicate completed jobs); failed generation/cooldown; process restart; disconnect/reconnect and late stale responses. Measure title propagation separately from model latency, with a proposed target of two seconds after durable title completion on an established healthy connection.
6. Check existing successes are preserved, title-only changes reach every subscriber, no user cache corruption causes fallback flicker, and gateway unavailability leaves readable titles. Parent handles release, install, and real UI acceptance.

## References

- Current behavior and title-language rules: [Maintainer guide](../MAINTAINER_GUIDE.md#标题与排序).
- Transport and identity boundaries: [Remote knowledge base](../REMOTE_KNOWLEDGE_BASE.md).
- Current gateway policy: [Global LLM gateway guide](~/.config/agentsync/docs/LLM_GATEWAY_GUIDE.md). Its mandatory gateway boundary supersedes the maintainer guide's legacy assistant-CLI transport description for new implementation.
- [Official offline-first guidance](https://developer.android.com/topic/architecture/data-layer/offline-first): separate local reads from network synchronization.
- [Now in Android repository implementation](https://github.com/android/nowinandroid/blob/main/core/data/src/main/kotlin/com/google/samples/apps/nowinandroid/core/data/repository/OfflineFirstNewsRepository.kt): shallow-cloned and inspected for local observable reads plus synchronization writes. Borrow that ownership separation only; do not adopt its Kotlin/JVM stack, Room, or Android scheduling APIs.

<!-- 该文档整理/压缩于 2026-09-29 -->
