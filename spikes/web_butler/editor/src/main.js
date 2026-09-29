// E2 editor spike: CodeMirror 6 with "delete = strikethrough", agent-authored text,
// locked done spans with a check widget, task anchors, and project-name hints.
// The document text never loses characters; strike/agent/done/anchor are range sets
// kept beside the text and serialized to Markdown only at save time.
import { EditorState, StateField, StateEffect, Annotation, RangeSet, RangeValue, ChangeSet, Transaction } from "@codemirror/state";
import { EditorView, Decoration, WidgetType, MatchDecorator, ViewPlugin, keymap } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, invertedEffects } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { syntaxHighlighting, HighlightStyle } from "@codemirror/language";
import { tags } from "@lezer/highlight";

const system = Annotation.define(); // programmatic edits (agent insert, load) bypass the filter

// ---- range sets -----------------------------------------------------------------------
const addStrike = StateEffect.define({ map: (v, m) => ({ from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const removeStrike = StateEffect.define({ map: (v, m) => ({ from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const addAgent = StateEffect.define({ map: (v, m) => ({ ...v, from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const addDone = StateEffect.define({ map: (v, m) => ({ ...v, from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const resetAll = StateEffect.define();
const addAnchor = StateEffect.define({ map: (v, m) => ({ ...v, from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const moveAnchor = StateEffect.define({ map: (v, m) => ({ ...v, from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1),
  prevFrom: m.mapPos(v.prevFrom, 1), prevTo: m.mapPos(v.prevTo, -1) }) });

const strikeMark = Decoration.mark({ class: "cm-strike" });
const agentMark = Decoration.mark({ class: "cm-agent" });

function rangeField(effectAdd, effectRemove, makeDeco) {
  return StateField.define({
    create: () => Decoration.none,
    update(set, tr) {
      if (tr.effects.some((e) => e.is(resetAll))) return Decoration.none;
      set = set.map(tr.changes);
      for (const e of tr.effects) {
        if (e.is(effectAdd) && e.value.to > e.value.from) {
          set = set.update({ add: [makeDeco(e.value).range(e.value.from, e.value.to)], sort: true });
        } else if (effectRemove && e.is(effectRemove)) {
          const { from, to } = e.value;
          set = set.update({ filter: (f, t) => !(f >= from && t <= to), filterFrom: from, filterTo: to });
        }
      }
      return set;
    },
    provide: (f) => EditorView.decorations.from(f),
  });
}

const strikeField = rangeField(addStrike, removeStrike, () => strikeMark);
const agentField = rangeField(addAgent, null, (v) => v.id
  ? Decoration.mark({ class: "cm-agent", attributes: { "data-agent": v.id } }) : agentMark);

class CheckWidget extends WidgetType {
  constructor(id) { super(); this.id = id; }
  eq(o) { return o.id === this.id; }
  ignoreEvent() { return false; } // let the editor's handlers open the result, not the browser place a caret
  toDOM() {
    const el = document.createElement("span");
    el.className = "cm-check";
    el.dataset.task = this.id;
    el.textContent = "✓";
    return el;
  }
}
const doneField = StateField.define({
  create: () => Decoration.none,
  update(set, tr) {
    if (tr.effects.some((e) => e.is(resetAll))) return Decoration.none;
    set = set.map(tr.changes);
    for (const e of tr.effects) if (e.is(addDone)) {
      const { from, to, id } = e.value;
      set = set.update({
        add: [
          Decoration.mark({ class: "cm-done", attributes: { "data-task": id } }).range(from, to),
          Decoration.widget({ widget: new CheckWidget(id), side: 1 }).range(to),
        ],
        sort: true,
      });
    }
    return set;
  },
  provide: (f) => EditorView.decorations.from(f),
});

class AnchorValue extends RangeValue {
  constructor(id, state = "queued") { super(); this.id = id; this.state = state; }
  eq(o) { return o.id === this.id && o.state === this.state; }
}
const setTaskState = StateEffect.define(); // { id, state }
const anchorField = StateField.define({
  create: () => RangeSet.empty,
  update(set, tr) {
    if (tr.effects.some((e) => e.is(resetAll))) return RangeSet.empty;
    set = set.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(addAnchor)) {
        set = set.update({ add: [new AnchorValue(e.value.id, e.value.state).range(e.value.from, e.value.to)], sort: true });
      } else if (e.is(moveAnchor) || e.is(setTaskState)) {
        let old = null;
        set.between(0, tr.state.doc.length, (f, t, v) => { if (v.id === e.value.id) old = { f, t, v }; });
        if (!old) continue;
        const from = e.is(moveAnchor) ? e.value.from : old.f, to = e.is(moveAnchor) ? e.value.to : old.t;
        const state = e.is(setTaskState) ? e.value.state : old.v.state;
        set = set.update({ filter: (_f, _t, value) => value.id !== e.value.id });
        set = set.update({ add: [new AnchorValue(e.value.id, state).range(from, to)], sort: true });
      }
    }
    return set;
  },
});

// Anchored text shows its task state; done spans are drawn by doneField instead.
const taskMarks = EditorView.decorations.compute([anchorField], (state) => {
  const out = [];
  state.field(anchorField).between(0, state.doc.length, (f, t, v) => {
    if (t > f && v.state !== "done") {
      out.push(Decoration.mark({ class: `cm-task cm-task-${v.state}`, attributes: { "data-anchor": v.id } }).range(f, t));
    }
  });
  return Decoration.set(out, true);
});

// Markdown stays source text; styling only makes its structure readable.
const markdownLook = syntaxHighlighting(HighlightStyle.define([
  { tag: tags.heading1, class: "md-h1" },
  { tag: tags.heading2, class: "md-h2" },
  { tag: [tags.heading3, tags.heading4, tags.heading5, tags.heading6], class: "md-h3" },
  { tag: tags.processingInstruction, class: "md-mark" },
  { tag: tags.strong, class: "md-strong" },
  { tag: tags.emphasis, class: "md-em" },
  { tag: tags.monospace, class: "md-code" },
  { tag: [tags.link, tags.url], class: "md-link" },
  { tag: tags.quote, class: "md-quote" },
]));

// Commands that move text would turn into "strike here + copy there"; the owner cuts and pastes instead.
const REWRITING = new Set(["Alt-ArrowUp", "Alt-ArrowDown", "Ctrl-t"]);
const editorKeys = defaultKeymap.filter((b) => !REWRITING.has(b.key) && !REWRITING.has(b.mac));

// ---- helpers --------------------------------------------------------------------------
function covered(set, from, to) {
  // true when every position in [from, to) is inside some range of the set
  let pos = from;
  const spans = [];
  set.between(from, to, (f, t) => { spans.push([Math.max(f, from), Math.min(t, to)]); });
  spans.sort((a, b) => a[0] - b[0]);
  for (const [f, t] of spans) { if (f > pos) return false; pos = Math.max(pos, t); }
  return pos >= to;
}
function intersects(set, from, to) {
  let hit = false;
  set.between(from, to, (f, t) => { if (t > from && f < to) hit = true; });
  return hit;
}

// ---- what the coordinator has seen----------------------------------------------------------------------
// Sorted, non-overlapping [from, to) ranges of text already submitted to the coordinator. Characters the
// owner types are unseen until the next round submits them, even inside a seen sentence.
const markSeen = StateEffect.define(); // { from, to }
function addRange(ranges, from, to) {
  const out = [];
  for (const [f, t] of ranges) {
    if (t < from || f > to) out.push([f, t]);
    else { from = Math.min(from, f); to = Math.max(to, t); }
  }
  out.push([from, to]);
  return out.sort((a, b) => a[0] - b[0]);
}
function subtractRange(ranges, from, to) {
  const out = [];
  for (const [f, t] of ranges) {
    if (t <= from || f >= to) { out.push([f, t]); continue; }
    if (f < from) out.push([f, from]);
    if (t > to) out.push([to, t]);
  }
  return out;
}
const seenField = StateField.define({
  create: () => [],
  update(ranges, tr) {
    if (tr.effects.some((e) => e.is(resetAll))) ranges = [];
    else if (tr.docChanged) {
      ranges = ranges.map(([f, t]) => [tr.changes.mapPos(f, 1), tr.changes.mapPos(t, -1)]).filter(([f, t]) => t > f);
      if (!tr.annotation(system)) {
        tr.changes.iterChangedRanges((_fa, _ta, fb, tb) => { if (tb > fb) ranges = subtractRange(ranges, fb, tb); });
      }
    }
    for (const e of tr.effects) if (e.is(markSeen) && e.value.to > e.value.from) ranges = addRange(ranges, e.value.from, e.value.to);
    return ranges;
  },
});
// Split [from, to) into [from, to, seen] pieces.
function seenPieces(ranges, from, to) {
  const out = [];
  let pos = from;
  for (const [f, t] of ranges) {
    if (t <= pos || f >= to) continue;
    if (f > pos) out.push([pos, f, false]);
    out.push([Math.max(f, pos), Math.min(t, to), true]);
    pos = Math.min(t, to);
  }
  if (pos < to) out.push([pos, to, false]);
  return out;
}

// ---- the core rule: deleting seen text becomes strikethrough -------------------------------
// Text the coordinator has not seen is deleted normally; text it has seen is struck instead.
// Composition (IME) may rewrite its own in-progress text; those deletions are exempt.
const composeRange = StateField.define({
  create: () => null,
  update(r, tr) {
    if (!tr.isUserEvent("input.type.compose")) return tr.docChanged || tr.selection ? null : r;
    let from = Infinity, to = -Infinity;
    if (r) { from = tr.changes.mapPos(r.from, -1); to = tr.changes.mapPos(r.to, 1); }
    tr.changes.iterChangedRanges((_fa, _ta, fb, tb) => { from = Math.min(from, fb); to = Math.max(to, tb); });
    return from <= to ? { from, to } : null;
  },
});

const strikeInsteadOfDelete = EditorState.transactionFilter.of((tr) => {
  if (!tr.docChanged || tr.annotation(system) || tr.isUserEvent("undo") || tr.isUserEvent("redo")) return tr;
  const start = tr.startState;
  const done = start.field(doneField);
  const seen = start.field(seenField);
  const compose = tr.isUserEvent("input.type.compose") ? start.field(composeRange) : null;
  const isExempt = (fromA, toA) => compose && fromA >= compose.from && toA <= compose.to;
  let blocked = false, needsRewrite = false;
  tr.changes.iterChanges((fromA, toA) => {
    if (toA > fromA && intersects(done, fromA, toA)) blocked = true;
    if (toA > fromA && !isExempt(fromA, toA) && seenPieces(seen, fromA, toA).some((x) => x[2])) needsRewrite = true;
  });
  if (blocked) return [];
  if (!needsRewrite) return tr; // nothing the coordinator has seen is being removed: an ordinary edit

  const specs = [];
  const struck = [];
  let minFrom = Infinity, maxTo = -Infinity;
  tr.changes.iterChanges((fromA, toA, _fb, _tb, inserted) => {
    const text = inserted.toString();
    minFrom = Math.min(minFrom, fromA); maxTo = Math.max(maxTo, toA);
    if (toA === fromA || isExempt(fromA, toA)) { specs.push({ from: fromA, to: toA, insert: text }); return; }
    const pieces = seenPieces(seen, fromA, toA);
    pieces.forEach(([f, t, isSeen], i) => {
      const last = i === pieces.length - 1;
      if (isSeen) {
        if (!covered(start.field(strikeField), f, t)) struck.push([f, t]);
        if (last && text) specs.push({ from: t, insert: text }); // keep the old text; new text goes after it
      } else {
        specs.push({ from: f, to: t, insert: last ? text : "" }); // unseen text simply goes away
      }
    });
  });
  const changes = ChangeSet.of(specs, start.doc.length);
  const effects = struck.map(([f, t]) => addStrike.of({ from: changes.mapPos(f, 1), to: changes.mapPos(t, -1) }));
  // caret: backspace lands left of the struck text; delete/cut/typing/paste land after it.
  // A run that was already struck is skipped whole, so held backspace keeps moving.
  const runs = merged(start.field(strikeField), start.doc.length).concat(struck).sort((a, b) => a[0] - b[0]);
  const backward = tr.isUserEvent("delete.backward");
  let pos = backward ? minFrom : maxTo;
  for (let moved = true; moved;) {
    moved = false;
    for (const [f, t] of runs) {
      if (backward && f < pos && t >= pos) { pos = f; moved = true; }
      if (!backward && tr.isUserEvent("delete.forward") && f <= pos && t > pos) { pos = t; moved = true; }
    }
  }
  const head = changes.mapPos(pos, backward ? -1 : 1);
  return { changes, effects, selection: { anchor: head }, userEvent: tr.annotation(Transaction.userEvent), scrollIntoView: true };
});

// undo of a strike removes the strike
const strikeUndo = invertedEffects.of((tr) => {
  const out = [];
  for (const e of tr.effects) if (e.is(addStrike)) out.push(removeStrike.of(e.value));
  for (const e of tr.effects) if (e.is(removeStrike)) out.push(addStrike.of(e.value));
  for (const e of tr.effects) if (e.is(moveAnchor)) {
    const v = e.value;
    out.push(moveAnchor.of({ id: v.id, from: v.prevFrom, to: v.prevTo, prevFrom: v.from, prevTo: v.to }));
  }
  return out;
});

// done spans reject any change inside them
const lockDone = EditorState.changeFilter.of((tr) => {
  if (tr.annotation(system)) return true;
  const ranges = [];
  tr.startState.field(doneField).between(0, tr.startState.doc.length, (f, t, v) => { if (t > f) ranges.push(f, t); });
  return ranges.length ? ranges : true;
});

// ---- project-name hints -----------------------------------------------------------------
function projectHighlighter(names) {
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const long = names.filter((n) => n.length > 4).map(esc);
  const short = names.filter((n) => n.length <= 4).map(esc);
  const parts = [];
  if (long.length) parts.push(`(?<![\\p{L}\\p{N}_])(?:${long.join("|")})(?![\\p{L}\\p{N}_])`);
  const reLong = long.length ? new RegExp(parts[0], "giu") : null;
  const reShort = short.length ? new RegExp(`(?<![\\p{L}\\p{N}_])(?:${short.join("|")})(?![\\p{L}\\p{N}_])`, "gu") : null;
  const deco = Decoration.mark({ class: "cm-project" });
  const plugins = [];
  for (const re of [reLong, reShort]) if (re) {
    const m = new MatchDecorator({ regexp: re, decoration: () => deco });
    plugins.push(ViewPlugin.fromClass(class {
      constructor(view) { this.decorations = m.createDeco(view); }
      update(u) { this.decorations = m.updateDeco(u, this.decorations); }
    }, { decorations: (v) => v.decorations }));
  }
  return plugins;
}

// ---- serialization ------------------------------------------------------------------------
const AGENT_OPEN = "<!--coordinator-->", AGENT_CLOSE = "<!--/coordinator-->";
function merged(set, len) {
  const out = [];
  set.between(0, len, (f, t) => {
    const last = out[out.length - 1];
    if (last && f <= last[1]) last[1] = Math.max(last[1], t); else out.push([f, t]);
  });
  return out;
}
function toMarkdown(state) {
  const marks = []; // [pos, order, text]
  for (const [f, t] of merged(state.field(strikeField), state.doc.length)) marks.push([f, 1, "~~"], [t, 0, "~~"]);
  state.field(agentField).between(0, state.doc.length, (f, t) => { marks.push([f, 2, AGENT_OPEN], [t, -1, AGENT_CLOSE]); });
  marks.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  let out = "", pos = 0;
  for (const [p, , s] of marks) { out += state.doc.sliceString(pos, p) + s; pos = p; }
  return out + state.doc.sliceString(pos);
}
function fromMarkdown(md) {
  let text = "";
  const strikes = [], agents = [];
  let s = null, a = null;
  for (let i = 0; i < md.length;) {
    if (md.startsWith("~~", i)) { if (s === null) s = text.length; else { strikes.push([s, text.length]); s = null; } i += 2; continue; }
    if (md.startsWith(AGENT_OPEN, i)) { a = text.length; i += AGENT_OPEN.length; continue; }
    if (md.startsWith(AGENT_CLOSE, i)) { if (a !== null) agents.push([a, text.length]); a = null; i += AGENT_CLOSE.length; continue; }
    text += md[i++];
  }
  return { text, strikes, agents };
}

// ---- setup ------------------------------------------------------------------------------
const log = [];
function create(parent, projects, initial = "", options = {}) {
  let pendingCut = null; // same-editor provenance; clipboard text is still the public payload
  let armedPaste = null;
  // The anchor move is part of the paste transaction, so one undo reverts text and anchor together.
  const pasteMove = EditorState.transactionExtender.of((tr) => {
    const token = armedPaste;
    if (!token || !tr.isUserEvent("input.paste")) return null;
    armedPaste = null;
    let at = -1;
    tr.changes.iterChanges((_fa, _ta, fb, _tb, ins) => { if (at < 0 && ins.toString() === token.text) at = fb; });
    if (at < 0) return null;
    const current = {};
    tr.startState.field(anchorField).between(0, tr.startState.doc.length, (f, t, v) => { current[v.id] = [f, t]; });
    const effects = token.anchors.filter(({ id }) => current[id]).map(({ id, from, to }) => moveAnchor.of({
      id, from: at + from, to: at + to,
      prevFrom: tr.changes.mapPos(current[id][0], 1), prevTo: tr.changes.mapPos(current[id][1], -1),
    }));
    return effects.length ? { effects } : null;
  });
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: "",
      extensions: [
        history(), keymap.of([...editorKeys, ...historyKeymap]), markdown(), markdownLook, EditorView.lineWrapping,
        composeRange, seenField, strikeField, agentField, doneField, anchorField, taskMarks, ...(options.extensions || []),
        strikeInsteadOfDelete, strikeUndo, lockDone, pasteMove, projectHighlighter(projects),
        EditorView.updateListener.of((u) => { for (const tr of u.transactions) if (tr.docChanged) log.push({ t: performance.now(), ev: tr.annotation(Transaction.userEvent) || (tr.annotation(system) ? "system" : "") }); }),
        EditorView.domEventHandlers({
          cut(_e, current) {
            const selected = current.state.selection.main;
            const anchors = [];
            current.state.field(anchorField).between(selected.from, selected.to, (from, to, value) => {
              if (from >= selected.from && to <= selected.to) {
                anchors.push({ id: value.id, from: from - selected.from, to: to - selected.from });
              }
            });
            pendingCut = selected.empty ? null : {
              text: current.state.sliceDoc(selected.from, selected.to), anchors, at: performance.now(),
            };
            return false; // Let CodeMirror copy the selected plain text; deletion is struck below.
          },
          copy() { pendingCut = null; return false; },
          paste(e, current) {
            const token = pendingCut;
            pendingCut = null;
            armedPaste = token && token.anchors.length && performance.now() - token.at <= 120_000 &&
              e.clipboardData?.getData("text/plain") === token.text ? token : null;
            return false; // CodeMirror performs the paste; pasteMove adds the anchor move to it
          },
          mousedown(e) {
            if (!e.target.closest("[data-task]")) return false;
            e.preventDefault(); // Opening a result must not place a caret inside locked text.
            return true;
          },
          click(e) {
            const el = e.target.closest("[data-task]");
            const pop = document.getElementById("popover");
            if (!el) { if (pop) pop.hidden = true; return false; }
            const fragments = [...document.querySelectorAll("[data-task]")].filter(
              node => node.dataset.task === el.dataset.task);
            const r = fragments[fragments.length - 1].getBoundingClientRect();
            if (options.onOpenResult) { options.onOpenResult(el.dataset.task, r); return true; }
            pop.textContent = results[el.dataset.task] || "(no result)";
            pop.hidden = false;
            pop.style.left = `${Math.max(12, Math.min(r.right + 8, window.innerWidth - pop.offsetWidth - 12)) + window.scrollX}px`;
            pop.style.top = `${r.bottom + window.scrollY + 8}px`;
            return true;
          },
        }),
      ],
    }),
  });
  if (initial) load(view, initial);
  return view;
}
const results = {};
function load(view, md, seen = null) {
  const { text, strikes, agents } = fromMarkdown(md);
  view.dispatch({ effects: resetAll.of(null), annotations: system.of(true) });
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, annotations: system.of(true) });
  view.dispatch({
    effects: [...strikes.map(([f, t]) => addStrike.of({ from: f, to: t })), ...agents.map(([f, t], i) => addAgent.of({ from: f, to: t, id: `q${i + 1}` })),
      ...(seen || [[0, text.length]]).map(([f, t]) => markSeen.of({ from: f, to: t }))],
    annotations: system.of(true),
  });
}

window.butler = {
  create, load, toMarkdown: (view) => toMarkdown(view.state), log,
  text: (view) => view.state.doc.toString(),
  strikes: (view) => merged(view.state.field(strikeField), view.state.doc.length).map(([f, t]) => [f, t, view.state.sliceDoc(f, t)]),
  insertAgentText(view, pos, text, id) {
    view.dispatch({
      changes: { from: pos, insert: text },
      effects: [addAgent.of({ from: pos, to: pos + text.length, id }), markSeen.of({ from: pos, to: pos + text.length })],
      annotations: system.of(true),
    });
  },
  agentTexts(view) {
    const o = [];
    view.state.field(agentField).between(0, view.state.doc.length, (f, t, d) => {
      o.push({ id: d.spec.attributes?.["data-agent"] || null, from: f, to: t, text: view.state.sliceDoc(f, t) });
    });
    return o;
  },
  markSeen(view, from = 0, to = view.state.doc.length) {
    view.dispatch({ effects: markSeen.of({ from, to }), annotations: system.of(true) });
  },
  seen: (view) => view.state.field(seenField).map((r) => [...r]),
  setTaskState(view, id, state) { view.dispatch({ effects: setTaskState.of({ id, state }), annotations: system.of(true) }); },
  result: (id) => results[id],
  markDone(view, from, to, id, result) {
    results[id] = result;
    view.dispatch({ effects: [addDone.of({ from, to, id }), setTaskState.of({ id, state: "done" })], annotations: system.of(true) });
  },
  addAnchor(view, id, from, to, state = "queued") { view.dispatch({ effects: addAnchor.of({ id, from, to, state }), annotations: system.of(true) }); },
  anchors(view) { const o = []; view.state.field(anchorField).between(0, view.state.doc.length, (f, t, v) => { o.push({ id: v.id, state: v.state, from: f, to: t, text: view.state.sliceDoc(f, t) }); }); return o; },
  struckIn(view, from, to) { return covered(view.state.field(strikeField), from, to); },
};
