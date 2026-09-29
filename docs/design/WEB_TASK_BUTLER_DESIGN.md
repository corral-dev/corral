# Web Task Butler (draft-driven dispatch) — requirements and analysis

> Status: **analysis draft, awaiting owner decisions** (2026-09-29). No product code yet.
> Read before planning, implementing, or reviewing the local web idea editor, the
> dispatcher agent (X), worker agents (Y), or Corral changes made for them.
> Items marked *Proposed* are unadopted suggestions; §8 lists decisions still open.

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

## 6. Edge cases that must each have a decided behavior

The idea is simple to state; its quality is decided by these cases. Each needs an explicit
behavior and an acceptance check before implementation.

Text ↔ task mapping
- One sentence yields several tasks; several scattered sentences form one task.
- A later sentence amends an earlier idea ("the thing above should also…").
- Owner moves, cuts/pastes, or merges paragraphs: anchors must follow, not re-dispatch.
- Undo/redo across a dispatch or a lock.
- Owner deletes text whose task is running: cancel, ask, or keep? (Deletion may be tidying.)
- Owner edits text whose task is waiting-to-dispatch vs. running vs. done (done is locked).
- The same idea written twice; an idea that is already done by an earlier task.
- Completion quote chosen by X no longer exists verbatim (text drifted): fall back and verify.

Project names
- Ambiguous or common-word names, Chinese vs. English references, no project mentioned,
  several projects in one idea, a project that does not exist yet (new repository).

Dispatch and dependencies
- A dependency discovered after both tasks already started.
- A failed prerequisite: dependents stay blocked and surface to the owner.
- Y quota exhausted / assistant unavailable / machine asleep; Corral restarted mid-task.
- Y finishes a turn but the task is not actually complete; Y claims done while tests fail.

Questions
- Several open questions at once; a question whose text anchor was edited or deleted.
- Owner answers in the draft text instead of the card; owner ignores a question.
- Y asks a choice question that X can answer from the draft vs. a real owner decision.

Editor experience
- Typing is never blocked or re-laid-out by background updates (cursor stability).
- Decorations arriving while the owner is typing in the same block.
- Result popover content: what changed, where, verification evidence, open the session.
- Reopening done text, and what happens to its task history.
- Offline / web service restarted: draft never lost; the page reconnects cleanly.

## 7. Risks

- Same-project parallel Y share one working tree (workspace rule: no branches/worktrees).
- Steering interrupts a running Y; X must choose steer vs. queue vs. cancel-and-restart.
- X token cost grows with edit frequency; block settling and merging bound it.
- Project-name false positives; ambiguity must be resolvable by the owner.

## 8. Open decisions

1. Same-project parallelism: serialize per project (*Proposed default*) or allow parallel in
   one working tree.
2. Assistant for X and default assistant for Y; per-idea override syntax.
3. Auto-dispatch after countdown (*Proposed*) vs. explicit confirm per idea.
4. One draft document vs. several.

## 9. References

- CodeMirror decorations / atomic ranges: https://codemirror.net/examples/decoration/ ,
  https://codemirror.net/docs/ref/
- CM6 source-preserving live preview: https://github.com/kenforthewin/atomic-editor
- Tiptap Markdown round-trip limits: https://tiptap.dev/docs/editor/markdown
- Conductor OSS (Markdown board → tmux agents; relies on explicit board columns, not free
  text): https://conductross.com/
