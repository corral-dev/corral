// E2 editor spike: CodeMirror 6 with "delete = strikethrough", agent-authored text,
// locked done spans with a check widget, task anchors, and project-name hints.
// The document text never loses characters; strike/agent/done/anchor are range sets
// kept beside the text and serialized to Markdown only at save time.
import { EditorState, StateField, StateEffect, Annotation, RangeSet, RangeValue, ChangeSet, Transaction } from "@codemirror/state";
import { EditorView, Decoration, WidgetType, MatchDecorator, ViewPlugin, keymap } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, invertedEffects } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";

const system = Annotation.define(); // programmatic edits (agent insert, load) bypass the filter

// ---- range sets -----------------------------------------------------------------------
const addStrike = StateEffect.define({ map: (v, m) => ({ from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const removeStrike = StateEffect.define({ map: (v, m) => ({ from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const addAgent = StateEffect.define({ map: (v, m) => ({ from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const addDone = StateEffect.define({ map: (v, m) => ({ ...v, from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const resetAll = StateEffect.define();
const addAnchor = StateEffect.define({ map: (v, m) => ({ ...v, from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });
const moveAnchor = StateEffect.define({ map: (v, m) => ({ ...v, from: m.mapPos(v.from, 1), to: m.mapPos(v.to, -1) }) });

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
const agentField = rangeField(addAgent, null, () => agentMark);

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

class AnchorValue extends RangeValue { constructor(id) { super(); this.id = id; } eq(o) { return o.id === this.id; } }
const anchorField = StateField.define({
  create: () => RangeSet.empty,
  update(set, tr) {
    if (tr.effects.some((e) => e.is(resetAll))) return RangeSet.empty;
    set = set.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(addAnchor)) {
        set = set.update({ add: [new AnchorValue(e.value.id).range(e.value.from, e.value.to)], sort: true });
      } else if (e.is(moveAnchor)) {
        set = set.update({ filter: (_f, _t, value) => value.id !== e.value.id });
        set = set.update({ add: [new AnchorValue(e.value.id).range(e.value.from, e.value.to)], sort: true });
      }
    }
    return set;
  },
});

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

// ---- the core rule: user deletions become strikethrough ---------------------------------
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
  const compose = tr.isUserEvent("input.type.compose") ? start.field(composeRange) : null;
  let blocked = false, needsRewrite = false;
  tr.changes.iterChanges((fromA, toA) => {
    if (toA > fromA && intersects(done, fromA, toA)) blocked = true;
    if (toA > fromA && !(compose && fromA >= compose.from && toA <= compose.to)) needsRewrite = true;
  });
  if (blocked) return [];
  if (!needsRewrite) return tr;

  const specs = [];
  const struck = [];
  let minFrom = Infinity, maxTo = -Infinity;
  tr.changes.iterChanges((fromA, toA, _fb, _tb, inserted) => {
    const text = inserted.toString();
    const exempt = compose && fromA >= compose.from && toA <= compose.to;
    minFrom = Math.min(minFrom, fromA); maxTo = Math.max(maxTo, toA);
    if (toA > fromA && !exempt) {
      if (text) specs.push({ from: toA, insert: text }); // keep the old text; new text goes after it
      if (!covered(start.field(strikeField), fromA, toA)) struck.push([fromA, toA]);
    } else {
      specs.push({ from: fromA, to: toA, insert: text });
    }
  });
  const changes = ChangeSet.of(specs, start.doc.length);
  const effects = struck.map(([f, t]) => addStrike.of({ from: changes.mapPos(f, 1), to: changes.mapPos(t, -1) }));
  // caret: backspace lands left of the struck text; delete/cut/typing/paste land after it
  const head = tr.isUserEvent("delete.backward") ? changes.mapPos(minFrom, -1) : changes.mapPos(maxTo, 1);
  return { changes, effects, selection: { anchor: head }, userEvent: tr.annotation(Transaction.userEvent), scrollIntoView: true };
});

// undo of a strike removes the strike
const strikeUndo = invertedEffects.of((tr) => {
  const out = [];
  for (const e of tr.effects) if (e.is(addStrike)) out.push(removeStrike.of(e.value));
  for (const e of tr.effects) if (e.is(removeStrike)) out.push(addStrike.of(e.value));
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
const AGENT_OPEN = "<!--x-->", AGENT_CLOSE = "<!--/x-->";
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
function create(parent, projects, initial = "") {
  let pendingCut = null; // same-editor provenance; clipboard text is still the public payload
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: "",
      extensions: [
        history(), keymap.of([...defaultKeymap, ...historyKeymap]), markdown(), EditorView.lineWrapping,
        composeRange, strikeField, agentField, doneField, anchorField,
        strikeInsteadOfDelete, strikeUndo, lockDone, projectHighlighter(projects),
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
            if (!token || !token.anchors.length || performance.now() - token.at > 120_000 ||
                e.clipboardData?.getData("text/plain") !== token.text ||
                !current.state.selection.main.empty) return false;
            const at = current.state.selection.main.from;
            queueMicrotask(() => {
              if (current.state.sliceDoc(at, at + token.text.length) !== token.text) return;
              current.dispatch({
                effects: token.anchors.map(({ id, from, to }) => moveAnchor.of({ id, from: at + from, to: at + to })),
                annotations: system.of(true),
              });
            });
            return false;
          },
          mousedown(e) {
            if (!e.target.closest("[data-task]")) return false;
            e.preventDefault(); // Opening a result must not place a caret inside locked text.
            return true;
          },
          click(e) {
            const el = e.target.closest("[data-task]");
            const pop = document.getElementById("popover");
            if (!el) { pop.hidden = true; return false; }
            const fragments = [...document.querySelectorAll("[data-task]")].filter(
              node => node.dataset.task === el.dataset.task);
            const r = fragments[fragments.length - 1].getBoundingClientRect();
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
function load(view, md) {
  const { text, strikes, agents } = fromMarkdown(md);
  view.dispatch({ effects: resetAll.of(null), annotations: system.of(true) });
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, annotations: system.of(true) });
  view.dispatch({
    effects: [...strikes.map(([f, t]) => addStrike.of({ from: f, to: t })), ...agents.map(([f, t]) => addAgent.of({ from: f, to: t }))],
    annotations: system.of(true),
  });
}

window.butler = {
  create, load, toMarkdown: (view) => toMarkdown(view.state), log,
  text: (view) => view.state.doc.toString(),
  strikes: (view) => merged(view.state.field(strikeField), view.state.doc.length).map(([f, t]) => [f, t, view.state.sliceDoc(f, t)]),
  insertAgentText(view, pos, text) {
    view.dispatch({ changes: { from: pos, insert: text }, effects: addAgent.of({ from: pos, to: pos + text.length }), annotations: system.of(true) });
  },
  markDone(view, from, to, id, result) {
    results[id] = result;
    view.dispatch({ effects: addDone.of({ from, to, id }), annotations: system.of(true) });
  },
  addAnchor(view, id, from, to) { view.dispatch({ effects: addAnchor.of({ id, from, to }), annotations: system.of(true) }); },
  anchors(view) { const o = []; view.state.field(anchorField).between(0, view.state.doc.length, (f, t, v) => { o.push({ id: v.id, from: f, to: t, text: view.state.sliceDoc(f, t) }); }); return o; },
};
