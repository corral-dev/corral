// CodeMirror 6 extensions for one day section: read-only taken spans, status marks,
// guarded undo/redo and the settle window (requirement 20).
import { Annotation, EditorState, Extension, Facet, Prec, StateEffect, StateField, Text } from "@codemirror/state";
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
import { sentenceDrag } from "./sentenceDrag";
import { AttachmentWidget, attachmentExtensions, type AttachmentOptions } from "../input/attachments";
import { attachmentTokens, type AttachmentToken } from "../input/content";

export interface SpanView {
  threadId: string;
  from: number;
  to: number;
  status: Presence;
  active: boolean;
  unread: boolean;
  /** Just taken up: a brief acknowledgement, the only take-up feedback. */
  fresh: boolean;
  /** Immutable token offsets relative to the taken quote, independent of outside Markdown. */
  attachments?: readonly AttachmentToken[];
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
    el.draggable = true;
    el.textContent = this.text;
    return el;
  }
  /**
   * The caret at the sentence's start or end, measured on its own line boxes. The default
   * measure puts the end of a sentence that wraps onto the end of its first line (owner report,
   * 2026-11-03). After the sentence the caret sits in its trailing gap.
   */
  coordsAt(dom: HTMLElement, pos: number): { left: number; right: number; top: number; bottom: number } | null {
    const rects = dom.getClientRects();
    if (!rects.length) return null;
    const atEnd = pos > 0;
    const r = atEnd ? rects[rects.length - 1] : rects[0];
    const x = atEnd ? r.right + 2 : r.left - 2;
    return { left: x, right: x, top: r.top, bottom: r.bottom };
  }
  /**
   * Leave mousedown and dragstart to the browser so a press-and-move starts a native drag of
   * this element, owned by sentenceDrag.ts (CodeMirror would otherwise start a text selection
   * or its own drag). Clicks and hover still reach the editor.
   */
  ignoreEvent(e: Event): boolean {
    return e.type === "mousedown" || e.type === "dragstart";
  }
}

const assetOptions = Facet.define<AttachmentOptions, AttachmentOptions | null>({ combine: (values) => values[0] ?? null });

function buildDecorations(spans: SpanView[], doc: Text, options: AttachmentOptions | null): DecorationSet {
  const ranges = [...spans].filter((s) => s.to > s.from).sort((a, b) => a.from - b.from).flatMap((s) => {
    const text = doc.sliceString(s.from, s.to);
    const tokens = s.attachments ?? attachmentTokens(text);
    if (!options || !tokens.length) return [Decoration.replace({ widget: new SentenceWidget(text, s) }).range(s.from, s.to)];
    const pieces: ReturnType<Decoration["range"]>[] = [];
    const addText = (from: number, to: number) => {
      // Leave real line separators in the source/layout; replacement widgets never overlap.
      let start = from;
      for (const line of doc.sliceString(from, to).split("\n")) {
        if (line) pieces.push(Decoration.replace({ widget: new SentenceWidget(line, s) }).range(start, start + line.length));
        start += line.length + 1;
      }
    };
    let from = s.from;
    for (const token of tokens) {
      const start = s.from + token.from;
      const end = s.from + token.to;
      if (start < from || end > s.to) continue;
      addText(from, start);
      pieces.push(Decoration.replace({ widget: new AttachmentWidget(token, options, {
        threadId: s.threadId, className: `is-${s.status}${s.active ? " is-active" : ""}${s.fresh ? " is-fresh" : ""}`,
      }) }).range(start, end));
      from = end;
    }
    addText(from, s.to);
    return pieces;
  });
  return Decoration.set(ranges, true);
}

const takenDecorations = EditorView.decorations.compute(["doc", spansField, assetOptions], (state) => buildDecorations(state.field(spansField), state.doc, state.facet(assetOptions)));

/** New Markdown syntax must never wrap part of an already frozen source range. */
const tokenOwnership = EditorState.transactionFilter.of((tr) => {
  if (!tr.docChanged) return tr;
  const spans = tr.state.field(spansField);
  const invalid = attachmentTokens(tr.newDoc.toString()).some((token) => spans.some((s) =>
    token.from < s.to && s.from < token.to && !(token.from >= s.from && token.to <= s.to),
  ));
  return invalid ? [] : tr;
});

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

// ---- paragraphs (used when moving dragged sentences, see sentenceDrag.ts) ---------------

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
  onSettled: (from: number, to: number) => boolean | Promise<boolean>;
  onPending: (blocks: PendingBlock[]) => void;
}

/** Hands a block to the coordinator only after `settleMs` without edits to it. */
function settlePlugin(opts: SettleOptions) {
  return ViewPlugin.fromClass(
    class {
      pending: { pos: number; last: number }[] = [];
      timer: number;
      inFlight = false;
      alive = true;
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
        if (!this.pending.length || this.inFlight) return;
        const now = Date.now();
        const candidate = this.pending.find((p) => now - p.last >= opts.settleMs);
        if (!candidate) return this.publish(now);
        const block = blockAt(this.view.state, candidate.pos);
        if (!block) { this.pending = this.pending.filter((p) => p !== candidate); return; }
        const source = this.view.state.doc.toString();
        this.inFlight = true;
        void Promise.resolve(opts.onSettled(block.from, block.to)).then((accepted) => {
          if (!this.alive || !accepted || this.view.state.doc.toString() !== source) return;
          this.pending = this.pending.filter((p) => p.pos !== candidate.pos || p.last !== candidate.last);
        }).catch((error: unknown) => { console.error("Ideas take-up failed", error); }).finally(() => {
          if (!this.alive) return;
          this.inFlight = false;
          this.publish(Date.now());
        });
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
        this.alive = false;
        clearInterval(this.timer);
      }
    },
  );
}

export interface EditorHooks extends SettleOptions {
  placeholder: string;
  attachments?: AttachmentOptions;
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
    tokenOwnership,
    sentenceDrag,
    ...(hooks.attachments ? [assetOptions.of(hooks.attachments), attachmentExtensions(hooks.attachments)] : []),
    settlePlugin(hooks),
    EditorView.updateListener.of((u) => {
      if (u.docChanged) hooks.onDocChanged(u.state.doc.toString(), u.state.field(spansField));
      if (u.docChanged || u.geometryChanged || u.viewportChanged) hooks.onLayout();
    }),
    EditorView.domEventHandlers({
      click(e) {
        const el = (e.target as HTMLElement).closest<HTMLElement>("[data-thread]");
        if (!el?.dataset.thread) return false;
        if (!window.getSelection()?.isCollapsed) return false;
        hooks.onOpenThread(el.dataset.thread);
        return true;
      },
      mouseover(e) {
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
