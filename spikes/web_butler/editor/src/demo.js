// Backend-free demo of the idea document. The editor mechanics are the real prototype
// (main.js); X is simulated here with simple rules so the page can be tried anywhere.
import { EditorView } from "@codemirror/view";
import "./main.js";

const B = window.butler;
const STORE = "corral-ideas-demo-v1";
const SETTLE_MS = 2500;
const PROJECTS = ["Corral", "Notely", "Beacon", "SessKit"];
const ASSISTANTS = { claude: "Claude", codex: "Codex", pi: "Pi" };
const STATE_LABEL = {
  queued: "Queued", running: "Working", asking: "Needs your answer", failed: "Failed",
  stopped: "Stopped", done: "Done",
};

const $ = (s) => document.querySelector(s);
const tasks = {};       // id -> { id, project, assistant, state, title, startedAt, finishedAt }
const questions = {};   // agent text id -> { id, ideaFrom, ideaTo, ideaText, open, kind, project }
let seq = 1, rotate = 0, dirty = [], settleTimer = null, busy = 0;

// ---------------------------------------------------------------- editor + layout hooks
const USER_EDITS = ["input", "delete", "undo", "redo", "move"];
const layout = EditorView.updateListener.of((u) => {
  if (u.docChanged) dirty = dirty.map((r) => ({ from: u.changes.mapPos(r.from, -1), to: u.changes.mapPos(r.to, 1) }));
  let edited = false;
  for (const tr of u.transactions) {
    if (!USER_EDITS.some((e) => tr.isUserEvent(e))) continue;
    edited = true;
    // a deletion becomes a strike with no text change, so mark the caret's line as well
    const head = tr.newSelection.main.head;
    dirty.push({ from: head, to: head });
  }
  if (edited) {
    u.changes.iterChangedRanges((_fa, _ta, fb, tb) => dirty.push({ from: fb, to: tb }));
    scheduleSettle();
  }
  if (u.docChanged || edited) save();
  if (u.selectionSet && !edited && dirty.length) {
    const line = u.state.doc.lineAt(u.state.selection.main.head);
    if (dirty.some((r) => r.to < line.from || r.from > line.to)) settle(false);
  }
  requestAnimationFrame(renderNotes);
});

const view = B.create($("#ed"), PROJECTS, "", { extensions: [layout], onOpenResult: openResult });
window.view = view;

// ---------------------------------------------------------------- simulated X
function scheduleSettle() {
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => settle(true), SETTLE_MS);
  renderStatus();
}

function settle(all) {
  const head = view.state.selection.main.head;
  const caretLine = view.state.doc.lineAt(head);
  const lines = new Set();
  const keep = [];
  for (const r of dirty) {
    const a = view.state.doc.lineAt(Math.min(r.from, view.state.doc.length)).number;
    const b = view.state.doc.lineAt(Math.min(r.to, view.state.doc.length)).number;
    const onCaret = a <= caretLine.number && caretLine.number <= b;
    if (!all && onCaret) { keep.push(r); continue; }
    for (let n = a; n <= b; n++) lines.add(n);
  }
  dirty = keep;
  if (!lines.size) { renderStatus(); return; }
  // these lines are now submitted to X: from here on, deleting them strikes instead of erasing
  for (const n of lines) {
    if (n > view.state.doc.lines) continue;
    const line = view.state.doc.line(n);
    if (line.to > line.from) B.markSeen(view, line.from, line.to);
  }
  busy++;
  renderStatus();
  // X reads for a moment, like a real round
  setTimeout(() => {
    busy--;
    for (const n of [...lines].sort((x, y) => x - y)) {
      if (n <= view.state.doc.lines) readLine(view.state.doc.line(n));
    }
    renderStatus();
    save();
  }, 700);
}

function liveSpans(line) {
  // live = not struck and not written by X
  const agents = B.agentTexts(view);
  const isAgent = (p) => agents.some((a) => p >= a.from && p < a.to);
  const spans = [];
  let start = -1;
  for (let p = line.from; p <= line.to; p++) {
    const live = p < line.to && !isAgent(p) && !B.struckIn(view, p, p + 1);
    if (live && start < 0) start = p;
    if (!live && start >= 0) { spans.push([start, p]); start = -1; }
  }
  return spans;
}

function readLine(line) {
  if (!line.text.trim() || /^\s*#/.test(line.text)) return;
  const anchors = B.anchors(view).filter((a) => a.to > line.from && a.from < line.to);
  for (const a of anchors) {
    const t = tasks[a.id];
    if (!t || t.state === "done" || t.state === "stopped") continue;
    if (B.struckIn(view, a.from, a.to)) {
      setState(a.id, "stopped");
      t.note = "You crossed this out";
    } else if (t.state === "running") {
      t.note = `Update sent to ${ASSISTANTS[t.assistant]}`;
      flashNote(a.id);
    }
  }
  const outside = liveSpans(line).map(([f, to]) => trimSpan(f, to))
    .filter(([f, to]) => to - f > 0 && !anchors.some((a) => f < a.to && to > a.from));
  for (const [f, to] of outside) {
    const text = view.state.sliceDoc(f, to);
    const answered = answerFor(line);
    if (answered && isReply(answered, text)) { resolveQuestion(answered, text, f, to); continue; }
    if (!settled(text)) continue;
    const project = findProject(text);
    if (!project) { ask(line, f, to, text, "project"); continue; }
    const component = componentOf(project, text);
    if (!component) { ask(line, f, to, text, "component", project); continue; }
    dispatch(f, to, text, component);
  }
}

function trimSpan(f, to) {
  const s = view.state.sliceDoc(f, to);
  const lead = s.length - s.trimStart().length, tail = s.length - s.trimEnd().length;
  return [f + lead, to - tail];
}

function settled(text) {
  const t = text.trim();
  if ([...t].length < 12) return false;
  return !/(\.\.\.|…|，|,|:|：|还有那个|然后|and|then)$/i.test(t);
}

function findProject(text) {
  return PROJECTS.find((p) => new RegExp(`(^|[^\\p{L}])${p}([^\\p{L}]|$)`, "iu").test(text)) || null;
}

function componentOf(project, text) {
  if (project === "Corral") return /iphone|ios|phone|手机/i.test(text) ? "Corral/ios" : "Corral/cli";
  if (project === "Notely") {
    if (/web|网页|browser|浏览器/i.test(text)) return "Notely/web";
    if (/backend|后端|server|服务|pdf|export|导出/i.test(text)) return "Notely/backend";
    return null;
  }
  return project === "Beacon" ? "Beacon/backend" : project;
}

function pickAssistant(text) {
  const named = Object.keys(ASSISTANTS).find((k) => new RegExp(`\\b${k}\\b`, "i").test(text));
  if (named) return named;
  const keys = Object.keys(ASSISTANTS);
  return keys[rotate++ % keys.length];
}

function dispatch(from, to, text, component) {
  const id = `t${seq++}`;
  tasks[id] = { id, project: component, assistant: pickAssistant(text), state: "queued", title: text };
  B.addAnchor(view, id, from, to, "queued");
  setTimeout(() => {
    if (tasks[id].state !== "queued") return;
    tasks[id].startedAt = Date.now();
    setState(id, "running");
    setTimeout(() => finish(id), 7000 + Math.random() * 6000);
  }, 1500);
}

function finish(id) {
  const t = tasks[id];
  if (t.state !== "running") return;
  const a = B.anchors(view).find((x) => x.id === id);
  if (!a || B.struckIn(view, a.from, a.to)) return;
  t.finishedAt = Date.now();
  t.state = "done";
  B.markDone(view, a.from, a.to, id,
    `${ASSISTANTS[t.assistant]} finished this in ${t.project}.\n` +
    `Checked by running the project's tests and trying the change on the real screen.\n\n` +
    `This is a demo, so no code was changed.`);
  renderAll();
  save();
}

function setState(id, state) {
  tasks[id].state = state;
  B.setTaskState(view, id, state);
  renderAll();
  save();
}

function ask(line, from, to, text, kind, project) {
  if (Object.values(questions).some((q) => q.open && q.ideaText === text)) return;
  const id = `x${seq++}`;
  const prompt = kind === "project"
    ? "Which project is this for?"
    : `Which part of ${project}: web or backend?`;
  questions[id] = { id, ideaFrom: from, ideaTo: to, ideaText: text, open: true, kind, project };
  B.insertAgentText(view, line.to, `\n${prompt}`, id);
  renderAll();
}

function answerFor(line) {
  // an answer is owner text on the lines right after an open question
  for (const a of B.agentTexts(view)) {
    const q = questions[a.id];
    if (!q || !q.open) continue;
    // the reply is the first non-blank line after the question
    const qLine = view.state.doc.lineAt(a.to).number;
    if (line.number <= qLine) continue;
    let gap = true;
    for (let n = qLine + 1; n < line.number; n++) if (view.state.doc.line(n).text.trim()) gap = false;
    if (gap) return q;
  }
  return null;
}

function isReply(q, text) {
  // a reply is little more than the missing fact; anything longer is a new idea
  const words = (t) => t.trim().split(/[\s,.;:，。；：]+/u).filter(Boolean);
  if (q.kind === "component") return /web|网页|backend|后端|server|服务/i.test(text) && [...text].length <= 60;
  const project = findProject(text);
  if (!project) return false;
  const FILLER = /\b(it|it's|its|is|the|this|that|that's|one|for|in|on|of|a|an|app|project|please|i|mean)\b|是|的|这个|那个|项目/giu;
  const rest = text.replace(new RegExp(project, "i"), "").replace(FILLER, " ");
  return words(rest).length <= 3 && [...rest.replace(/\s/g, "")].length <= 16;
}

function resolveQuestion(q, answer, answerFrom, answerTo) {
  const project = q.kind === "component" ? q.project : findProject(answer);
  const component = project && componentOf(project, `${q.ideaText} ${answer}`);
  if (!component) return; // X waits for a clearer answer
  q.open = false;
  const idea = locate(q.ideaText) || [answerFrom, answerTo];
  dispatch(idea[0], idea[1], q.ideaText, component);
  renderAll();
  save();
}

function locate(text) {
  const at = B.text(view).indexOf(text);
  return at < 0 ? null : [at, at + text.length];
}

// ---------------------------------------------------------------- margin notes
const notes = $("#notes");
function renderNotes() {
  const box = notes.getBoundingClientRect();
  const items = [];
  for (const a of B.anchors(view)) {
    const t = tasks[a.id];
    if (!t) continue;
    items.push({ key: a.id, pos: a.from, kind: t.state, task: t });
  }
  for (const a of B.agentTexts(view)) {
    const q = questions[a.id];
    if (q && q.open) items.push({ key: a.id, pos: a.from, kind: "question", question: q });
  }
  // lines X has not read yet get a faint dot where their status will appear
  const taken = new Set(items.map((it) => view.state.doc.lineAt(Math.min(it.pos, view.state.doc.length)).number));
  const pending = new Set();
  for (const r of dirty) {
    const line = view.state.doc.lineAt(Math.min(r.from, view.state.doc.length));
    if (taken.has(line.number) || pending.has(line.number) || /^\s*#/.test(line.text)) continue;
    if ([...line.text.trim()].length < 12) continue;
    pending.add(line.number);
    items.push({ key: `pending-${line.number}`, pos: line.from, kind: "pending" });
  }
  items.sort((x, y) => x.pos - y.pos);
  const seen = new Set();
  let floor = 0;
  for (const it of items) {
    const c = view.coordsAtPos(Math.min(it.pos, view.state.doc.length));
    if (!c) continue;
    let el = notes.querySelector(`[data-note="${it.key}"]`);
    if (!el) {
      el = document.createElement("button");
      el.type = "button";
      el.className = "note";
      el.dataset.note = it.key;
      el.addEventListener("click", () => { if (!el.dataset.note.startsWith("pending-")) noteClicked(el.dataset.note); });
      el.addEventListener("mouseenter", () => highlight(el.dataset.note, true));
      el.addEventListener("mouseleave", () => highlight(el.dataset.note, false));
      notes.append(el);
    }
    fillNote(el, it);
    const top = Math.max(c.top - box.top - 1, floor);
    el.style.transform = `translateY(${top}px)`;
    floor = top + el.offsetHeight + 6;
    seen.add(it.key);
  }
  for (const el of notes.querySelectorAll(".note")) if (!seen.has(el.dataset.note)) el.remove();
}

function fillNote(el, it) {
  const kind = it.kind;
  el.dataset.kind = kind;
  let title, meta;
  if (kind === "pending") {
    title = busy > 0 ? "X is reading" : "X will read this";
    meta = "";
  } else if (kind === "question") {
    title = "Waiting for your answer";
    meta = "Reply on the next line";
  } else {
    const t = it.task;
    title = t.note && (kind === "running" || kind === "stopped") ? t.note : STATE_LABEL[kind];
    meta = `${ASSISTANTS[t.assistant]} · ${t.project}`;
    if (kind === "done" && t.finishedAt) meta += ` · ${ago(t.finishedAt)}`;
  }
  const html = `<span class="dot"></span><span class="note-text"><span class="note-title"></span><span class="note-meta"></span></span>`;
  if (!el.firstChild) el.innerHTML = html;
  el.querySelector(".note-title").textContent = title;
  el.querySelector(".note-meta").textContent = meta;
  el.setAttribute("aria-label", `${title}. ${meta}`);
}

function flashNote(id) {
  const el = notes.querySelector(`[data-note="${id}"]`);
  if (!el) return;
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
  setTimeout(() => { const t = tasks[id]; if (t && t.state === "running") t.note = null; renderAll(); }, 2600);
}

function highlight(key, on) {
  for (const el of document.querySelectorAll(`[data-anchor="${key}"],[data-task="${key}"],[data-agent="${key}"]`)) {
    el.classList.toggle("is-linked", on);
  }
}

const narrow = window.matchMedia("(max-width: 860px)");
function noteClicked(key) {
  const t = tasks[key];
  const el = notes.querySelector(`[data-note="${key}"]`);
  if (narrow.matches && el && !(t && t.state === "done")) {
    // on a phone the margin only has dots, so a tap shows what the dot means
    const q = questions[key];
    return openCard({
      kind: el.dataset.kind, badge: el.querySelector(".note-title").textContent,
      title: t ? t.title : q.ideaText, body: "", meta: el.querySelector(".note-meta").textContent,
    }, el.getBoundingClientRect());
  }
  if (t && t.state === "done") {
    const frags = document.querySelectorAll(`[data-task="${key}"]`);
    if (frags.length) return openResult(key, frags[frags.length - 1].getBoundingClientRect());
  }
  const range = t ? B.anchors(view).find((a) => a.id === key) : B.agentTexts(view).find((a) => a.id === key);
  if (!range) return;
  const pos = t ? range.from : view.state.doc.lineAt(range.to).to;
  view.dispatch({ selection: { anchor: Math.min(pos + (t ? 0 : 1), view.state.doc.length) },
    effects: EditorView.scrollIntoView(range.from, { y: "center" }) });
  view.focus();
  highlight(key, true);
  setTimeout(() => highlight(key, false), 1200);
}

// ---------------------------------------------------------------- result card
const pop = $("#result");
function openResult(id, rect) {
  const t = tasks[id];
  openCard({
    kind: "done", badge: "Done", title: t ? t.title : "", body: B.result(id) || "",
    meta: t ? `${ASSISTANTS[t.assistant]} · ${t.project}${t.finishedAt ? ` · ${ago(t.finishedAt)}` : ""}` : "",
  }, rect);
}
function openCard({ kind, badge, title, body, meta }, rect) {
  pop.dataset.kind = kind;
  $("#result .badge").textContent = badge;
  $("#result-title").textContent = title;
  $("#result-body").textContent = body;
  $("#result-body").hidden = !body;
  $("#result-meta").textContent = meta;
  pop.hidden = false;
  const w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.max(12, Math.min(rect.right - 20, window.innerWidth - w - 12));
  const below = rect.bottom + 10 + h < window.innerHeight;
  pop.style.left = `${left}px`;
  pop.style.top = `${below ? rect.bottom + 10 : Math.max(12, rect.top - h - 10)}px`;
}
function closeResult() { if (!pop.hidden) { pop.hidden = true; view.focus(); } }
$("#result-close").addEventListener("click", closeResult);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeResult(); });
document.addEventListener("mousedown", (e) => {
  if (!pop.hidden && !pop.contains(e.target) && !e.target.closest("[data-task],.note")) closeResult();
});
window.addEventListener("scroll", closeResult, { passive: true });

// ---------------------------------------------------------------- top bar
function renderStatus() {
  const reading = busy > 0 || dirty.length > 0;
  const status = $("#x-status");
  status.dataset.state = reading ? "reading" : "idle";
  status.querySelector("span").textContent = reading ? "X is reading" : "X is up to date";
  const open = B.agentTexts(view).filter((a) => questions[a.id]?.open);
  const pill = $("#questions");
  pill.hidden = open.length === 0;
  pill.textContent = open.length === 1 ? "1 question" : `${open.length} questions`;
  const running = Object.values(tasks).filter((t) => t.state === "running" || t.state === "queued").length;
  $("#working").hidden = running === 0;
  $("#working").textContent = `${running} working`;
  requestAnimationFrame(renderNotes);
}
let jump = 0;
$("#questions").addEventListener("click", () => {
  const open = B.agentTexts(view).filter((a) => questions[a.id]?.open);
  if (open.length) noteClicked(open[jump++ % open.length].id);
});

function renderAll() {
  for (const a of B.agentTexts(view)) {
    const q = questions[a.id];
    document.body.classList.toggle(`answered-${a.id}`, !!q && !q.open);
  }
  answeredStyle();
  renderStatus();
  renderNotes();
}
function answeredStyle() {
  const ids = Object.values(questions).filter((q) => !q.open).map((q) => `[data-agent="${q.id}"]`);
  $("#answered-style").textContent = ids.length ? `${ids.join(",")}{opacity:.5}` : "";
}

function ago(t) {
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}

// ---------------------------------------------------------------- persistence
let saveTimer = null, resetting = false;
function save() {
  clearTimeout(saveTimer);
  if (resetting) return;
  saveTimer = setTimeout(saveNow, 300);
}
function saveNow() {
  {
    const done = {};
    for (const [id] of Object.entries(tasks)) if (B.result(id)) done[id] = B.result(id);
    localStorage.setItem(STORE, JSON.stringify({
      md: B.toMarkdown(view), anchors: B.anchors(view), tasks, questions, done, seq,
      agents: B.agentTexts(view).map((a) => a.id), seen: B.seen(view),
    }));
  }
}

function restore(saved) {
  B.load(view, saved.md, saved.seen); // questions come back numbered q1..qn in document order
  B.agentTexts(view).forEach((a, i) => {
    const q = saved.questions[(saved.agents || [])[i]];
    if (q) questions[a.id] = { ...q, id: a.id };
  });
  Object.assign(tasks, saved.tasks);
  seq = saved.seq || 1;
  for (const a of saved.anchors) B.addAnchor(view, a.id, a.from, a.to, a.state);
  for (const a of saved.anchors) if (a.state === "done" && saved.done[a.id]) B.markDone(view, a.from, a.to, a.id, saved.done[a.id]);
  // work that was in flight when the page closed carries on
  for (const t of Object.values(tasks)) {
    if (t.state === "queued" || t.state === "running") {
      t.state = "running";
      B.setTaskState(view, t.id, "running");
      setTimeout(() => finish(t.id), 4000 + Math.random() * 4000);
    }
  }
}

// ---------------------------------------------------------------- sample document
const SAMPLE = `# This week

Make Corral session titles consistent between Mac and iPhone.

Corral iPhone: find an older session quickly by project or title. ~~Maybe this needs a separate screen.~~ Keep it in the list.

Notely PDF export sometimes loses Chinese characters, check a real export after the fix.

Fix the login bug that sends people back to the sign-in page
<!--x-->Which project is this for?<!--/x-->

`;

function seed() {
  B.load(view, SAMPLE);
  const text = B.text(view);
  const put = (snippet, state, assistant, project, extra = {}) => {
    const from = text.indexOf(snippet);
    const id = `t${seq++}`;
    tasks[id] = { id, project, assistant, state, title: snippet, ...extra };
    B.addAnchor(view, id, from, from + snippet.length, state);
    return { id, from, to: from + snippet.length };
  };
  const done = put("Make Corral session titles consistent between Mac and iPhone.", "done", "claude", "Corral/ios",
    { finishedAt: Date.now() - 18 * 60_000 });
  B.markDone(view, done.from, done.to, done.id,
    "Titles now come from the conversation itself, so Mac and iPhone show the same title.\n" +
    "Checked on the Mac session list and on a real iPhone.");
  const run = put("Corral iPhone: find an older session quickly by project or title.", "running", "codex", "Corral/ios",
    { startedAt: Date.now() });
  setTimeout(() => finish(run.id), 15000);
  const pdf = put("Notely PDF export sometimes loses Chinese characters, check a real export after the fix.", "running",
    "pi", "Notely/backend", { startedAt: Date.now() });
  setTimeout(() => finish(pdf.id), 24000);
  const [q] = B.agentTexts(view);
  const idea = "Fix the login bug that sends people back to the sign-in page";
  const at = text.indexOf(idea);
  questions[q.id] = { id: q.id, ideaFrom: at, ideaTo: at + idea.length, ideaText: idea, open: true, kind: "project" };
  const end = view.state.doc.length;
  view.dispatch({ selection: { anchor: end } });
}

$("#reset").addEventListener("click", () => {
  resetting = true;
  clearTimeout(saveTimer);
  localStorage.removeItem(STORE);
  location.reload();
});
window.addEventListener("pagehide", () => { if (!resetting && saveTimer) { clearTimeout(saveTimer); saveTimer = null; saveNow(); } });

const saved = (() => { try { return JSON.parse(localStorage.getItem(STORE)); } catch { return null; } })();
if (saved && saved.md) {
  restore(saved);
  view.dispatch({ selection: { anchor: view.state.doc.length } }); // keep writing where the document ends
} else seed();
renderAll();
new ResizeObserver(() => renderNotes()).observe($("#ed"));
setInterval(renderNotes, 30_000);
view.focus();
document.body.dataset.ready = "1";
