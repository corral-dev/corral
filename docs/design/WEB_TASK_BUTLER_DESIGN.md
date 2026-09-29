# Web Task Butler (draft-driven dispatch) — requirements and analysis

> Status: **analysis draft; owner decisions recorded, design freeze pending** (2026-09-29). No product code yet.
> Read before planning, implementing, or reviewing the local web idea editor, the
> dispatcher agent (X), worker agents (Y), or Corral changes made for them.
> Items marked *Proposed* are unadopted suggestions; §8 records owner decisions.

## 1. Owner requirements (2026-09-29)

1. Use Corral's project discovery to know every local project.
2. A free-form local web **Markdown** editor. Text that closely matches a project name is
   rendered in a distinct style as the user types.
3. The owner keeps adding idea drafts continuously and loosely.
4. One dispatcher agent **X** always knows every task, its state and its owner. X turns the
   drafts into tasks, detects strong dependencies, and runs everything else in parallel.
   Worker agents are collectively **Y**.
5. X and Y sessions are created and driven through Corral; Corral will need changes.
6. When a task is done, X finds the words/sentence in the owner's original text that best
   match it: a green circular check icon at its top-right, the text turns green and becomes
   read-only, and clicking it opens a result popover.
7. If the owner edits text after dispatch, X updates the running Y with new instructions in
   near real time.
8. Deciding **when** to prompt X is the key to X. Debounce is required. X stays one session.
9. Agents (X or Y) may ask the owner for details or decisions. The editor design is the key
   to the user experience.

10. The owner judges this project deceptively simple: every detail matters, and it requires
    deep investigation, testing, and careful design **before** implementation.
11. **The product is agentic.** Do not design it with conventional rule-based software
    thinking: whenever a situation calls for judgement (duplicates, failures, retries,
    deleted ideas, dependencies, which assistant), X decides. The system supplies facts,
    tools and guarantees, not hard-coded workflow policies.
12. **Nothing is deleted in the editor.** Deleting marks the text with a strikethrough; the
    text stays visible. X is told which text the owner struck out and decides what to do.
13. **Agents talk inside the document.** When X needs details or a decision, it inserts its
    own question as text near the related idea, visibly marked as agent-written. The owner
    answers by writing in the editor whenever convenient. No separate question cards.
14. Questions awaiting the owner are also pushed to the phone.
15. SessKit and OpenConductor may be changed, optimized, used as references, and have modules
    extracted or updated for reuse.
16. Use an explicitly selected low-cost model, such as Haiku or Luna, when calling real agents
    for debugging and risk experiments; do not silently use the CLI's default model.

### Delivery approach (*Proposed*, follows requirement 10)

1. **Research**: study existing orchestrators (Conductor OSS, claude-orchestrator,
   Multiclaude, Vibe Kanban, Nimbalyst, Claude Code agent teams) from source — how they
   represent tasks, detect completion, surface questions, and handle mid-task changes.
2. **Risk experiments**, each with a pass bar, before any product code:
   injection reliability per assistant (mid-turn, confirmed delivery); task-level completion
   accuracy; anchor survival under move/paste/undo; X judgement quality on a corpus of messy
   drafts (task split, dependencies, steer vs. new task); trigger timing replayed from
   recorded typing; editor cursor stability under background decoration updates; X session
   behaviour over hours (compaction, restart from ledger).
3. **Design freeze**: every §6.1 guarantee has an acceptance check and every §6.2 judgement
   an evaluation case; UI prototype images reviewed by the owner.
4. **Test layers**: deterministic tests for ledger/anchors/triggers; recorded-replay tests
   for typing → X rounds; an evaluation set for X decisions; end-to-end runs with real
   assistants on disposable projects; browser screenshots/recordings for the editor.

## 2. Existing capability (verified in source, 2026-09-29)

| Need | Present today | Gap |
|---|---|---|
| Project list and name matching | `projects.discover()` / `match_projects()` (session cwds ∪ git roots) | Aliases; common-word names (`Go`, `Mirror`, `Research`, `planet`) cause false matches |
| Create a hosted session | `SessionHub.new_session()` in `remote/sessions.py`, tmux keepalive backend | Lives in the phone daemon; needs a shared session-control layer usable by a local web service and by X |
| Send text into a running session | `SessionHub.send_text()` (tmux paste + Enter, phone path always steers) | Delivery can be partial (`PartialInjectionError`); X needs a confirmed-delivery result |
| Know a turn finished / was interrupted | SessKit `completion_id` (see `AGENT_COMPLETION_NOTIFICATIONS_DESIGN.md`), attention `waiting` | A finished *turn* is not a finished *task*; needs X's judgement plus Y's final summary |
| Machine-readable queries for agents | `agent_api.py` (read-only list/search/show/export) | No write commands (create task, dispatch, steer, ask, mark done) |
| Local web service | None (`websockets` is already a dependency) | New |

## 3. Core architecture (*Proposed*)

- **Deterministic ledger, LLM at the edges.** Corral owns a task ledger (task, state, owner
  session, dependencies, text anchors, questions, results). X never holds the truth in its
  context; each X round receives the ledger snapshot plus the change set, and acts only
  through validated Corral commands. X can therefore be restarted or compacted without loss.
  (Same lesson as OpenConductor's "deterministic core + model only for semantics".)
- **The document is shared by owner and X.** Owner text, struck-out text and X-written text
  all live in the Markdown file (strikethrough as Markdown strikethrough; X text carries an
  authorship marker so the file alone shows who wrote what — exact syntax fixed at design
  freeze). Task anchors, states and results live in the ledger and are rendered as view-only
  decorations.
- **Mechanics vs. judgement.** The system guarantees mechanics (anchors follow text, typing
  is never disturbed, nothing is lost, delivery is confirmed, done text is locked). Every
  semantic choice belongs to X's instructions, not to code.
- **Storage** under `~/.config/corral/`; every ledger mutation records actor
  (owner / X / Y / system), action, target and time in an append-only audit log.
- **Local-only web service**: bind loopback, random access token, origin check. It can start
  agents that skip permission prompts, so exposure equals remote code execution.

## 4. Editor (*Proposed*)

- **CodeMirror 6**, not Milkdown/Tiptap: those rewrite Markdown through a ProseMirror model,
  while CM6 keeps the text exact and supports mark/widget decorations, `atomicRanges`, and
  `changeFilter` for read-only ranges.
- Visual states on anchored text: waiting-to-dispatch (dotted underline, cancellable),
  running (subtle gutter dot), waiting on owner (amber), failed (red), done (green text +
  green check, locked; click opens result popover with summary and a link to the session).
- Done text is read-only. New text written beside it is simply new input for X.
- **Deleting never removes text**, whether or not X has seen it: delete, backspace and
  overwrite always turn the text into strikethrough (overwriting = old text struck + new
  text inserted). There is no true deletion anywhere in the editor.
- **X's questions are text in the document**, inserted near the related idea in a distinct
  agent style. The owner answers by writing anywhere nearby; X reads the answer on its next
  round. A top-bar count and a keyboard jump go to the next unanswered X question. Y's
  questions go to X first; X answers from the document when it can.

## 5. When X is prompted (*Proposed*)

- Unit of change is a **block** (paragraph / list item), never a keystroke.
- A block is *settled* when the cursor leaves it, Enter starts a new block, or it has been
  idle ~8 s. Only settled blocks produce changes; whitespace/format-only diffs are dropped.
- Blocks linked to a **running** Y use a shorter delay, because Y is working in a stale
  direction.
- **One X round in flight.** Changes arriving during a round are merged into the next one.
- Y events (turn completed, waiting for an answer, failed, quota exhausted) also wake X.
- Each round sends: changed blocks (before/after), linked task ids, ledger snapshot.
- New ideas get a short visible dispatch countdown; continued editing restarts it, so
  half-written ideas are not dispatched.

## 6. Edge cases

Split by requirement 11: the system guarantees mechanics; X judges everything else.
Each item still needs an acceptance check (mechanics) or an evaluation case (X judgement).

### 6.1 System guarantees (mechanics)

- **Anchors follow text** through typing, moves, cut/paste, merges and undo/redo. Identical
  text removed and re-inserted within one settle window is reported to X as a move.
- **Undo/redo** changes text only; it never undoes a dispatch or unlocks done text.
- **Strikethrough, not deletion**, for all text (§4); the change sent to X says which text
  was struck. Whether struck text is a typo fix or a withdrawn idea is X's judgement.
- **Completion marking**: X names the words to mark; the system verifies they exist in the
  current text and otherwise uses the task's stored anchor range. It never guesses a span.
  A span shared by several tasks turns green only when all of them are done.
- **Project highlight** is a hint only: whole-word matches, short/common names matched
  case-sensitively; clicking a highlight offers "not a project", remembered for that phrase.
- **Typing always wins**: background updates never move the cursor or re-layout the block
  being edited; its decorations and X insertions wait until it settles, and X never inserts
  inside the block the owner is editing.
- **Never lose the draft**: saved locally in the page and in the service; reconciled by
  version after restarts; only one tab edits, others are read-only with an "edit here"
  takeover.
- **Restarts**: the ledger is persisted; after sleep or restart the system reconciles the
  ledger with live sessions and tells X; no action is replayed twice.
- **Confirmed delivery** of every message X sends to Y; failed delivery is reported to X.
- **Phone push** for X questions that await the owner; answers are written in the editor.
- **Result popover** shows X's plain-language summary, project, verification evidence,
  assistant used, time taken, and "open session in Corral".
- **Y sessions** appear in Corral's normal session list, grouped under the butler.

### 6.2 X decides (judgement, covered by X's instructions and evaluation set)

- How text maps to tasks: one sentence → several tasks, scattered sentences → one task,
  later text amending earlier ideas, repeated ideas (including ideas already done).
- What struck-out text means for its task (stop, keep, adjust) — requirement 12.
- Edits to dispatched text: update, steer, or stop and restart Y.
- Which project an idea targets, aliases, several projects per idea, and a project that does
  not exist yet (X asks in the document when it needs the owner, e.g. a new project's name).
- Dependencies, including ones discovered after tasks started.
- Failures, retries, quota exhaustion and switching assistants.
- Whether a Y's finished turn is an accepted completion (Y must report what changed and how
  it was verified; X may send it back).
- Whether the owner's nearby writing answers an open question; whether to ask at all.

## 7. Risks

- Steering interrupts a running Y; X must choose steer vs. queue vs. cancel-and-restart.
- X token cost grows with edit frequency; block settling and merging bound it.
- Project-name false positives; ambiguity must be resolvable by the owner.
- A quota-exhausted or logged-out Y cannot be asked for a status report. Corral must retain
  the last verified session/workspace state so X can move the task to another usable Y.

## 8. Owner decisions (2026-09-29)

1. **Parallel within one project is allowed.** Independent tasks run in parallel even in the
   same project and working tree; no file-conflict handling or per-project serialization.
   X still orders tasks only for real (semantic) dependencies.
2. **Auto-dispatch.** Ideas are dispatched without a per-idea confirmation; the settle window
   in §5 only prevents dispatching half-written text.
3. **Assistant choice.** If the idea names an assistant, use it; otherwise X chooses. X
   itself runs on whichever assistant runtime is available — no fixed requirement.
4. **One draft document** per owner.

Later decisions (same day): requirements 11–14 — agentic judgement by X, strikethrough
instead of deletion, questions as agent-written text in the document, phone push for
questions.

Strikethrough applies to all deletions, including text X has never seen (owner, same day);
overwrite = strike old + insert new (owner-approved).

Undo/redo restore the previous editor state exactly (undoing just-typed text removes it); if X
had already seen that text, X is told it was withdrawn (owner, same day).

## 9. Research and experiment findings (2026-09-29)

### 9.1 Reference implementations (read from source)

| Project | Worker control | Completion / needs-input | Take | Do not copy |
|---|---|---|---|---|
| multiclaude | tmux set-buffer + paste + Enter; "delivered" = command exit 0 | Daemon nudges every agent every 2 min | Supervisor acts only through CLI commands; "Brownian ratchet" (overlap is fine) matches requirement 11 | Unconfirmed delivery; timer polling instead of events |
| Conductor OSS | Workers in real terminal UIs; dispatcher over ACP | Executor-specific output parsing → `NeedsInput`; 15-min dispatcher heartbeat | Dispatcher turns chat into structured cards (objective, dependencies, acceptance) | Fixed board columns as the input format |
| claude-orchestrator | Headless `claude --print` with stream-json in/out | Process exit + event stream | Structured events are reliable | Invisible sessions — conflicts with requirement 5 |
| OpenConductor (own) | — | — | Deterministic core + model only for semantics; HITL; "real call + data diff" live tests | Go code cannot be lifted into Python directly |

Chosen direction: Y and X stay visible, hosted Corral sessions; **delivery and completion are
confirmed from each assistant's own history files**, never from paste exit codes or screen text
alone; X's decisions land through Corral commands, never parsed from its screen.

### 9.2 E1 — driving hosted assistants (`spikes/web_butler/e1_injection.py`)

Per assistant: start hosted session in a new git folder → first instruction (≈40 s busy) →
mid-turn second instruction → verify from history files and produced files.

| Assistant | Startup gate | Mid-turn instruction | Evidence of delivery in history | Completion signal |
|---|---|---|---|---|
| Claude Code 2.1.284 | Folder trust, default **No, exit** | Absorbed mid-turn | `queue-operation enqueue` (received) and `remove … absorbed_mid_turn` (seen by model); the human text also lands as `attachment/queued_command`, which SessKit now surfaces as a user message (`queue-operation` rows stay ignored so each prompt counts once) | Correct |
| Codex 0.158 | Folder trust, appears **seconds after** the composer looks ready | Held until the current tool call ends ("Messages to be submitted after next tool call"; Esc sends immediately) | Recorded once submitted | **Three "completed" signals inside one turn**, the first while `sleep` still ran |
| Cursor Agent 2026.09 | Workspace trust (`[a]`/Enter) | Not testable: weekly quota exhausted | — | Quota error shown as **completed** |
| Pi | none | Absorbed | Recorded as a user message | Correct — full pass |
| OpenCode 2.0.16 | none; composer placeholder disappears while busy | Absorbed (both files made) | Not found by a text search of its history store — evidence path still to be identified | Correct |
| Kimi Code 2.1.1 | Folder trust, default trust | Not testable: not logged in ("LLM not set, send /login") | — | Shown as **waiting for reply** while unusable |

Findings that change the design or existing Corral behavior:

- **F1 Startup gates** (trust dialogs) block unattended sessions in new folders for Claude,
  Codex, Cursor and Kimi, with different defaults and keys, and can appear late. Gate
  handling must match the exact dialog screen, per assistant.
- **F2 Readiness detection is per assistant.** Corral's `_pane_accepts_input` only recognizes
  an arrow prompt; it rejects Claude Code 2.1 (`❯`), Codex (`›`), OpenCode, Pi and Kimi
  prompts, so `send_turn` (used by EditHere) times out for them. Existing defect.
- **F3 Delivery must be confirmed from history.** Paste + Enter can succeed while the text is
  lost (Codex dialog race) or stays in the composer (Enter before paste settled). Claude
  records mid-turn input as queue-operation rows plus an `attachment/queued_command`
  entry carrying the human text; SessKit now surfaces that entry as a user message
  (2026-09-29 fix, verified on a 3-prompt session showing Your prompts = 3), so mid-turn
  messages — including phone messages — appear in Corral's conversation view.
  SessKit change (landed).
- **F4 A finished turn is not a finished task.** Codex emits several completion ids within a
  single turn; Cursor reports a quota error as completed; an unlogged Kimi shows as waiting. X must verify completion content;
  quota exhaustion and "not logged in" must be distinct availability states that X can act on
  (switch assistant). Of six installed assistants, two were unusable on test day.
- **F5 Mid-turn semantics differ**: absorbed at the next model step (Claude, Pi) vs. held until
  the running tool call ends (Codex). X needs this per assistant to choose wait vs. interrupt.
- **F6 Kimi permission mode.** Kimi 2.1.1 redefined `-y` as "Ask When Needed" (risky actions,
  questions and plans still ask); never-ask is `--auto`. Corral still launches Kimi with `-y`,
  so hosted Kimi sessions can stop for approval. Existing defect; the global CLI-wrapping rule
  that names `-y` is also outdated.

### 9.3 E2 — editor mechanics (`spikes/web_butler/editor/`)

CodeMirror 6 prototype driven in real Chrome through CDP with real key and IME events
(`e2_editor_test.py`, 14/14 pass). Model: the text never loses characters; strike, agent text,
done spans and task anchors are range sets beside the text, serialized to Markdown only on
save (`~~…~~` for strike; an HTML-comment pair for agent text in the spike).

Verified: backspace / forward delete / cut / overwrite-typing / paste-over-selection all
strike instead of delete, with the caret where the owner expects; undo removes a strike;
pinyin composition can correct itself without leaving strikes, and committed text strikes on
backspace; done spans reject edits and deletions; anchors follow inserts and strikes before
them; five X insertions above the caret while the owner types continuously lose or misplace
no characters; Markdown round trip is exact; project hints render; clicking a done span opens
the result popover (`results/e2.png`, `results/e2-popover.png`).

Findings:
- **F7 Adjacent strikes must merge** (each backspace creates its own range) — fixed in the
  spike by merging on read/serialize.
- **F8 Projects are components, the owner names products.** Corral lists git roots
  (`Corral/cli`, `Notely/web`); the owner writes `Corral`, `Notely`. Hints match both; X needs
  the product → components map.
- **F9 Clicking done text places a caret** inside the locked span; the product should open
  the popover without a caret. The done span can be split into several DOM fragments when
  a project hint overlaps it, so the popover must anchor to the whole task's final fragment,
  not the first word that happened to receive the click.
  Clicking the check icon itself was still ignored by the editor (CodeMirror widgets ignore
  events by default), so the browser placed a caret and no popover opened; the widget must
  opt in to editor event handling. Fixed and re-verified in the spike (icon click and text
  click both open the result with no caret).
- Undo/redo is exempt from strike conversion — owner decision, §8.

Still unverified: T9 only covered typing and strikethrough **before** an anchor. A cut in
this editor strikes the source and leaves its characters in place. A later paste creates
a second copy, so ordinary range mapping may leave the task anchor on the struck source
instead of following the move. This needs a separate cut/paste experiment before the anchor
guarantee in §6.1 can be accepted.

### 9.4 E3 — X judgement (`spikes/web_butler/x_eval/`)

The first 15-case run with an explicitly selected **Luna, medium** model returned 15 valid
responses; 13 met their case checks and copied anchor text exactly. The earlier Claude CLI
run was interrupted by a session quota: only 9 responses were valid, so its apparent 8/15
score must not be treated as a judgement result. E3 is one-shot; it does not yet test one X
session across many edits or ledger reconstruction after compaction.

The two failures require different treatment. In the "owner answers in text" case, the
fixture said only "that login bug"; X had a legitimate reason to ask for the symptom even
after learning the project. That check was over-specific, so the case is being revised to
give X a concrete symptom, with a separate ambiguous case where asking remains valid. When
Cursor's quota was exhausted, X tried to steer Cursor for a status report. A disabled
runtime cannot receive that request. The command vocabulary needs a same-task **reassign**
action carrying a new usable assistant and a brief based on verified ledger/session/workspace
evidence. In the first targeted rerun X chose `reassign` correctly but placed its reason
outside the action object, yielding invalid JSON. Therefore X's command boundary needs an
enforced structured output schema plus validation, not prompt wording alone. This correction
is being evaluated before freezing the contract; it is not product code yet.

With the schema, the 16-case rerun passed all **mechanical** checks, but manual review found
two checks too loose: X asked about a visibly unfinished fragment, and later asked which
component owned a bug after the owner had already said "web". The evaluation must check
the *content* of questions and require silence for incomplete fragments. A numerical pass
count alone is insufficient for design acceptance.

After tightening those checks, the next full run passed **15/16**. In the typo-strike case,
X constructed an anchor by skipping over struck text, so the quote did not occur contiguously
in the current document. It also sent `update_task` to a running task, although that action
is reserved for work not yet started. X needs a `reanchor` action usable during execution;
the command boundary must validate exact current-document spans and legal actions for each
ledger state. This is a contract risk, not merely a prompt-quality score.

### 9.5 E4 — trigger-timing replay (`spikes/web_butler/trigger_replay.py`)

Six labeled, **synthetic** typing/event traces were replayed against four idle/running delay
pairs with a 20-second X round. At 3 s idle / 1.5 s running, 3 X rounds fired before the
labeled ideas were complete; at 8 s / 3 s, 1 did; at 12 s / 3 s, none did, but the longer
wait may defer dispatch. An explicit cursor leave sent a settled block immediately. Edits
and a worker event arriving while X was busy merged into one following round; whitespace
only did not wake X, while a strikethrough did. The 8 s / 3 s setting is a candidate, not
an accepted UX threshold: a 10-second thinking pause still woke X early. X's semantic
"half-written" check remains necessary, and real typing traces are needed before freezing
the delays.

### 9.6 E5 — moving a task anchor by cut/paste (`spikes/web_butler/editor/e5_anchor_move.py`)

The real-browser E5 probe cut an anchored sentence and pasted it elsewhere in the same
document. The clipboard paste succeeded; the source remained visible and struck, as required.
The RangeSet anchor stayed at source offset 7 instead of moving to the pasted copy at offset
29. Therefore ordinary CodeMirror position mapping is insufficient for §6.1's move guarantee.

The next spike will record the source anchor IDs/relative offsets on `cut`, verify that the
next `paste` carries the same text, and atomically move those IDs to the pasted instance
while preserving the struck source. This proof covers an unambiguous same-editor cut/paste.
Copying between apps, repeated identical text, multiple pending cuts, drag/drop and
undo/redo need separate acceptance; custom clipboard formats are optional in browsers, so
they cannot be the sole identity channel. [CodeMirror event/RangeSet API](https://codemirror.net/docs/ref/),
[Clipboard API specification](https://www.w3.org/TR/clipboard-apis/).

### 9.7 Interactive visual draft (`spikes/web_butler/editor/preview.html`)

The visual draft uses the same CodeMirror prototype as E2. Its desktop and 390 px views were
opened and inspected in Chrome. It shows one Markdown idea document, project hints, struck
text, an X-written question, a done span, a status summary, and a result popover. Clicking a
done span now leaves the caret outside the locked text and anchors the popover to its final
DOM fragment even when a project hint splits the span. The preview is for owner review; its
layout and copy are not frozen product decisions.

Preview captures: [desktop](assets/web-task-butler-preview.png),
[result popover](assets/web-task-butler-result-preview.png),
[390 px layout](assets/web-task-butler-mobile-preview.png).

### 9.8 Corral / SessKit change list derived so far

1. Session-control layer shared by the phone daemon, the web service and X (create, send with
   confirmed delivery, interrupt, observe), extracted from `SessionHub`.
2. Per-assistant adapters for startup gates, readiness and mid-turn submission.
3. SessKit: surface mid-turn user input (Claude queue records — landed 2026-09-29:
   `attachment/queued_command` prompts surface as user messages; `queue-operation`
   rows ignored); one completion per turn
   (Codex); quota/limit as its own status (Cursor, others).
4. Kimi launch flag `--auto` (F6; owner-approved; global CLI-wrapping rule already updated).
5. Persistent same-task assistant reassignment after quota/login failure, with prior progress
   gathered from verified session/workspace evidence rather than a call to the unavailable Y.

## 10. References

- CodeMirror decorations / atomic ranges: https://codemirror.net/examples/decoration/ ,
  https://codemirror.net/docs/ref/
- CM6 source-preserving live preview: https://github.com/kenforthewin/atomic-editor
- Tiptap Markdown round-trip limits: https://tiptap.dev/docs/editor/markdown
- Conductor OSS (Markdown board → tmux agents; relies on explicit board columns, not free
  text): https://conductross.com/
