// CodeMirror 6 extensions for one day section: read-only taken spans, status marks,
// guarded undo/redo and the settle window (requirement 20).
import { Annotation, EditorState, Extension, Prec, StateEffect, StateField, Text } from "@codemirror/state";
import {
  Decoration,
  DecorationSet,
  EditorView,
  ViewPlugin,
  ViewUpdate,
  WidgetType,
  drawSelection,
  keymap,
  placeholder as placeholderExt,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, redo, undo } from "@codemirror/commands";
import { markdownLanguage } from "@codemirror/lang-markdown";
import { HighlightStyle, LanguageSupport, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";
import type { Presence } from "../model";
import { sentenceDrag, suppressClickUntil } from "./sentenceDrag";

export interface SpanView {
  threadId: string;
  from: number;
  to: number;
  status: Presence;
  active: boolean;
  unread: boolean;
  /** Just taken up: a brief acknowledgement, the only take-up feedback. */
  fresh: boolean;
}

export interface PendingBlock {
  from: number;
  to: number;
  remainingMs: number;
}

/** Marks transactions that come from the page itself rather than the owner's typing. */
export const external = Annotation.define<boolean>();
export const setSpans = StateEffect.define<SpanView[]>();

export const spansField = StateField.define<SpanView[]>({
  create: () => [],
  update(spans, tr) {
    for (const e of tr.effects) if (e.is(setSpans)) return e.value;
    if (!tr.docChanged) return spans;
    // Inserts exactly at a span's edge stay outside it.
    return spans.map((s) => ({ ...s, from: tr.changes.mapPos(s.from, 1), to: tr.changes.mapPos(s.to, -1) }));
  },
});

/**
 * A taken sentence rendered as one atomic, non-editable unit. Its colour or background is its
 * only state mark; there is no end icon (owner, 2026-11-03). The caret can sit at either edge
 * but never inside, and long press cannot select it.
 */
class SentenceWidget extends WidgetType {
  constructor(
    readonly text: string,
    readonly s: SpanView,
  ) {
    super();
  }
  eq(o: SentenceWidget): boolean {
    const a = this.s;
    const b = o.s;
    return (
      o.text === this.text &&
      a.threadId === b.threadId &&
      a.status === b.status &&
      a.active === b.active &&
      a.unread === b.unread &&
      a.fresh === b.fresh
    );
  }
  toDOM(): HTMLElement {
    const s = this.s;
    const el = document.createElement("span");
    el.className = `cm-taken is-${s.status}${s.active ? " is-active" : ""}${s.fresh ? " is-fresh" : ""}`;
    el.dataset.thread = s.threadId;
    el.spellcheck = false;
    el.textContent = this.text;
    return el;
  }
  ignoreEvent(): boolean {
    return false;
  }
}

function buildDecorations(spans: SpanView[], doc: Text): DecorationSet {
  const ranges = [...spans]
    .filter((s) => s.to > s.from)
    .sort((a, b) => a.from - b.from)
    .map((s) => Decoration.replace({ widget: new SentenceWidget(doc.sliceString(s.from, s.to), s) }).range(s.from, s.to));
  return Decoration.set(ranges);
}

const takenDecorations = EditorView.decorations.compute(["doc", spansField], (state) => buildDecorations(state.field(spansField), state.doc));

/** Cursor motion and deletion treat each taken sentence as one unit. */
const takenAtomic = EditorView.atomicRanges.of((view) =>
  Decoration.set(
    view.state
      .field(spansField)
      .filter((s) => s.to > s.from)
      .sort((a, b) => a.from - b.from)
      .map((s) => Decoration.mark({}).range(s.from, s.to)),
  ),
);

/** Taken text is read-only: changes inside a span are dropped; edits beside it pass. */
const lockTaken = EditorState.changeFilter.of((tr) => {
  if (tr.annotation(external)) return true;
  const ranges: number[] = [];
  for (const s of tr.startState.field(spansField)) if (s.to > s.from) ranges.push(s.from, s.to);
  return ranges.length ? ranges : true;
});

// Undo/redo are dispatched with filter: false, which bypasses the change filter.
// Build the history step first and drop it if it would touch taken text.
function guarded(command: typeof undo) {
  return (view: EditorView): boolean => {
    let pending: import("@codemirror/state").Transaction | null = null;
    command({ state: view.state, dispatch: (tr) => (pending = tr) });
    if (!pending) return false;
    const tr = pending as import("@codemirror/state").Transaction;
    const spans = tr.startState.field(spansField);
    let touches = false;
    tr.changes.iterChanges((fromA, toA) => {
      for (const s of spans) if (s.from < toA && fromA < s.to) touches = true;
    });
    if (!touches) view.dispatch(tr);
    return true;
  };
}

const guardedHistory = Prec.high([
  keymap.of([
    { key: "Mod-z", run: guarded(undo), preventDefault: true },
    { key: "Mod-y", mac: "Mod-Shift-z", run: guarded(redo), preventDefault: true },
    { linux: "Ctrl-Shift-z", run: guarded(redo), preventDefault: true },
  ]),
  EditorView.domEventHandlers({
    beforeinput(e, view) {
      if (e.inputType === "historyUndo") {
        e.preventDefault();
        return guarded(undo)(view);
      }
      if (e.inputType === "historyRedo") {
        e.preventDefault();
        return guarded(redo)(view);
      }
      return false;
    },
  }),
]);

const markdownStyle = HighlightStyle.define([
  { tag: tags.heading1, class: "md-h1" },
  { tag: tags.heading2, class: "md-h2" },
  { tag: [tags.heading3, tags.heading4, tags.heading5, tags.heading6], class: "md-h3" },
  { tag: tags.strong, class: "md-strong" },
  { tag: tags.emphasis, class: "md-em" },
  { tag: tags.strikethrough, class: "md-strike" },
  { tag: tags.monospace, class: "md-code" },
  { tag: [tags.link, tags.url], class: "md-link" },
  { tag: tags.quote, class: "md-quote" },
  { tag: [tags.processingInstruction, tags.contentSeparator], class: "md-mark" },
]);

/** The paragraph (run of non-blank lines) around a position. */
export function blockAt(state: EditorState, pos: number): { from: number; to: number } | null {
  const doc = state.doc;
  let line = doc.lineAt(Math.min(pos, doc.length));
  if (!line.text.trim()) {
    // The edit may have left the caret on a fresh blank line; use the line above.
    if (line.number === 1) return null;
    line = doc.line(line.number - 1);
    if (!line.text.trim()) return null;
  }
  let first = line;
  let last = line;
  while (first.number > 1 && doc.line(first.number - 1).text.trim()) first = doc.line(first.number - 1);
  while (last.number < doc.lines && doc.line(last.number + 1).text.trim()) last = doc.line(last.number + 1);
  return { from: first.from, to: last.to };
}

// ---- paragraphs (used to place dragged sentences, see sentenceDrag.ts) ---------------

export interface Paragraph {
  from: number;
  to: number;
}

/** Runs of non-blank lines, in document order. */
export function paragraphs(state: EditorState): Paragraph[] {
  const out: Paragraph[] = [];
  let cur: Paragraph | null = null;
  for (let n = 1; n <= state.doc.lines; n++) {
    const line = state.doc.line(n);
    if (line.text.trim()) {
      if (cur) cur.to = line.to;
      else cur = { from: line.from, to: line.to };
    } else if (cur) {
      out.push(cur);
      cur = null;
    }
  }
  if (cur) out.push(cur);
  return out;
}

export interface SettleOptions {
  settleMs: number;
  onSettled: (from: number, to: number) => void;
  onPending: (blocks: PendingBlock[]) => void;
}

/** Hands a block to the coordinator only after `settleMs` without edits to it. */
function settlePlugin(opts: SettleOptions) {
  return ViewPlugin.fromClass(
    class {
      pending: { pos: number; last: number }[] = [];
      timer: number;
      constructor(readonly view: EditorView) {
        this.timer = window.setInterval(() => this.tick(), 250);
      }
      update(u: ViewUpdate) {
        if (!u.docChanged || u.transactions.every((tr) => tr.annotation(external))) return;
        const now = Date.now();
        const mapped = this.pending.map((p) => ({ pos: u.changes.mapPos(p.pos), last: p.last }));
        u.changes.iterChangedRanges((_fa, _ta, fromB) => mapped.push({ pos: fromB, last: now }));
        this.pending = this.dedupe(mapped);
        this.publish(now);
      }
      dedupe(list: { pos: number; last: number }[]) {
        const byBlock = new Map<number, { pos: number; last: number }>();
        for (const p of list) {
          const b = blockAt(this.view.state, p.pos);
          if (!b) continue;
          const prev = byBlock.get(b.from);
          if (!prev || prev.last < p.last) byBlock.set(b.from, { pos: b.from, last: p.last });
        }
        return [...byBlock.values()];
      }
      tick() {
        if (!this.pending.length) return;
        const now = Date.now();
        const keep: { pos: number; last: number }[] = [];
        for (const p of this.pending) {
          const b = blockAt(this.view.state, p.pos);
          if (!b) continue;
          if (now - p.last >= opts.settleMs) opts.onSettled(b.from, b.to);
          else keep.push(p);
        }
        this.pending = keep;
        this.publish(now);
      }
      publish(now: number) {
        const blocks: PendingBlock[] = [];
        for (const p of this.pending) {
          const b = blockAt(this.view.state, p.pos);
          if (b) blocks.push({ ...b, remainingMs: Math.max(0, opts.settleMs - (now - p.last)) });
        }
        opts.onPending(blocks);
      }
      destroy() {
        clearInterval(this.timer);
      }
    },
  );
}

export interface EditorHooks extends SettleOptions {
  placeholder: string;
  onDocChanged: (text: string, spans: SpanView[]) => void;
  onLayout: () => void;
  onOpenThread: (id: string) => void;
  onHoverThread: (id: string | null) => void;
}

// Override CodeMirror's base theme (monospace font, line padding) with the page's type.
const pageTheme = EditorView.theme({
  "&": { fontSize: "var(--text-size)", lineHeight: "var(--text-line)", fontFamily: "var(--font)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "inherit", lineHeight: "inherit", overflow: "visible" },
  ".cm-content": { padding: "2px 0", caretColor: "var(--ink)" },
  ".cm-line": { padding: "0" },
  ".cm-placeholder": { color: "var(--ink-4)" },
  ".cm-cursor, .cm-dropCursor": { borderLeft: "2px solid var(--ink)", marginLeft: "-1px" },
});

export function editorExtensions(hooks: EditorHooks): Extension[] {
  return [
    pageTheme,
    history(),
    // The editor draws its own caret so it stays visible at the edges of taken sentences,
    // where a native caret beside a non-editable box is often not painted.
    drawSelection(),
    guardedHistory,
    keymap.of([...defaultKeymap, ...historyKeymap]),
    new LanguageSupport(markdownLanguage),
    syntaxHighlighting(markdownStyle),
    EditorView.lineWrapping,
    EditorView.contentAttributes.of({ spellcheck: "true", autocapitalize: "sentences" }),
    placeholderExt(hooks.placeholder),
    spansField,
    takenDecorations,
    takenAtomic,
    lockTaken,
    sentenceDrag,
    settlePlugin(hooks),
    EditorView.updateListener.of((u) => {
      if (u.docChanged) hooks.onDocChanged(u.state.doc.toString(), u.state.field(spansField));
      if (u.docChanged || u.geometryChanged || u.viewportChanged) hooks.onLayout();
    }),
    EditorView.domEventHandlers({
      // Taken text behaves as one object: no caret or selection inside it, so a press can
      // become a drag (sentenceDrag) and a short press opens the discussion.
      mousedown(e) {
        if (e.button !== 0 || !(e.target as HTMLElement).closest(".cm-taken")) return false;
        e.preventDefault();
        return true;
      },
      click(e) {
        const el = (e.target as HTMLElement).closest<HTMLElement>("[data-thread]");
        if (!el?.dataset.thread) return false;
        if (Date.now() < suppressClickUntil) return true;
        if (!window.getSelection()?.isCollapsed) return false;
        hooks.onOpenThread(el.dataset.thread);
        return true;
      },
      mouseover(e, view) {
        if (view.dom.classList.contains("is-sentence-dragging")) return false;
        const el = (e.target as HTMLElement).closest<HTMLElement>("[data-thread]");
        hooks.onHoverThread(el?.dataset.thread ?? null);
        return false;
      },
      mouseleave() {
        hooks.onHoverThread(null);
        return false;
      },
    }),
  ];
}
