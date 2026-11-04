# Ideas mixed input: text, images and files

> Status: **detailed proposal, not implemented** (2026-11-04).
> The owner requirement is adopted in [Web Task Butler, requirement 21](WEB_TASK_BUTLER_DESIGN.md#1-owner-requirements-2026-09-29).
> All technical choices, numerical limits and rollout slices below are recommendations,
> not additional owner decisions. This document does not authorize implementation,
> deployment, a new native client feature or permanent deletion of user data.

## 1. Outcome and scope

The owner can write a thought, paste a screenshot or file at the caret, keep writing, and
later discuss the same material without changing input modes. Text, images and files form
**one ordered piece of content**, not text plus a detached upload basket.

The same capability covers every Ideas content input:

| Surface | Required behavior |
|---|---|
| Today's canvas and editable text in an expanded older day | Mixed content at the caret; existing date ownership unchanged |
| New top-level discussion comment | Mixed content; explicit send |
| Reply to a comment or a reply | The same editor, attachment behavior and one-level forum grouping |
| A future content-edit surface | Reuse this contract if that surface is separately authorized |
| Taken-up text, posted comments and quoted source | Read-only display/preview, not new editable surfaces |

Search, login and identifier fields are not content composition. This does not expand the
request into attaching files to every unrelated form or adding comment editing.

Success means more than seeing a thumbnail: the original bytes survive a normal reload,
the right comment or canvas location retains the attachment, and a real worker can access
exactly the referenced version when execution is enabled. Browser clipboard restrictions
must be disclosed rather than disguised as product support.

## 2. Verified baseline

Inspected on 2026-11-04; paths are relative to the CLI repository.

| Area | Current implementation | Consequence |
|---|---|---|
| Canvas | `web/ideas/src/canvas/editor.ts`, `DaySection.tsx`: CodeMirror 6, Markdown source, taken-span widgets/locks, guarded history and 60 s settling | Keep the editor and its established caret/drag behavior |
| Discussion | `web/ideas/src/discussion/Discussion.tsx`: forum comments; `Composer` uses `react-textarea-autosize`, keeps draft in component state and sends only nonempty trimmed text | Cannot place media between text; an unmounted composer loses its unsent draft |
| Content | `web/ideas/src/model.ts`: `DaySection.text`, `Thread.quote`, `Message.text`; no assets or references | Requires a versioned content/asset contract, not just `onPaste` |
| Persistence | `web/ideas/src/store.ts`: whole JSON snapshot in account-scoped localStorage, 250 ms debounce and exit/visibility flush | No binary storage, durable comment draft, upload queue, conflict protocol or quota-error recovery |
| Product backend | Preview coordinator is simulated in `preview.ts`; no Ideas persistence/upload service or real worker integration | A static preview cannot provide cross-device delivery |
| Existing session images | `embed.save_image_and_paste_path`, `SessionHub.send_image`/`send_turn`, remote `input.image` | Saving also injects into a live session; image-only/base64 transport is not an Ideas asset API |
| Tests | Ideas package exposes build/typecheck, not a behavioral suite | Add editor, persistence and browser tests before declaring support |

Ideas currently has no native Mac/iPhone or TUI content surface. Desktop browsers and
physical iPhone Safari are the affected clients; native clients are not silently added.
This is Corral product-owned content/storage/dispatch, not SessKit history parsing.

## 3. Recommended approach and references

### Choice

**Keep Markdown + CodeMirror 6; share its attachment-capable editing core between the
canvas and a compact discussion composer. Add a separate asset repository.**

| Option | Benefit | Cost / decision |
|---|---|---|
| Add `onPaste` plus an attachment tray to each existing input | Small initial patch | Splits ordering, persistence and undo across two systems; does not deliver free mixed input. Reject as the final design |
| Share CodeMirror mixed-input extensions | Preserves Markdown and current canvas mechanics; one attachment implementation | Some composer/IME work, but no second editor model. Recommend |
| Replace the page with a rich-document editor, for example Tiptap | Strong rich-document ecosystem | Rebuilds exact-source editing, taken-span locking, sentence drag and history. Not justified by this request |

Tiptap does support Markdown through a Markdown-to-JSON-to-Markdown bridge [R8]. This
recommendation is about preserving the existing source/range contract and limiting migration
scope, not a claim that another editor cannot handle pasted images or Markdown.

### Inspected implementations and applicability

- **Existing Ideas editor and forum composer**, paths in §2. Reuse CodeMirror state/history,
  range mapping, guarded taken spans, forum ordering and the existing mobile sheet.
  Do not bring back chat bubbles, status rails, countdown chrome or a formatting toolbar.
- **ProseMirror upload example**, inspected from a shallow clone of
  `https://github.com/ProseMirror/website`, commit
  `a16b4ece45f135e5f001ab59d25f2d43c25974e1`, `example/upload/index.js` [R1].
  Reuse the idea of stable placeholder identity, transaction mapping and ignoring a late
  completion after removal. Do not copy its editor/schema, data-URL demo uploader,
  transient-only placeholder or deletion of failed content. Our references are persisted
  source tokens; completion updates the asset record, never inserts another image.
- **CodeMirror public extension contracts** [R2]: `EditorView.domEventHandlers`,
  `StateField`, `Decoration.replace` and `atomicRanges`. File paste/drop requires an event
  handler; `clipboardInputFilter` transforms text only. A `true` handler result bypasses
  later/default handlers, so routing order must be explicit.
- **Browser APIs** [R3–R7] establish clipboard/file/storage feasibility independently of
  those component references. No Apple-native editor APIs apply to this web feature.

No new editor framework, uploader dashboard, cloud storage provider or shared-platform
extraction is required. A small product-specific import coordinator is appropriate;
editor behavior, file selection and drag gestures still use existing browser/CM APIs.

## 4. User interaction

### Canvas example

```text
The login button is clipped on a small phone.
[ screenshot preview ]
Here is the failing response: [ response.json · 18 KB ]
Keep the current desktop layout.
```

The image is content at that position, not a thumbnail in a separate bottom tray. Files
appear as compact filename/size objects at their source position. Image previews occupy
their own visual line, with preserved aspect ratio and no upscaling. Ordinary file objects
can stay inline. Source delimiters may require a line boundary around an image; no unrelated
paragraphs are rewritten and existing sentence-move spacing rules stay unchanged.

### Shared behavior

- Paste with the normal shortcut or system Paste menu. Drag files onto the caret position.
  A small paperclip button beside each composing surface opens the browser's native
  multi-file picker as a discoverable fallback; no separate upload/confirm screen.
- Insert at the selection, not the end of the page. Refuse a replacement intersecting
  taken content; do not partially delete a selection while inserting files elsewhere.
  Dragging onto a taken object resolves to its nearest valid edge with the existing caret.
- Allow text before, between and after attachments. Attachments alone are valid content:
  the send predicate is meaningful text **or** at least one valid attachment reference.
- Image click opens a dismissible accessible preview; file action downloads the original.
  Local drafts can preview/download their local bytes. A missing original on another
  device is an explicit unavailable item, not a fabricated preview or an empty space.
- Keep the same attachment dimensions and action slots across canvas, quotes and comments.
  Proposed preview cap: the lesser of content width, 320 px height and 40% of the viewport;
  use a smaller common cap inside the composer so the send button remains reachable.
  These are initial layout values, not fixed accessibility dimensions.
- Removing an unsent attachment is an ordinary reversible edit. Backspace/Delete and
  selection treat its source token atomically; one paste is one undo step. Undoing an
  import never resurrects it when a background task finishes. Redo reuses retained bytes.
- Give attachment actions accessible names including filename, keyboard preview/removal,
  visible selection/caret and screen-reader image descriptions. Do not use color alone.
  Reordering can use selection/cut/paste first; if native dragging of attachment objects
  is enabled, provide its own visible drag affordance. The no-grip exception applies only
  to taken sentences and is not inherited by every new draggable object.
- Normal background replication has no extra progress badges or sync banner, per the
  [offline authority](~/.config/agentsync/docs/OFFLINE_FIRST_ARCHITECTURE_GUIDE.md#8-建立用户对本地数据的信任).
  Show an inline exception only when a user must act: unreadable source, storage failure,
  rejected size/type, expired access or permanent server rejection. Keep useful content.

### Discussion-specific behavior

Keep oldest-first comments, author/time, Reply and one-level nesting. The original sentence
is still the post; attachments do not convert the forum into chat or a task-card feed.

The compact composer uses the same editor core without canvas take-up, day grouping or
settle scheduling. Keep the current keyboard contract: desktop Enter sends, Shift+Enter
inserts a line break; touch Return inserts a line break and the button sends; IME composition
never submits. Preserve caret/selection and the reply target around file-picker focus.

Persist one unsent draft per account/thread/reply target. Closing the panel, switching
threads, cancelling a reply target or reloading does not discard it. Cancelling a target
returns to the root draft while retaining the targeted draft. Successful local submission
clears only that exact draft revision after the transaction commits; typing while a send
is being committed cannot be cleared by its late completion.

## 5. Clipboard and file semantics

Use user-initiated `paste` and `drop` events as the primary path. Read the event's files and
strings synchronously before any `await`; call `preventDefault()` synchronously only when
this handler owns the operation. No clipboard polling, global keyboard interception,
automatic permission prompt or repeated fallback read of the same paste.

| Input | Initial behavior |
|---|---|
| Text only | Leave ordinary CodeMirror text paste untouched |
| Screenshot / image bytes | Import once; show a local preview at the selection |
| Multiple files | Import every exposed file in source order, within limits; preserve each occurrence |
| Text plus actual image/file bytes | One ordered insertion transaction; preserve nonempty text as well as files |
| Same copied object in several MIME representations | Select one supported representation per proven logical item, not every representation as another attachment |
| Rich text with external image URLs but no bytes | Keep text/link; never automatically fetch or authenticate to the source site. Explain unavailable embedded images briefly when omission would otherwise be silent |
| `file://` URL, filesystem path or filename only | Treat as text. It does not authorize reading local bytes |
| PDF, source code, JSON, ZIP, office document, audio/video | Store original as a file; no automatic execution, extraction, macros, transcription or conversion |
| HEIC/TIFF/unknown or unsupported image decoder | Retain as a file with download; optional safe derivative later, never reject solely because inline preview is unavailable |
| Directory | Do not recurse implicitly. Give a concise error and allow file/ZIP selection |

Clipboard data does not always preserve the spatial order of Word/webpage text and images.
When a trustworthy internal fragment or a verified HTML-to-byte mapping supplies order,
retain it. Otherwise preserve the plain text and insert the exposed files after it at the
same selection, in file-list order. Do not claim lossless arbitrary rich-document paste.
Initial scope does not include general HTML-to-Markdown conversion; never inject copied
HTML into the page. Explicit Markdown remote-image syntax is a link unless separately
requested for import; do not start third-party tracking requests just to render it.

Internal copy/paste between Ideas editors may carry a versioned fragment alongside a
plain Markdown fallback. Validate ownership and every referenced asset; mint fresh occurrence
IDs on copy. A hash or custom MIME is not authorization. Cross-account or cross-origin paste
without usable bytes must not resolve another account's private references. No promise that
all browsers retain custom MIME or can export several binary files to unrelated apps.

**Browser limit:** `ClipboardEvent.clipboardData` exposes only what the browser supplies,
and `getAsFile()` can return null [R3]. Async `navigator.clipboard.read()` has secure-context,
permission/activation and MIME constraints [R4]. Its availability does not imply access to
arbitrary Finder/Files copies. Verify each actual browser/OS/source combination. If bytes
are not exposed, native file selection and file drag remain the dependable alternative;
there is no silent URL/path-based workaround.

## 6. Content and asset model

Preserve Markdown as the source of ordering and text anchors. Do not store base64, object
URLs, local absolute paths or expiring download URLs in document text.

```markdown
The button is clipped.
![Small-phone screenshot](ideas-asset:ref_01)
Failing response: [response.json](ideas-asset:ref_02)
```

`ideas-asset:` is an application reference, not a browser navigation scheme. Only the
Ideas renderer resolves it. Escape labels with a Markdown-aware serializer; code fences,
inline code and escaped examples do not create attachment references.

Proposed entities:

| Entity | Durable identity and facts |
|---|---|
| Content revision | Owner, document/message ID, revision, schema version, exact Markdown, actor and timestamps |
| Asset | Owner-scoped random ID, original display name, declared/detected MIME, byte count, original-blob ID, optional dimensions/preview derivative |
| Blob | Owner-scoped opaque storage identity and server-computed SHA-256; immutable original bytes |
| Attachment occurrence | Random reference ID in Markdown, asset ID and owning content identity; revision-specific use is derived from parsing that revision |
| Composer draft | Owner/thread/reply target, revision, Markdown and durable local asset dependencies |
| Pending operation | Operation ID, account, immutable payload/revision, dependencies and reconciliation receipt |

Separate asset, blob and occurrence identity. Two files may share bytes but have different
names; two intentional pastes of the same file remain two occurrences. Deduplicate physical
bytes only within an authorized owner boundary, not visible content or explicit sends.
The server does not reveal another account's hashes/existence through dedup responses.

Parsing Markdown determines **which references the current revision uses**. A cached list
of references is a validated projection, not another editable source of ordering. Resolve
asset metadata from the repository. A worker snapshot pins the content revision and original
blob hashes, so later draft edits cannot change the material already taken up.

Read-only rendering of comments, source quotes and exports uses one attachment-aware
renderer. Migrate old strings as version-1 content without rewriting whitespace or offsets.
Do not render the literal source token to ordinary users when an asset cannot be resolved;
show a named unavailable file instead, while keeping recoverable source available in export.

## 7. Editor integration and difficult races

Recommended modules under `web/ideas/src/`:

- `input/`: shared CodeMirror editing host, import event routing, attachment syntax/widgets,
  atomic selection and per-surface keyboard policy.
- `assets/`: import validation, repository interface, local bytes/derivatives and sync worker.
- `content/`: versioned Markdown/reference parsing and read-only rendering.
- `persistence/`: transactional local drafts, migrations, pending operations and audit.

These are product-local boundaries, not a new generic editor platform. React mounts the CM
view once per content identity; background metadata changes do not recreate it or move the
caret. Existing sentence drag is dispatched first for its own internal drag type; external
files then go through asset import; ordinary text remains with CodeMirror. A handled event
must not also run CodeMirror's default insertion.

### Import sequence

1. Capture selection, owner, content identity and revision. Read the clipboard/drop payload,
   validate cheap limits and allocate import/asset/occurrence IDs. A count/total-size violation
   rejects the batch before replacing any selection; never silently import only the first N.
2. Insert text and source references in one undoable transaction, immediately rendering
   local placeholders/previews. Map ranges through all subsequent edits.
3. Commit original Blobs, occurrence metadata, changed draft and its audit event in one
   IndexedDB transaction. Only transaction completion establishes local durability.
4. Decode/derive bounded thumbnails independently. Background replication updates repository
   facts, not Markdown or editor history. No second insertion when a URL becomes available.
5. Completion resolves IDs, never the current caret. If an occurrence was removed, owner
   changed or the draft was replaced, do not restore it or attach it to another document.

The local commit is asynchronous. A process killed before it commits cannot be promised a
saved file; signal an exceptional unsaved state if it fails, retain in-memory content and
the original selection recovery data, and never clear the composer. Do not depend on
`pagehide` starting a fresh asynchronous save successfully. Persist continuously and keep
only short, serialized local transactions on the critical path.

An import from an older editor state must not overwrite a newer whole-document snapshot.
Serialize revisions and merge dependency metadata into the current revision, or reject and
rebase mechanically; test typing and undo while a large Blob write is pending. A later
per-file read/validation failure stays as a named failed item in its original position with
replace/remove actions; the other content remains editable and retained. Never submit an
unresolved failed occurrence as if the material were present, and never silently drop it.

### Taken spans and attachment layout

The existing `SentenceWidget.textContent` cannot display an embedded attachment. Separate
**logical read-only ownership** from rendering: lock the complete taken source range,
including its reference tokens; render text fragments and attachment widgets in nonoverlapping
ranges. Do not nest or overlap CM replacement decorations. Multi-line image previews need
state-provided layout-changing decorations and measured geometry; viewport-only decoration
callbacks must not introduce block height [R2].

Take-up ranges must end at valid token boundaries and include the exact attachment occurrences
being accepted. A frozen sentence and its associated attachments retain one logical ownership
snapshot. Moving that taken unit carries its complete source/reference association within
the same day; it does not duplicate an attachment or edit the sent material. Existing text-only
sentence drag, wrapped caret geometry, no-extra-spaces rule and undo protection must regress
cleanly. Changing a taken image/file is a discussion reply, never an in-place replacement.

## 8. Persistence, offline operation and recovery

### Local repository

Use IndexedDB for drafts, content, original Blobs, references, pending operations and audit
[R5]. Reserve localStorage for small compatibility markers/preferences, not attachment data.
Original file content never goes through base64 JSON snapshots. Object URLs are transient:
create for mounted previews, revoke on disposal, and regenerate from durable bytes on reload.

Local user-content writes and their actor/action/target/time audit rows commit together.
Do not put file content, authentication material or sensitive paths into diagnostics.
Request persistent storage where supported and inspect capacity, but handle refusal and
`QuotaExceededError`; browser storage is not an archival backup and cannot survive explicit
site-data clearing or all browser eviction policies [R6]. Keep the local-only preview's limit
clear in documentation, not a permanent tutorial banner.

A versioned migration imports the current account's legacy JSON into one transaction and
sets a migration marker only after validation. Retain the legacy copy until readback passes;
never fall back to sample seeds over recoverable user data after a parse/migration error.
A legacy email-scoped key is not a production authorization identity: the authenticated
backend must supply a stable owner mapping. Do not infer ownership of other local keys by
matching display names or bulk-import every stored account.

### Offline capability table

| Capability | Without network |
|---|---|
| Write/paste/edit a canvas or comment draft | Fully local after durable commit; no upload wait |
| View/download locally held originals | Available |
| Read a remote attachment never fetched here | Named unavailable item; fetch when access returns |
| Submit a discussion reply | Locally append and enqueue atomically; remote acceptance/agent response still pending |
| Automatic canvas take-up and worker start | Wait for accepted content and complete asset dependencies; never invent a local worker |
| Continue in another device/browser | Requires replication; local-only preview cannot provide it |
| Export content whose originals are local | Available without network |

**Offline unit:** the active day or opened discussion plus all original assets referenced by
that local working set. New imports always belong to it. Prefetch remote originals for the
opened content within the local storage budget; do not call a unit fully offline-ready until
its dependencies are present. Other history loads on demand. Keep unreplicated originals,
unsent drafts, pending operations and undo-retained assets out of cache eviction.

### Sending and synchronization

Submitting a reply commits the local post, its exact body/assets and an outbox record before
clearing the composing revision. Send attempts keep the same operation ID. A new intentional
identical comment has a new operation ID; content hashes are not message deduplication keys.

Upload missing bytes first; commit the comment/revision plus server audit and coordinator
notification-outbox event atomically only when all its references are ready and authorized.
An unconfirmed response is reconciled by operation ID/receipt. Do not forward just the text,
then silently append a missing screenshot as a different instruction. Other independent
comments/blocks continue while one waits. Routine queue/upload states stay unobtrusive;
never label local acceptance as delivery to an agent, and retain the owner's question until
its actual answer-processing state changes.

After reload, abandoned in-flight operations return to reconciliation/retry, not permanent
spinners. Expired authentication pauses only that account's outbox; other account credentials
never transmit it. Abort network work and revoke mounted object URLs on account change,
retain account-scoped unsent bytes, and resume only under the matching identity.

Initially keep the parent's single-editing-tab lease and revision checks rather than adding
a CRDT. Define a service-owned document lease for cross-device editing, not merely a browser
lock. Offline drafts remain editable if the lease expires, but cannot overwrite a conflicting
server revision: preserve both drafts, auto-merge only proven nonoverlapping changes, and
ask for semantic resolution when necessary. Comments are append-only independent creations.

## 9. Backend boundary and API proposal

### Modes

1. **Current static-preview extension:** local repository, mixed input, durable drafts,
   preview/download and export work in this browser. No remote sync or real-agent claims.
2. **Production Ideas service:** add authenticated content/asset persistence to the planned
   Corral Ideas backend, not to SessKit and not to the static file server. Local loopback
   token/origin mode and hosted multi-account mode use explicit authentication adapters.
   A browser session is not interchangeable with a paired remote-client key.

Recommend the planned Python/SQLite product ledger with files outside its webroot; no new
JVM, container or independent upload microservice. Default local service storage lives under
`XDG_CONFIG_HOME/corral/ideas/` (`~/.config/corral/ideas/`). Hosted deployment supplies its own
private service-owned path. Storage keys use server-issued owner/asset identities, never a
submitted filename or a user-provided filesystem path.

### Proposed HTTP contracts

| Operation | Essential contract |
|---|---|
| `POST /api/ideas/assets` | Authenticated owner; idempotent import operation, filename/size/declaration; returns owner-scoped asset/upload ID and chunk policy |
| `PUT /api/ideas/assets/{id}/chunks/{index}` | Bounded binary body, declared length/hash; same chunk bytes replay safely, conflicting bytes reject |
| `POST /api/ideas/assets/{id}/complete` | Verify complete byte count, chunk coverage, final server-computed digest and safe type/decoder policy; returns durable asset receipt |
| `GET /api/ideas/assets/{id}/content` | Authorize the referring content and owner on every request; safe disposition/type; no public permanent bearer URL |
| Save content / append comment | Scoped base revision or fresh message ID, immutable operation ID and validated ready asset references; transactional audit and durable event |
| Reconcile changes/operations | Durable cursor and receipts; reconnect works even when live notifications were missed |

Paths are proposals, not existing endpoints. A proposed 4 MiB chunk size bounds retries and
memory; no base64 payloads over the remote session WebSocket. Transient errors retry with
bounded backoff, while permanent quota/access/content rejection preserves the draft and
reports its affected item. Reusing an operation key with a different payload is a conflict,
not success. Define receipt retention at least as long as supported offline-outbox replay;
expired operations require reconciliation, not blind replay.

Filesystem and SQLite are not one transaction. First write bounded staging bytes, verify,
flush and atomically publish the immutable file; then commit the ready metadata, references
and audit. A crash may leave an unreferenced file, never a committed ready reference to
missing bytes. Recovery reconciles staging/orphans under the declared retention policy.
Do not make permanent deletion an implicit consequence of removing a thumbnail.

## 10. Coordinator and worker delivery

Images/files are first-class source material, not decoration for humans only.

- The canvas settle deadline remains last **semantic edit + 60 s**. Replication or thumbnail
  completion is not a new user edit and does not restart it. Dispatch eligibility additionally
  requires a durable accepted revision and all assets in that block ready; wait for the
  slower condition. Do not stop unrelated settled blocks.
- Take-up records revision, source ranges, asset occurrences and original hashes. An
  attachment-only block is eligible; the coordinator decides whether it is actionable and
  how it relates to surrounding text. No hard-coded screenshot-to-task assumption.
- A discussion reply bypasses settling, but reaches the coordinator only as a complete,
  idempotent body with its authorized asset manifest. Attachments to a taken requirement
  are explicit follow-up instructions, just like textual replies.
- Worker materialization copies only the task's pinned assets to a task-scoped directory
  accessible to the actual executing host. Verify sizes/hashes after transfer. An HTTP URL
  requiring the owner's browser cookie, or a path on a different computer, is not a handoff.
- For images, use a verified native image-input capability where available; otherwise use
  an explicitly supported local-file reading mechanism. For arbitrary files, pass the
  materialized file references with names/types/hash and original ordering. Never silently
  replace image input with OCR or claim that a path string proves model perception.
- Reuse AgentSessionControl for generic execution/input mechanisms and Corral host adapters
  for product asset ownership. SessKit supplies normalized native history evidence only;
  do not introduce a second executor or runtime-history parser for Ideas.
- Existing `save_image_and_paste_path()` is not a storage primitive: it injects a path as a
  side effect, has image-specific limits/conversion and no asset lifecycle. Do not call it
  on paste/import. A future bounded pure materialization contract may be extracted only
  when execution integration needs it, preserving the current session path's behavior.
- Record each stage separately: locally persisted, accepted by Ideas, materialized on the
  worker host, input accepted by the runtime, and content actually usable by the worker.
  An uncertain partial injection is not safe to replay wholesale. Preserve the existing
  delivery-receipt/reconciliation boundary.

Each active runtime (Claude Code, Codex, OpenCode, Cursor, Pi) needs a real image/file input
probe with requested and observed model/capability. The design does not claim their models
all accept images, add dormant Kimi to the UI, or silently change runtime/model. Missing
capability is a fact for the coordinator/owner, not permission to guess a fallback.

## 11. Security, limits and retention

Apply file-upload defense in depth [R7]. File storage acceptance and safe preview/processing
are different permissions. Proposed initial limits: **50 MiB per original, 200 MiB and 20
files per import**, with server-owned account/storage quotas. They are tunable guardrails,
not a limit inherited from the existing 8 MiB session image helper. Decode budget and image
pixel limits are separate from compressed size and require real stress tests.

- Permit ordinary originals as inert downloads, including code/archives. Preview only a
  narrow safely decoded raster set; SVG, HTML, PDFs and unsupported image formats default
  to file presentation in the first slice. Keep original bytes unchanged; derived previews
  have separate identities, strip unnecessary metadata and never replace the original.
- Never serve uploaded active content as executable same-origin HTML. Use attachment
  disposition, `nosniff`, safe generated storage names, authorization, restrictive CSP and
  a sandboxed/isolated preview boundary where a richer preview is later justified.
- Validate actual size/type and decoding, not just extensions or client MIME. Bound
  concurrency, decompression, dimensions and derived output. Do not auto-unzip, run scripts
  or send private uploads to public malware-scanning services. Any malware scanning must
  use an approved private boundary and cannot be advertised as a complete safety proof.
- Authenticate every read/write, including thumbnails and chunks. Bind ownership to the
  authenticated principal; ignore caller-asserted owner IDs. Protect writes from CSRF and
  follow the [frontend origin contract](~/Codes/_standards/frontend.md#同源表单与来源校验所有前端必做),
  including same-origin Safari's null-Origin case. Rate/size limits apply before full reads.
- Treat attachment contents as untrusted source material for agents, not higher-priority
  instructions. Do not let filenames become shell commands, traversal paths or overwrite
  targets; materialize under generated per-task names with bounded permissions.
- Initial implementation retains unsent imports and referenced originals. Cleanup candidates
  exclude live revisions, discussions, accepted snapshots, export jobs, pending operations
  and supported undo history. An owner-approved retention policy must define grace periods,
  undo lifetime, backups and final deletion before enabling automatic permanent cleanup.
  Enforce quotas in the meantime; metadata removal alone never authorizes byte deletion.

Performance proposals: immediate paste acknowledgment within one frame where practical,
no network wait for editing, at most two derivative decodes/uploads concurrently, and no
re-render of unrelated day editors on thumbnail completion. Avoid whole-batch ArrayBuffers
or base64 copies; compute canonical hashes incrementally on the service and bound client
hashing to one limited file at a time when needed. Measure long documents and the 16 GB Mac,
not only a single empty-screen paste. Numerical targets require spike measurements.

## 12. Export and portability

Offer an export package containing Markdown, original files and a manifest of source IDs,
original names/types and hashes. Rewrite internal references to generated relative paths
under `assets/`, not expiring URLs or local absolute paths. Preserve forum authorship,
reply relationships and taken snapshots in the structured export; do not flatten them into
an ambiguous chat log. If some originals are unavailable, name them and require an explicit
choice for an incomplete export instead of announcing a complete package.

This is the recovery path for local-only preview content and a portability requirement for
production assets, not a new requirement to publish private files or bundle them in Git.

## 13. Implementation slices and acceptance

The slices are dependencies, not a partial-feature completion claim. Proposed effort is
roughly **8–16 engineering days** across risk probes, local editing, backend, runtime bridge
and acceptance, assuming the existing page/hosting foundation; revise after the probes.
No date or parallel-worker plan is committed by this estimate.

| Slice | Scope | Pass bar |
|---|---|---|
| A: risk probes | Actual clipboard payloads; shared CM composer on physical iPhone; mixed taken spans and async undo | Resolve file-exposure, IME, geometry and freeze-token risks before production UI work |
| B: complete local preview capability | Shared editor in every content input; IndexedDB migration/drafts/bytes; preview/download/export | Same-browser end-to-end paths pass, no server/worker claims |
| C: production content/assets | Account-scoped service, chunk transfer, versioned content, outbox/receipts, authentication, working-set replication | Two accounts and two devices; offline/reconnect/conflict/crash tests; no text-only early delivery |
| D: actual worker use | Immutable task materialization and existing session-control integration | Real affected runtime/model probes and task evidence, not a simulated coordinator |

B can be reviewed as a clearly scoped local-preview improvement; it is not completion of
cross-device or worker delivery. Production feature completion requires C and D.

### Acceptance matrix

1. **All inputs:** canvas today/older day, new comment, reply and reply-to-reply each accept
   text + image + arbitrary file and attachment-only input. Same ordering, deletion and
   errors; keyboard-only and screen-reader routes remain usable.
2. **Clipboard reality:** actual macOS screenshot copy, browser Copy Image, Finder file
   copy, file drag, native picker, copied webpage text/images, and multi-file selection in
   desktop Chrome and Safari; physical iPhone Safari with Photos/Files/system Paste and
   touch keyboard. Report exposed bytes and unsupported combinations separately. Synthetic
   ClipboardEvents are unit tests, not evidence that OS clipboard access works.
3. **Editor races:** paste mid-sentence and over selection; continue typing while import
   commits; remove/undo before thumbnail/upload completion; redo; reversed completion order;
   selected ranges crossing taken spans; wrapped caret; sentence drag then external file
   drop; no duplicate insertion and no source mutation by background completion.
4. **Durability:** reload after local commit; close/reopen reply panel; switch targets;
   force process termination; corrupt legacy JSON; storage denied/full; IDB migration
   interrupted; object URL lifetime; unsent originals never evicted by app cleanup.
5. **Offline/sync:** paste/send with network blocked, reload, reconnect; accept response
   lost; restart during chunking; server rejects after a delay; same operation replay vs
   fresh identical comment; stale editing tab/device; older snapshot cannot erase new data.
6. **Isolation/security:** account switch/logout during import/upload, guessed asset IDs,
   copied cross-account tokens, malformed MIME, SVG/HTML, traversal names, huge decoded
   image, malicious archive, quota/rate limits and private GET/thumbnail/chunk authorization.
7. **Agent path:** known test image with an unpredictable visible marker and a known file
   with a random internal value; real worker returns both correctly from pinned bytes.
   Then change the unaccepted draft, reply with another file and verify version/ordering,
   partial-input recovery and no replay. Do not infer this from upload success or a path echo.
8. **Presentation:** fresh inspected desktop light/dark/narrow captures plus the physical
   iPhone keyboard-open composer, three mixed filename/preview peers, error recovery and
   accessible preview dismissal. Regression: forum layout, folded days, taken-sentence
   colors/drag/spacing, no new chrome, no layout jump or stolen focus.
9. **Reproducibility:** complete TypeScript/build diagnostics plus new deterministic and
   browser tests. Existing Corral image/partial-delivery regressions apply only if those
   mechanisms change. Record source/artifact, actual path and matching evidence per claim.

No implementation tests, clipboard device probes, new screenshots, runtime calls or
cross-device attachment behavior were performed for this design-only task.

## 14. References

- **[R1]** ProseMirror, upload placeholders and transaction mapping:
  https://prosemirror.net/examples/upload/ ; inspected source:
  https://github.com/ProseMirror/website/blob/a16b4ece45f135e5f001ab59d25f2d43c25974e1/example/upload/index.js
- **[R2]** CodeMirror 6 public reference, event handlers/state/decorations:
  https://codemirror.net/docs/ref/ ; Context7 `/codemirror/view` public API/source query,
  retrieved 2026-11-04. Installed dependency range in the Ideas package: `^6.43.15`.
- **[R3]** Clipboard event data and file extraction:
  https://developer.mozilla.org/en-US/docs/Web/API/ClipboardEvent/clipboardData ;
  https://developer.mozilla.org/en-US/docs/Web/API/DataTransferItem/getAsFile
- **[R4]** Async Clipboard constraints and multiple representations:
  https://developer.mozilla.org/en-US/docs/Web/API/Clipboard/read ;
  https://webkit.org/blog/10855/async-clipboard-api/ (historical WebKit implementation
  explanation, not a current all-version MIME support matrix).
- **[R5]** IndexedDB transactions and Blob storage:
  https://developer.mozilla.org/en-US/docs/Web/API/IndexedDB_API
- **[R6]** Persistence requests and browser-controlled retention:
  https://developer.mozilla.org/en-US/docs/Web/API/StorageManager/persist ;
  https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria
- **[R7]** OWASP file upload defenses:
  https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- **[R8]** Tiptap's Markdown/JSON bridge, considered as an alternative rather than adopted:
  https://tiptap.dev/docs/editor/markdown
- Native file picker file-list contract:
  https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/files

Product interaction, session control and global rules remain in their authorities:
[Web Task Butler](WEB_TASK_BUTLER_DESIGN.md),
[session delegation](AGENT_SESSION_DELEGATION_DESIGN.md),
[UI design](~/.config/agentsync/docs/UI_DESIGN_GUIDE.md),
[Web interaction](~/.config/agentsync/docs/WEB_APP_INTERACTION_GUIDE.md),
[offline architecture](~/.config/agentsync/docs/OFFLINE_FIRST_ARCHITECTURE_GUIDE.md), and
[data integrity](~/.config/agentsync/docs/BACKEND_DATA_INTEGRITY_GUIDE.md).
