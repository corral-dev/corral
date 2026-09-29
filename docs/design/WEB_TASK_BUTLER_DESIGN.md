# Web Task Butler (draft-driven dispatch) — requirements and analysis

> Status: **analysis draft; core decisions made, edge-case behaviour open** (2026-09-29). No product code yet.
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
3. **Design freeze**: every §6 case has a decided behaviour and an acceptance check; UI
   prototype images reviewed by the owner.
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
- **Markdown stays pure.** The draft file is byte-for-byte the owner's text. Anchors, states,
  checks and question cards live in the ledger and are rendered as view-only decorations.
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
- Done text is read-only; a "reopen" action on the popover unlocks it. Adding new text beside
  done text is a new idea, not an edit of the done one.
- **Questions from X or Y** appear as an inline card under the related sentence (view-only
  widget, not written into the Markdown), with option buttons or a free-text reply. A top-bar
  count and a keyboard jump go to the next unanswered card. Y questions go to X first; X
  answers from the draft when it can and escalates only real decisions.

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

## 6. Edge-case behavior

Status: behaviors below are **decided by the design owner (agent) 2026-09-29** unless marked
*Owner to decide*; each still needs an acceptance check. Governing principle: the owner only
writes; the system never asks when a safe default exists, and asks only for real decisions.

Text ↔ task mapping
- **One sentence, several tasks**: each task anchors to its own sub-phrase; if the words
  cannot be split, tasks share the span. The span turns green only when all its tasks are
  done; until then its popover shows per-task progress.
- **Several scattered sentences, one task**: the task holds several anchors. On completion
  all turn green; the check icon sits on the anchor X names as primary.
- **Later sentence amends an earlier idea**: treated as a change to the existing task (update
  if not started, steer if running, new linked follow-up task if done). The new sentence
  becomes an extra anchor of that task.
- **Move / cut-paste / merge paragraphs**: anchors follow the text. Identical text removed
  and re-inserted within one settle window is a move, reported to X as "moved", never as
  delete + new idea; no re-dispatch.
- **Undo/redo** changes text only; it never undoes a dispatch. Undoing an idea's text is a
  deletion; redoing it re-attaches the anchor. Undo steps that would alter locked text skip.
- **Deleting text before dispatch** (inside the settle window): the pending idea is dropped
  silently.
- **Deleting text of a running task**: *Owner to decide* (§8).
- **Editing text**: waiting-to-dispatch → countdown restarts; dispatched but blocked by a
  dependency → task updated silently; running → X steers Y, or stops and restarts Y when the
  direction reverses (X's call, never asks the owner); done → locked.
- **Same idea written twice**: the second text is linked to the existing task, no duplicate
  dispatch. If that task is already done: *Owner to decide* (§8).
- **Completion quote drifted**: the system checks X's quote against the current text; if it
  is gone, the task's stored anchor range is used as-is. Never guesses a different span.

Project names
- **Highlight is a hint, not a decision.** Whole-word matches only; names of ≤4 letters or
  common words match case-sensitively. Clicking a highlight offers "not a project"; that
  choice is remembered for the phrase. X decides the actual project independently.
- **Aliases** (e.g. a Chinese nickname) are learned when X resolves one and the owner has not
  rejected it; learned aliases highlight too.
- **No project mentioned**: X infers from nearby text and recent ideas; if not confident it
  asks with a card listing the likely candidates.
- **Several projects in one idea**: split into per-project tasks, with a dependency only if
  one really needs the other.
- **Project does not exist yet**: *Owner to decide* (§8).

Dispatch and dependencies
- **Dependency found after both started**: X tells the dependent Y to stop at a safe point
  and wait; when the prerequisite is done, the same Y session is resumed with the new facts.
- **Failure**: X retries once, on another available assistant if the cause is the assistant.
  A second failure marks the text red with a card: retry / change approach / drop.
  Dependents stay blocked and say which prerequisite they wait for.
- **Quota exhausted / assistant unavailable**: X continues the task on another available
  assistant through Corral's cross-assistant handoff, without asking.
- **Machine asleep / Corral or web service restarted**: the ledger is persisted; on start
  the system reconciles ledger with live sessions and resumes. Nothing is re-dispatched
  twice.
- **"Done" means accepted, not a finished turn**: Y must end with a report (what changed,
  where, how verified). X accepts it or sends Y back; missing or failed verification is not
  done.

Questions
- **Several open questions**: each card sits under its own text; the top counter shows the
  total; blocking questions first when jumping.
- **Anchor edited**: the card stays; X re-reads the edit and closes the card if the edit
  answered it. **Owner answers in the draft instead of the card**: same rule.
- **Owner ignores a question**: that task waits; everything else continues; no nagging in
  the editor. Phone push: *Owner to decide* (§8).
- **Y's questions** go to X first; X answers from the draft and records its answer in the
  task history; only real decisions reach the owner.

Editor experience
- **Typing always wins**: background updates never move the cursor or re-layout the block
  being edited; decorations for that block wait until it settles.
- **Result popover**: plain-language summary, project, verification evidence, assistant
  used, time taken, "open session in Corral", and "reopen".
- **Reopen**: unlocks the text and returns it to normal color; task history is kept; later
  edits become a linked follow-up task.
- **Never lose the draft**: every change is saved locally in the page and to the service;
  after a restart the page reconnects and reconciles by version. Only one tab edits; other
  tabs are read-only with a "edit here" takeover.
- **Y sessions** appear in Corral's normal session list, grouped under the butler.

## 7. Risks

- Steering interrupts a running Y; X must choose steer vs. queue vs. cancel-and-restart.
- X token cost grows with edit frequency; block settling and merging bound it.
- Project-name false positives; ambiguity must be resolvable by the owner.

## 8. Owner decisions (2026-09-29)

1. **Parallel within one project is allowed.** Independent tasks run in parallel even in the
   same project and working tree; no file-conflict handling or per-project serialization.
   X still orders tasks only for real (semantic) dependencies.
2. **Auto-dispatch.** Ideas are dispatched without a per-idea confirmation; the settle window
   in §5 only prevents dispatching half-written text.
3. **Assistant choice.** If the idea names an assistant, use it; otherwise X chooses. X
   itself runs on whichever assistant runtime is available — no fixed requirement.
4. **One draft document** per owner.

Still open (§6 *Owner to decide*):
- Deleting the text of a running task.
- Writing again an idea whose task is already done.
- An idea for a project that does not exist yet.
- Whether questions awaiting the owner also push to the phone.

## 9. References

- CodeMirror decorations / atomic ranges: https://codemirror.net/examples/decoration/ ,
  https://codemirror.net/docs/ref/
- CM6 source-preserving live preview: https://github.com/kenforthewin/atomic-editor
- Tiptap Markdown round-trip limits: https://tiptap.dev/docs/editor/markdown
- Conductor OSS (Markdown board → tmux agents; relies on explicit board columns, not free
  text): https://conductross.com/
