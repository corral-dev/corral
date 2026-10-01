# Web Task Butler (draft-driven dispatch) — requirements and analysis

> Status: **analysis draft; owner decisions recorded, design freeze pending** (2026-09-29). No product code yet.
> Read before planning, implementing, or reviewing the local web idea editor, the
> coordinator agent, the worker agents, or Corral changes made for them.
> Items marked *Proposed* are unadopted suggestions; §8 records owner decisions.

## 1. Owner requirements (2026-09-29)

1. Use Corral's project discovery to know every local project.
2. A free-form local web **Markdown** editor. Text that closely matches a project name is
   rendered in a distinct style as the user types.
3. The owner keeps adding idea drafts continuously and loosely.
4. One **coordinator** agent always knows every task, its state and its owner. The coordinator turns the
   drafts into tasks, detects strong dependencies, and runs everything else in parallel.
   The agents that carry out the tasks are **workers**.
5. Coordinator and worker sessions are created and driven through Corral; Corral will need changes.
6. When a task is done, the coordinator finds the words/sentence in the owner's original text that best
   match it: a green circular check icon at its top-right, the text turns green and becomes
   read-only, and clicking it opens a result popover.
7. If the owner edits text after dispatch, the coordinator updates the running worker with new instructions in
   near real time.
8. Deciding **when** to prompt the coordinator is the central design problem. Debounce is required. The coordinator stays one session.
9. Agents (the coordinator or a worker) may ask the owner for details or decisions. The editor design is the key
   to the user experience.

10. The owner judges this project deceptively simple: every detail matters, and it requires
    deep investigation, testing, and careful design **before** implementation.
11. **The product is agentic.** Do not design it with conventional rule-based software
    thinking: whenever a situation calls for judgement (duplicates, failures, retries,
    deleted ideas, dependencies, which assistant), the coordinator decides. The system supplies facts,
    tools and guarantees, not hard-coded workflow policies.
12. **Text the coordinator has seen is never deleted; text the coordinator has not seen is.** Deleting text that has
    already been submitted to the coordinator marks it with a strikethrough and it stays visible; the coordinator is
    told which text was struck and decides what to do. Deleting text that has not been
    submitted to the coordinator yet removes it outright, as in any editor (owner, 2026-09-29, after trying
    the demo; supersedes "strike everything").
13. **Agents talk inside the document.** When the coordinator needs details or a decision, it inserts its
    own question as text near the related idea, visibly marked as agent-written. The owner
    answers by writing in the editor whenever convenient. No separate question cards.
14. Questions awaiting the owner are also pushed to the phone.
15. SessKit and OpenConductor may be changed, optimized, used as references, and have modules
    extracted or updated for reuse.
16. Use an explicitly selected low-cost model, such as Haiku or Luna, when calling real agents
    for debugging and risk experiments; do not silently use the CLI's default model.

17. **Named roles with stated duties** (owner, 2026-09-29). The owner's first description
    used X and Y only as placeholders; those letters are not used anywhere (docs, prompts,
    interface, code). The roles are:
    - **Coordinator** (协调员), one per owner, one long-lived session. Reads the idea
      document, turns settled ideas into tasks, picks the project and the assistant, orders
      only real dependencies, sends and updates tasks, answers workers from the document when
      it can, asks the owner inside the document when it cannot, and marks a task done only
      on verified evidence. It acts only through validated Corral commands and never edits
      project code itself.
    - **Worker** (执行者), one session per task, on whichever assistant the coordinator picks.
      Does exactly one task in one project, follows the owner's constraints in the brief,
      sends questions to the coordinator rather than guessing, and ends with what changed
      and how it was verified.
    Each role's standing prompt states who it is, who it works for, and its duties and
    limits; the per-task brief comes on top of the worker's standing prompt. Drafts:
    `spikes/web_butler/coordinator_eval/coordinator_prompt.md` and `worker_prompt.md`.
18. **The document lets the owner step away** (owner, 2026-10-01). The owner noticed the
    product matches a common anti-procrastination advice: before leaving, take a two-minute
    checkpoint (where was I, what new ideas do I have, what is the first step next time),
    then get up. "Leaving the computer" is a metaphor for not staying glued to the screen,
    not powering it off; agents keep running. The product goal includes: the owner can hand
    off their current thinking and walk away without fear of losing the thread, and
    coming back has an obvious first step. The owner left the follow-up design to the agent
    ("你考虑一下吧"); the proposals below are not yet confirmed.

### Delivery approach (*Proposed*, follows requirement 10)

1. **Research**: study existing orchestrators (Conductor OSS, claude-orchestrator,
   Multiclaude, Vibe Kanban, Nimbalyst, Claude Code agent teams) from source — how they
   represent tasks, detect completion, surface questions, and handle mid-task changes.
2. **Risk experiments**, each with a pass bar, before any product code:
   injection reliability per assistant (mid-turn, confirmed delivery); task-level completion
   accuracy; anchor survival under move/paste/undo; coordinator judgement quality on a corpus of messy
   drafts (task split, dependencies, steer vs. new task); trigger timing replayed from
   recorded typing; editor cursor stability under background decoration updates; coordinator session
   behaviour over hours (compaction, restart from ledger).
3. **Design freeze**: every §6.1 guarantee has an acceptance check and every §6.2 judgement
   an evaluation case; UI prototype images reviewed by the owner.
4. **Test layers**: deterministic tests for ledger/anchors/triggers; recorded-replay tests
   for typing → coordinator rounds; an evaluation set for coordinator decisions; end-to-end runs with real
   assistants on disposable projects; browser screenshots/recordings for the editor.

### Stepping away and coming back (*Proposed*, follows requirement 18)

The checkpoint advice has three questions; only the second one is covered by the document so far.

| Checkpoint question | Today's design | Proposal |
|---|---|---|
| Where was I? | Not covered | Filled automatically. The ledger and session states already know what is running, done, failed or waiting; the coordinator words it. The owner does not reconstruct it. |
| What new ideas do I have? | The idea document itself | Unchanged |
| First step next time? | Partly: green done text and amber questions are scattered through the document | One "pick up here" summary at the top on return: questions awaiting the owner first, then results to review, then what is still running. |

- **Leaving should take seconds, not two minutes.** A single "step away" action may ask the
  coordinator for a checkpoint note; the owner only adds new thoughts. The action must not
  be required: closing the tab already loses nothing (§6.1 "never lose the draft").
- **Phone push must not pull the owner back** (refines requirement 14). If every question is
  pushed, the owner has left the screen but not the work. Only questions that block all
  useful progress should be pushed; questions that can wait stay in the document for the
  "pick up here" summary. Requirement 11 applies: the coordinator judges whether a question
  is blocking and marks it, the system only routes by that mark. Needs §6.2 evaluation
  cases (blocking vs. can-wait) and a check that most questions are not pushed.

## 2. Existing capability (verified in source, 2026-09-29)

| Need | Present today | Gap |
|---|---|---|
| Project list and name matching | `projects.discover()` / `match_projects()` (session cwds ∪ git roots) | Aliases; common-word names (`Go`, `Mirror`, `Research`, `planet`) cause false matches |
| Create a hosted session | `SessionHub.new_session()` in `remote/sessions.py`, tmux keepalive backend | Lives in the phone daemon; needs a shared session-control layer usable by a local web service and by the coordinator |
| Send text into a running session | `SessionHub.send_text()` (tmux paste + Enter, phone path always steers) | Delivery can be partial (`PartialInjectionError`); the coordinator needs a confirmed-delivery result |
| Know a turn finished / was interrupted | SessKit `completion_id` (see `AGENT_COMPLETION_NOTIFICATIONS_DESIGN.md`), attention `waiting` | A finished *turn* is not a finished *task*; needs the coordinator's judgement plus the worker's final summary |
| Machine-readable queries for agents | `agent_api.py` (read-only list/search/show/export) | No write commands (create task, dispatch, steer, ask, mark done) |
| Local web service | None (`websockets` is already a dependency) | New |

## 3. Core architecture (*Proposed*)

- **Deterministic ledger, LLM at the edges.** Corral owns a task ledger (task, state, owner
  session, dependencies, text anchors, questions, results). The coordinator never holds the truth in its
  context; each coordinator round receives the ledger snapshot plus the change set, and acts only
  through validated Corral commands. The coordinator can therefore be restarted or compacted without loss.
  (Same lesson as OpenConductor's "deterministic core + model only for semantics".)
- **The document is shared by owner and the coordinator.** Owner text, struck-out text and coordinator-written text
  all live in the Markdown file (strikethrough as Markdown strikethrough; coordinator text carries an
  authorship marker so the file alone shows who wrote what — exact syntax fixed at design
  freeze). Task anchors, states and results live in the ledger and are rendered as view-only
  decorations.
- **Mechanics vs. judgement.** The system guarantees mechanics (anchors follow text, typing
  is never disturbed, nothing is lost, delivery is confirmed, done text is locked). Every
  semantic choice belongs to the coordinator's instructions, not to code.
- **Storage** under `~/.config/corral/`; every ledger mutation records actor
  (owner / coordinator / worker / system), action, target and time in an append-only audit log.
- **Local-only web service**: bind loopback, random access token, origin check. It can start
  agents that skip permission prompts, so exposure equals remote code execution.

## 4. Editor (*Proposed*)

- **CodeMirror 6**, not Milkdown/Tiptap: those rewrite Markdown through a ProseMirror model,
  while CM6 keeps the text exact and supports mark/widget decorations, `atomicRanges`, and
  `changeFilter` for read-only ranges.
- Visual states on anchored text: waiting-to-dispatch (dotted underline, cancellable),
  running (subtle gutter dot), waiting on owner (amber), failed (red), done (green text +
  green check, locked; click opens result popover with summary and a link to the session).
- Done text is read-only. New text written beside it is simply new input for the coordinator.
- **Deleting depends on whether the coordinator has seen the text.** The editor tracks, per character,
  which text has been submitted to the coordinator. Delete, backspace, cut and overwrite remove unseen
  text outright and turn seen text into strikethrough (overwriting seen text = old text
  struck + new text inserted). A selection spanning both is split: the seen part is struck
  and the unseen part is removed. New characters typed inside a seen sentence are unseen
  until the next round submits them. The coordinator's own text counts as seen.
- **The coordinator's questions are text in the document**, inserted near the related idea in a distinct
  agent style. The owner answers by writing anywhere nearby; the coordinator reads the answer on its next
  round. A top-bar count and a keyboard jump go to the next unanswered coordinator question. Workers'
  questions go to the coordinator first; the coordinator answers from the document when it can.

## 5. When the coordinator is prompted (*Proposed*)

- Unit of change is a **block** (paragraph / list item), never a keystroke.
- A block is *settled* when the cursor leaves it, Enter starts a new block, or it has been
  idle ~8 s. Only settled blocks produce changes; whitespace/format-only diffs are dropped.
- Blocks linked to a **running** worker use a shorter delay, because the worker is working in a stale
  direction.
- **One coordinator round in flight.** Changes arriving during a round are merged into the next one.
- Worker events (turn completed, waiting for an answer, failed, quota exhausted) also wake the coordinator.
- Each round sends: changed blocks (before/after), linked task ids, ledger snapshot.
- New ideas get a short visible dispatch countdown; continued editing restarts it, so
  half-written ideas are not dispatched.

## 6. Edge cases

Split by requirement 11: the system guarantees mechanics; the coordinator judges everything else.
Each item still needs an acceptance check (mechanics) or an evaluation case (coordinator judgement).

### 6.1 System guarantees (mechanics)

- **Anchors follow text** through typing, moves, cut/paste, merges and undo/redo. Identical
  text removed and re-inserted within one settle window is reported to the coordinator as a move.
- **Undo/redo** changes text only; it never undoes a dispatch or unlocks done text.
- **Strikethrough, not deletion**, for text the coordinator has seen (§4); unseen text is deleted
  normally and never reaches the coordinator. The change sent to the coordinator says which text was struck. Whether struck text is a typo fix or a withdrawn idea is the coordinator's judgement.
- **Completion marking**: the coordinator names the words to mark; the system verifies they exist in the
  current text and otherwise uses the task's stored anchor range. It never guesses a span.
  A span shared by several tasks turns green only when all of them are done.
- **Project highlight** is a hint only: whole-word matches, short/common names matched
  case-sensitively; clicking a highlight offers "not a project", remembered for that phrase.
- **Typing always wins**: background updates never move the cursor or re-layout the block
  being edited; its decorations and coordinator insertions wait until it settles, and the coordinator never inserts
  inside the block the owner is editing.
- **Never lose the draft**: saved locally in the page and in the service; reconciled by
  version after restarts; only one tab edits, others are read-only with an "edit here"
  takeover.
- **Restarts**: the ledger is persisted; after sleep or restart the system reconciles the
  ledger with live sessions and tells the coordinator; no action is replayed twice.
- **Confirmed delivery** of every message the coordinator sends to the worker; failed delivery is reported to the coordinator.
- **Phone push** for coordinator questions that await the owner; answers are written in the editor.
- **Result popover** shows the coordinator's plain-language summary, project, verification evidence,
  assistant used, time taken, and "open session in Corral".
- **Worker sessions** appear in Corral's normal session list, grouped under the butler.

### 6.2 The coordinator decides (judgement, covered by the coordinator's instructions and evaluation set)

- How text maps to tasks: one sentence → several tasks, scattered sentences → one task,
  later text amending earlier ideas, repeated ideas (including ideas already done).
- What struck-out text means for its task (stop, keep, adjust) — requirement 12.
- Edits to dispatched text: update, steer, or stop and restart the worker.
- Which project an idea targets, aliases, several projects per idea, and a project that does
  not exist yet (the coordinator asks in the document when it needs the owner, e.g. a new project's name).
- Dependencies, including ones discovered after tasks started.
- Failures, retries, quota exhaustion and switching assistants.
- Whether a worker's finished turn is an accepted completion (the worker must report what changed and how
  it was verified; the coordinator may send it back).
- Whether the owner's nearby writing answers an open question; whether to ask at all.

## 7. Risks

- Steering interrupts a running worker; the coordinator must choose steer vs. queue vs. cancel-and-restart.
- Coordinator token cost grows with edit frequency; block settling and merging bound it.
- Project-name false positives; ambiguity must be resolvable by the owner.
- A quota-exhausted or logged-out worker cannot be asked for a status report. Corral must retain
  the last verified session/workspace state so the coordinator can move the task to another usable worker.

## 8. Owner decisions (2026-09-29)

1. **Parallel within one project is allowed.** Independent tasks run in parallel even in the
   same project and working tree; no file-conflict handling or per-project serialization.
   The coordinator still orders tasks only for real (semantic) dependencies.
2. **Auto-dispatch.** Ideas are dispatched without a per-idea confirmation; the settle window
   in §5 only prevents dispatching half-written text.
3. **Assistant choice.** If the idea names an assistant, use it; otherwise the coordinator chooses. The coordinator
   itself runs on whichever assistant runtime is available — no fixed requirement.
4. **One draft document** per owner.
5. **Kimi is out of scope** (owner, 2026-09-30). The coordinator and workers never run on
   Kimi, and no butler work targets Kimi's signals. SessKit made the same call: Kimi stays
   on its legacy parser and is left out of the unified activity model. Kimi findings below
   (F4, F6) are kept as history; Corral's existing Kimi support is unchanged.

Later decisions (same day): requirements 11–14 (agentic judgement, strikethrough,
in-document agent questions, phone push). Strikethrough first covered all deletions,
including text the coordinator had never seen; after trying the demo the owner narrowed it (2026-09-29):
only text already submitted to the coordinator is struck, unseen text is deleted outright; overwrite of
seen text = strike old + insert new. Undo/redo
restore the editor state exactly (just-typed text is removed); text the coordinator already saw is
reported to the coordinator as withdrawn (owner, same day).

## 9. Research and experiment findings (2026-09-29)

### 9.1 Reference implementations (read from source)

| Project | Worker control | Completion / needs-input | Take | Do not copy |
|---|---|---|---|---|
| multiclaude | tmux set-buffer + paste + Enter; "delivered" = command exit 0 | Daemon nudges every agent every 2 min | Supervisor acts only through CLI commands; "Brownian ratchet" (overlap is fine) matches requirement 11 | Unconfirmed delivery; timer polling instead of events |
| Conductor OSS | Workers in real terminal UIs; dispatcher over ACP | Executor-specific output parsing → `NeedsInput`; 15-min dispatcher heartbeat | Dispatcher turns chat into structured cards (objective, dependencies, acceptance) | Fixed board columns as the input format |
| claude-orchestrator | Headless `claude --print` with stream-json in/out | Process exit + event stream | Structured events are reliable | Invisible sessions — conflicts with requirement 5 |
| OpenConductor (own) | — | — | Deterministic core + model only for semantics; HITL; "real call + data diff" live tests | Go code cannot be lifted into Python directly |

Chosen direction: the coordinator and workers stay visible, hosted Corral sessions; **delivery and completion are
confirmed from each assistant's own history files**, never from paste exit codes or screen text
alone; the coordinator's decisions land through Corral commands, never parsed from its screen.

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
  single turn; Cursor reports a quota error as completed; an unlogged Kimi shows as waiting. The coordinator must verify completion content;
  quota exhaustion and "not logged in" must be distinct availability states that the coordinator can act on
  (switch assistant). Of six installed assistants, two were unusable on test day.
- **F5 Mid-turn semantics differ**: absorbed at the next model step (Claude, Pi) vs. held until
  the running tool call ends (Codex). The coordinator needs this per assistant to choose wait vs. interrupt.
- **F6 Kimi permission mode.** Kimi 2.1.1 redefined `-y` as "Ask When Needed" (risky actions,
  questions and plans still ask); never-ask is `--auto`. Fixed in Corral v0.24.228 (hosted
  Kimi now shows `Never Ask`); the global CLI-wrapping rule was updated the same day.

### 9.3 E2 — editor mechanics (`spikes/web_butler/editor/`)

CodeMirror 6 prototype driven in real Chrome through CDP with real key and IME events
(`e2_editor_test.py`, 14/14 pass at first). Model: text the coordinator has seen never loses characters; strike, agent text,
done spans and task anchors are range sets beside the text, serialized to Markdown only on
save (`~~…~~` for strike; an HTML-comment pair for agent text in the spike).

Verified: backspace / forward delete / cut / overwrite-typing / paste-over-selection all
strike instead of delete, with the caret where the owner expects; undo removes a strike;
pinyin composition can correct itself without leaving strikes, and committed text strikes on
backspace; done spans reject edits and deletions; anchors follow inserts and strikes before
them; five coordinator insertions above the caret while the owner types continuously lose or misplace
no characters; Markdown round trip is exact; project hints render; clicking a done span opens
the result popover (`results/e2.png`, `results/e2-popover.png`).

Seen/unseen deletion (added 2026-09-29 after the owner's demo trial; now 23/23 pass):
- Typed text that the coordinator has not seen is deleted outright (T15), and undo brings it back (T18).
- A selection over seen and unseen text strikes only the seen part (T16).
- Characters typed inside a seen sentence stay unseen, so deleting them erases them while
  the seen characters around them strike (T17).
- Overwriting unseen text replaces it; overwriting seen text strikes it (T19).
- Backspace next to an already-struck run jumps over the whole run instead of stepping
  through it one character at a time (T20).
- In the demo, a line becomes seen the moment the coordinator's round picks it up, and the seen ranges
  survive a reload.

Findings:
- **F7 Adjacent strikes must merge** (each backspace creates its own range) — fixed in the
  spike by merging on read/serialize.
- **F8 Projects are components, the owner names products.** Corral lists git roots
  (`Corral/cli`, `Notely/web`); the owner writes `Corral`, `Notely`. Hints match both; the coordinator needs
  the product → components map.
- **F9 Clicking done text places a caret** inside the locked span; the product must open
  the popover with no caret, anchored to the whole task's final fragment (a project hint
  can split the span into several DOM fragments). The check-icon click was ignored because
  CodeMirror widgets ignore events by default; the widget must opt in to editor event
  handling. Fixed and re-verified in the spike (icon and text clicks both open the result).
- Undo/redo is exempt from strike conversion — owner decision, §8.
- **F12 Undo could remove text the owner did not write, and text already done.** Found by E2
  T21/T22.
  - **Cause 1:** system edits (document load, coordinator insertions) were recorded in the
    owner's undo history, so undo could delete a coordinator question.
  - **Cause 2:** CodeMirror dispatches undo/redo with `filter: false`, bypassing the done lock,
    so undo could erase text already marked done.
  - **Fix in the spike:** system edits are kept out of history. Undo/redo (keys and the
    browser's Edit menu) is built first and dropped if it would touch a done span.

Still unverified: T9 only covered typing and strikethrough **before** an anchor. A cut in
this editor strikes the source and leaves its characters in place. A later paste creates
a second copy, so ordinary range mapping may leave the task anchor on the struck source
instead of following the move. This needs a separate cut/paste experiment before the anchor
guarantee in §6.1 can be accepted.

### 9.4 E3 — coordinator judgement (`spikes/web_butler/coordinator_eval/`)

The first 15-case run (**Luna, medium**) returned 15 valid responses; 13 met case checks
with exact anchor quotes. The earlier Claude CLI run was quota-interrupted (9 valid), so its
apparent 8/15 must not be treated as a judgement result. E3 is one-shot; it does not yet test
one coordinator session across many edits or ledger reconstruction after compaction.

The two failures need different treatment. In the "owner answers in text" case the check was
over-specific ("that login bug" legitimately needed a symptom question), so the case is being
revised to give a concrete symptom plus a separate ambiguous case where asking stays valid.
When Cursor's quota was exhausted, the coordinator tried to steer an unusable runtime; the command
vocabulary needs a same-task **reassign** action (new assistant + brief from verified
ledger/session/workspace evidence). The first rerun chose `reassign` but placed its reason
outside the action object (invalid JSON), so the coordinator's command boundary needs an enforced
structured-output schema plus validation, not prompt wording alone — under evaluation,
not product code yet.

With the schema, the 16-case rerun passed all **mechanical** checks, but manual review found
two checks too loose: the coordinator asked about a visibly unfinished fragment, and later asked which
component owned a bug after the owner had already said "web". The evaluation must check
the *content* of questions and require silence for incomplete fragments. A numerical pass
count alone is insufficient for design acceptance.

After tightening those checks, the next full run passed **15/16**. In the typo-strike case,
the coordinator constructed an anchor by skipping over struck text, so the quote did not occur contiguously
in the current document. It also sent `update_task` to a running task, although that action
is reserved for work not yet started. The coordinator needs a `reanchor` action usable during execution;
the command boundary must validate exact current-document spans and legal actions for each
ledger state. This is a contract risk, not merely a prompt-quality score.

Command boundary (`coordinator_eval/contract.py`, built 2026-09-29). Every action is checked before
it runs:
- Quotes must be contiguous spans of one current block; struck characters keep their positions.
- Each action is legal only in certain ledger states: `update_task` when queued or blocked,
  `reanchor` and `stop` up to running, `steer` and `mark_done` when running,
  `reopen_as_followup` when done.
- Ids, dependencies and components must exist, and assistants must be usable.

Rejections go back to the coordinator once with reasons (`run_eval.py`).

Full run with the boundary (2026-09-29, Claude Haiku one-shot, two complete runs):
- **15/16 pass in both runs**, with no transport errors and every quote exact.
- Latency: median ~12 s, maximum 58.6 s (a retried case).
- **S5 (typo strike):** Haiku's first answer both times joined live text across a struck
  character. The boundary rejected it and the one retry produced a legal span both times.
  The boundary plus one retry is the mechanism that makes quote exactness hold.
- **S16 (vague bug, product now known) fails in both runs, identically.** The coordinator dispatches instead
  of asking what actually happens. Cause: judging rule 1 ("ask only for a concrete missing fact
  that blocks…") licenses acting, and Haiku resolves it against rule 11 in favour of action.
  This is a prompt defect to fix, not a boundary defect.
- Codex Luna could not be scored (account usage limit).

Follow-up run (same day, rule 1 now says a bug report without an observable symptom is not
actionable, so asking for the symptom is a blocking fact):
- S16 passes in both runs, and S4 and S12 still pass.
- **S14 (worker says only "改好了", i.e. "done") is unreliable.** It failed both full runs and
  passed 2 of 3 targeted reruns: the coordinator marks the task done without verification evidence.
- **Evidence quote for `mark_done` (tested the same day).** `mark_done` must carry an
  `evidence` quote copied exactly from the worker's report. The boundary rejects a missing or
  inexact quote, and rule 10 tells the coordinator to quote the report's verification
  sentence or else send the worker back to verify.
  - **Result:** S14 went from 2/5 to **7/7** (two full runs plus five targeted reruns); every
    run now asks the worker what changed and how it was checked. Full runs: 16/16 and 15/16.
  - **Limit:** the boundary checks exactness only. `evidence: "改好了。"` would still pass,
    because it is in the report. Judging whether a sentence describes a check stays with the
    coordinator. The improvement comes from making the coordinator name its evidence, and the
    sample is small.
  - **Remaining flaky case:** S16 (vague bug), which passed 3 of 4 full runs.
- After the role rename (coordinator prompt now opens with identity, duties and limits;
  worker prompt added): **15/16**. S14 passes; S16 failed again (3 of 5 full runs since
  the rule-1 fix). S16 is the open judgement weakness.
- **S16 diagnosis and fix** (same day). The failing runs' notes show the coordinator treating
  "the project is now known" as "the request is now actionable"; the project and the symptom
  are two separate missing facts.
  - **Change:** rule 1 now adds that naming the project does not state what happens, so the
    symptom must still be asked.
  - **Measured:** S16 5/6 and S12 3/3. The alternative wording on rule 11 reached 4/6 and was
    dropped.
  - **Pending:** a full 16-scenario regression run is scheduled for after the model's usage
    limit resets.

Long-running coordinator (E6, `long_run.py`, 2026-09-29, Claude Haiku):
- **Setup:** one persistent session over a 26-round scripted timeline. The owner adds ideas,
  fixes a typo by striking, withdraws an idea, answers a question in plain text, receives
  worker events (verified and unverified reports, quota exhausted), follows up on a done task,
  and leaves a half-written line. Round 1 carries the full prompt; later rounds only carry
  the payload.
- **26/26 in both the continuous run and a run restarted at round 13** from the ledger and
  document alone. The restart kept the same final state (10 tasks) and reduced input tokens
  by ~31%.
- **Evidence:** this is the evidence behind "the ledger is the memory": restarting or compacting
  the coordinator is a cost lever, not a behaviour risk.
- **Context growth:** from 14.7k to 82.7k tokens over 26 rounds, with the per-round increase
  widening from ~1.6k to ~3.8k because the ledger and document in each payload grow.
- **Latency:** median 14 s continuous, 11 s with the restart.
- **Product implication:** send finished tasks in compact form and restart the coordinator
  periodically.
- **Only correction:** one boundary rejection per run, at the typo strike. The coordinator
  copied `~~` markers into a quote, and the retry fixed it (same class as S5).

- **F10 Tool catalogs cost context in every session.** A one-shot Claude call carried ~250k
  tokens of context, over Haiku's 200k limit, because every configured MCP server's tool
  catalog loads into every new session; one stock-data server alone was ~230k. The owner
  removed it from the canonical MCP config (2026-09-29); a blank Claude session now starts at
  ~18k (~13k with MCP disabled). The coordinator must be launched with only the tools dispatching needs.
  Measurement method: shared MCP clients guide (agentsync `docs/MCP_CLIENTS_GUIDE.md`).
- **F11 The coordinator inherits the owner's global rules.** Run as an ordinary Claude session, the coordinator wrote the worker
  briefs that already carried the owner's standing requirements (e.g. verify iOS on the paired
  device). Desirable: keep global instructions loaded for the coordinator and workers.

### 9.5 E4 — trigger-timing replay (`spikes/web_butler/trigger_replay.py`)

Six labeled, **synthetic** typing/event traces were replayed against four idle/running delay
pairs with a 20-second coordinator round. At 3 s idle / 1.5 s running, 3 coordinator rounds fired before the
labeled ideas were complete; at 8 s / 3 s, 1 did; at 12 s / 3 s, none did, but the longer
wait may defer dispatch. An explicit cursor leave sent a settled block immediately. Edits
and a worker event arriving while the coordinator was busy merged into one following round; whitespace
only did not wake the coordinator, while a strikethrough did. The 8 s / 3 s setting is a candidate, not
an accepted UX threshold: a 10-second thinking pause still woke the coordinator early. The coordinator's semantic
"half-written" check remains necessary, and real typing traces are needed before freezing
the delays.

### 9.6 E5 — moving a task anchor by cut/paste (`spikes/web_butler/editor/e5_anchor_move.py`)

The real-browser E5 probe cut an anchored sentence and pasted it elsewhere in the same
document. The clipboard paste succeeded; the source remained visible and struck, as required.
The RangeSet anchor stayed at source offset 7 instead of moving to the pasted copy at offset
29. Therefore ordinary CodeMirror position mapping is insufficient for §6.1's move guarantee.

Fix, verified (E5 PASS, E2 regression 15/15): on `cut` the editor records which anchors lie
inside the selection and their relative offsets; a following `paste` whose clipboard text is
identical moves those anchors to the pasted copy **inside the paste transaction itself**, with
an inverse move registered for history. The struck source stays. A first version moved the
anchor in a separate transaction; undo then restored the text but left the anchor collapsed
on an empty span — so the move must be part of the same transaction as the paste. Undo returns
text and anchor to the source; redo moves both back. Scope: same-editor cut → paste of
identical text; copy/paste, cross-tab and edited clipboard text are not moves (the anchor
stays; the coordinator sees the pasted text as new writing and decides).
Copying between apps, repeated identical text, multiple pending cuts, drag/drop and
undo/redo need separate acceptance; custom clipboard formats are optional in browsers, so
they cannot be the sole identity channel. [CodeMirror event/RangeSet API](https://codemirror.net/docs/ref/),
[Clipboard API specification](https://www.w3.org/TR/clipboard-apis/).

### 9.7 Interactive demo (`spikes/web_butler/editor/preview.html`, `src/demo.js`)

The demo runs the real editor prototype with a rule-based stand-in for the coordinator, so the whole
loop can be tried without a backend. It was checked in Chrome at 1440 px and 390 px, in
light and dark mode, by typing into the page. Screenshots and simulated behaviour are for
owner review; layout and copy are not frozen product decisions.

Layout and states (proposed for the product):

- One column of writing (max 720 px) with a **margin** on the right. Each task shows a
  margin note beside the first line of its text: a status dot, the state (Queued, Working,
  Needs your answer, Failed, Stopped, Done), and `assistant · component`. Notes stack
  downward when they would overlap. Hovering a note highlights its text. Clicking a note
  scrolls to its text, or opens the result for a done task.
- Anchored text carries the same state in the text itself: dotted underline (queued),
  solid tinted underline (working), amber (needs answer), wavy red (failed), none
  (stopped, the text is struck anyway), green with a check (done).
- The coordinator's questions are violet text with a small `Coordinator` label. Answered questions fade.
- The top bar has only live facts: how many tasks are working, a question count that jumps
  to the next open question (hidden at zero), and whether the coordinator has read everything
  ("Coordinator is reading" pulses while a round is pending). A line the coordinator has not read yet shows a
  pulsing dashed dot in the margin, where its status will appear once read.
- The result card has a close button, closes on Esc, outside click and scroll, and shows
  the idea, the result, and `assistant · component · time`.
- Below 860 px the margin shrinks to dots; tapping a dot opens the same card with the
  state and details.
- Markdown stays source text but is styled: headings are sized, and syntax marks such as
  `#` and `**` are dimmed.
- The editor keymap drops shortcuts that move text (Option+Up/Down move-line and
  Ctrl+T transpose). Under strike-instead-of-delete they would leave a struck copy
  behind (E2 check T14).

The simulated coordinator in the demo:
- It reads a line about 2.5 s after typing stops, or when the caret leaves the line.
- It dispatches lines that name a known project.
- It asks "Which project is this for?" for lines that name none, and asks for the component
  when a product has several.
- It treats the first non-blank line under a question as the answer only when it is little
  more than the missing fact, e.g. "Notely web app". A full sentence such as "Beacon should
  retry feeds that time out" is a new idea even when it sits right under the question.
- It stops a task whose text is struck.
- It reports "Update sent" when a working task's line is edited.

These rules only exercise the interface; they are not the judgement design in §6.2.

A backend-free build is published for owner review (static files; nothing is sent
anywhere; the document is kept in the browser's local storage). Its address is kept in the
maintainer's private infrastructure notes, not in this public repository.

Captures: [desktop](assets/web-task-butler-preview.png),
[result card](assets/web-task-butler-result-preview.png),
[dark](assets/web-task-butler-dark-preview.png),
[390 px](assets/web-task-butler-mobile-preview.png).

### 9.8 Corral / SessKit change list derived so far

1. Session-control layer shared by the phone daemon, the web service and the coordinator (create, send with
   confirmed delivery, interrupt, observe), extracted from `SessionHub`.
2. Per-assistant adapters for startup gates, readiness and mid-turn submission.
3. SessKit: surface mid-turn user input (Claude queue records — landed 2026-09-29:
   `attachment/queued_command` prompts surface as user messages; `queue-operation`
   rows ignored); one completion per turn
   (Codex); quota/limit as its own status (Cursor, others).
4. ~~Kimi launch flag `--auto`~~ — shipped in v0.24.228 (F6).
5. Persistent same-task assistant reassignment after quota/login failure, with prior progress
   gathered from verified session/workspace evidence rather than a call to the unavailable worker.

### 9.9 Remaining before design freeze (2026-09-29)

1. ~~Full E3 run through the command boundary~~ — done (§9.4). Open: S16 (vague bug)
   is still unreliable (3 of 5).
2. ~~Long-running coordinator~~ — done (E6, 26/26 continuous and restarted, §9.4).
3. Trigger delays from the owner's real typing in the preview page (E4 used synthetic traces;
   a 10-second thinking pause still woke the coordinator at 8 s / 3 s). Needs the owner.
4. Owner review of the interactive demo (§9.7). The first trial (2026-09-29) produced two
   decisions: unseen text deletes outright (requirement 12) and the roles get names
   (requirement 17). Further review is open.
6. Read the scheduled full run of S1–S22 (queued for after the model usage limit reset,
   2026-09-30). It checks the S16 fix, the stricter action shapes (possible extra retries)
   and the first results for S17–S22.
5. Freeze: acceptance checks for §6.1 and evaluation cases for §6.2 (coverage and gaps in
   §9.10); implementation plan in §10, with three decisions for the owner.

### 9.10 Freeze coverage (2026-09-29)

Freeze requires an acceptance check for every §6.1 guarantee and an evaluation case for every
§6.2 judgement.
- **Covered:** checked in a spike today.
- **Product:** can only be checked once the real service exists; it gets an acceptance test
  in the implementation plan.
- **Gap:** needs a new spike check or scenario before freeze.

| §6.1 guarantee | Status | Evidence |
|---|---|---|
| Anchors follow typing, strikes, cut/paste, undo/redo | Covered | E2 T9, E5 |
| Identical text removed and re-inserted in one settle window is reported as a move | Gap | — |
| Undo changes text only; never undoes a dispatch or unlocks done text | Covered | E2 T6, T18, T21, T22 (F12 fixed) |
| Seen text struck, unseen text deleted, mixed selections split | Covered | E2 T1–T5, T15–T20 |
| Completion marking verifies the quote; falls back to the stored anchor | Partly | Boundary checks the quote and evidence (`contract.py`, 53 tests in `test_contract.py`, including action shapes); fallback is Product |
| A span shared by several tasks turns green only when all are done | Product | — |
| Project highlight: whole words, short names case-sensitive, "not a project" | Partly | E2 T12; "not a project" is Product |
| Typing always wins; coordinator never inserts inside the block being edited | Partly | E2 T10 (inserts while typing); the insertion policy is Product |
| Never lose the draft; single editing tab | Product | Demo keeps the document in the browser only |
| Restart: ledger persisted and reconciled with live sessions, nothing replayed twice | Partly | E6 restart of the coordinator from the ledger; session reconciliation is Product |
| Confirmed delivery to workers | Partly | E1 confirmed delivery from each assistant's history; the product check is Product |
| Phone push for open questions | Product | Existing Corral push path |
| Result card contents and "open session in Corral" | Partly | E2 T13, demo; "open session" is Product |
| Worker sessions listed in Corral, grouped | Product | — |

| §6.2 judgement | Scenarios |
|---|---|
| One sentence → several tasks | S1 |
| Scattered sentences → one task | S17 (written, first run pending) |
| Later text amends an earlier idea | S7; E6 |
| Repeated idea, including one already done | S8 (follow-up), S18 (pending) |
| Struck text: typo fix vs. withdrawal | S5, S6; E6 |
| Edit to dispatched text: steer vs. stop and restart | S7 (steer), S19 reversal (pending) |
| Which project; ambiguous; new project; answer in text | S3, S9, S12, S16 |
| Project aliases; several projects per idea | S1 (several), S20 shorthand (pending) |
| Dependencies, including one discovered after start | S2, S21 (pending) |
| Failures, retries, quota, switching assistants | S11, S15, S22 worker error (pending) |
| Accepting a finished turn as done | S13, S14 |
| Whether writing answers a question; whether to ask at all | S4, S12, S16 |

## 10. Implementation plan (*Proposed*, for design freeze)

Order follows risk: the parts every later slice depends on come first. Each slice ships
behind no user-facing entry until slice 6 (unfinished features are not exposed).

| Slice | What | Where | Acceptance |
|---|---|---|---|
| 0 | **Session control layer**: host, deliver-and-confirm, interrupt, stop, observe turn state. Extracted from `SessionHub` so the phone daemon and the butler share it. Per-assistant startup gates, input readiness, composer check and mid-turn mode become runtime-adapter methods (fixes F1/F2; follows the "runtime-private behaviour lives in `runtime/`" rule). | new `src/corral/control.py`; `runtime/*.py`; `remote/sessions.py` delegates | Unit tests per adapter from recorded pane text. Opt-in live E1 matrix per installed assistant: start in a new folder, first message, mid-turn message, delivery confirmed from history. Phone `send_turn` regression tests unchanged. |
| 1 | **SessKit signals**: one completion id per turn (Codex), quota/limit and not-logged-in as their own states (Cursor). Kimi excluded (§8.5). | SessKit, then Corral pin | SessKit contract tests on recorded histories. |
| 2 | **Ledger and command boundary**: tasks, questions, dependencies, anchors, results, audit log (actor, action, target, time; same transaction as the change). Boundary = the spike's `contract.py` + shape checks + `test_contract.py`. | new `src/corral/butler/` (`ledger.py`, `boundary.py`); SQLite under `~/.config/corral/butler/` | Deterministic tests: every action and state from §6.1; audit row for every mutation; replay of E6's 26 rounds against the ledger with recorded coordinator answers. |
| 3 | **Document service and editor**: loopback-only server on `websockets` (already a dependency) serving the editor and a live channel. Random token, origin check, single editing tab, versioned saves, seen ranges persisted with the document. Editor source moves from the spike; the built bundle ships in the package. | `src/corral/butler/web.py`, `document.py`; editor source under `cli/web/butler/` | E2/E5 checks (25 + anchor) run against the served page; restart and two-tab tests; token/origin rejection tests. |
| 4 | **Coordinator runner**: one hosted coordinator session, standing prompt from `coordinator_prompt.md`. Rounds are triggered per §5: block settle, idle delay, one round in flight. The payload sends finished tasks in compact form. Replies are parsed, validated with one retry, and applied to the ledger. The coordinator restarts from the ledger when its context grows (E6). | `src/corral/butler/coordinator.py` | Recorded-replay tests of trigger timing (E4 traces); E3 scenarios + new S17–S22 through the real runner with a cheap model; restart test. |
| 5 | **Worker lifecycle**: create a worker session per task with `worker_prompt.md` + brief. Steer, stop and reassign go through slice 0. Completion and failure events go back to the coordinator. Worker sessions appear in Corral's list, grouped. | `src/corral/butler/workers.py`; `split_layout` grouping | Live end-to-end on a disposable project: idea → task → worker → verified report → done span; quota switch with a fake unavailable assistant. |
| 6 | **Owner-facing finish**: margin notes and result card with "open session in Corral"; questions pushed to the phone through the existing push path; a command to open the page. | editor; `remote/` push; `bootstrap.py` entry | Browser screenshots light/dark/phone; push received on a real phone; §9.10 Product rows all checked. |

Decisions for the owner at freeze:
1. **Entry point name.** Proposed: `corral ideas` opens the page in the browser. The internal
   code name "butler" never appears in the interface.
2. **Coordinator assistant.** Proposed: the first usable of Claude, Codex, Pi, overridable in
   settings (requirement 3 leaves it open).
3. **Scope of the first release.** Proposed: slices 0–5 plus the page command. Phone push of
   questions (slice 6) can follow in the next release.
4. **Stepping away and coming back** (§1, requirement 18). Proposed: the "pick up here"
   summary and the blocking-only push in slice 6; the explicit "step away" action is optional
   and may come later.

## 11. References

- CodeMirror decorations / atomic ranges: https://codemirror.net/examples/decoration/ ,
  https://codemirror.net/docs/ref/
- CM6 source-preserving live preview: https://github.com/kenforthewin/atomic-editor
- Tiptap Markdown round-trip limits: https://tiptap.dev/docs/editor/markdown
- Conductor OSS (Markdown board → tmux agents; relies on explicit board columns, not free
  text): https://conductross.com/

<!-- 该文档整理/压缩于 2026-09-29 -->
