// Drag a taken sentence to another place in the same day (owner, 2026-11-03: no grip; the
// sentence itself is the handle). The drag is the platform's own HTML drag and drop: the
// sentence widget is `draggable`, and a caret shows where it will land (drawn here because
// CodeMirror's `dropCursor()` places it on the first line of a wrapped sentence's end). CodeMirror's built-in widget drag is not used: its drop would delete and reinsert the
// text without the span, and it never learns the drag ended once the moved widget re-renders. A mouse drags as soon as it moves; on
// iOS / iPadOS Safari 15+ a long press lifts it natively, so an ordinary swipe still scrolls.
// Only the drop is ours: it moves the sentence to the caret position and keeps its span,
// adding nothing but one space between Latin neighbours (owner, 2026-11-03).
import { EditorState, Transaction } from "@codemirror/state";
import { EditorView, ViewPlugin } from "@codemirror/view";
import { SpanView, external, paragraphs, setSpans, spansField } from "./editor";

/** The text a move takes away: the sentence, one neighbouring space, or a whole paragraph with its separator. */
function removalRange(state: EditorState, sp: SpanView): { from: number; to: number } | null {
  const doc = state.doc;
  const ps = paragraphs(state);
  const pi = ps.findIndex((p) => sp.from >= p.from && sp.to <= p.to);
  if (pi < 0) return null;
  const para = ps[pi];
  if (para.from === sp.from && para.to === sp.to) {
    if (pi < ps.length - 1) return { from: para.from, to: ps[pi + 1].from };
    if (pi > 0) return { from: ps[pi - 1].to, to: para.to };
    return { from: sp.from, to: sp.to };
  }
  if (doc.sliceString(sp.to, sp.to + 1) === " ") return { from: sp.from, to: sp.to + 1 };
  if (doc.sliceString(sp.from - 1, sp.from) === " ") return { from: sp.from - 1, to: sp.to };
  return { from: sp.from, to: sp.to };
}

const latin = (c: string | undefined) => !!c && /[A-Za-z0-9.,!?;:)"']/.test(c);

/** One removal plus one insertion at the drop caret; the moved span keeps its words, others map normally. */
export function moveSentence(view: EditorView, threadId: string, target: number): boolean {
  let pos = target;
  const state = view.state;
  const doc = state.doc;
  const spans = state.field(spansField);
  const sp = spans.find((s) => s.threadId === threadId);
  if (!sp) return false;
  const rem = removalRange(state, sp);
  if (!rem || (pos > rem.from && pos < rem.to)) return false;
  // Never split another taken sentence.
  const inside = spans.find((s) => s.threadId !== threadId && pos > s.from && pos < s.to);
  if (inside) pos = pos - inside.from <= inside.to - pos ? inside.from : inside.to;
  const text = doc.sliceString(sp.from, sp.to);
  // The only added text: a space where the sentence would otherwise touch a Latin word,
  // judged on the text as it reads once the sentence has been taken out.
  const removal = state.changes({ from: rem.from, to: rem.to });
  const rest = removal.apply(doc);
  const at = removal.mapPos(pos, -1);
  const prefix = latin(rest.sliceString(at - 1, at)) && latin(text[0]) ? " " : "";
  const suffix = latin(rest.sliceString(at, at + 1)) && latin(text[text.length - 1]) ? " " : "";
  const changes = state.changes([
    { from: rem.from, to: rem.to },
    { from: pos, insert: prefix + text + suffix },
  ]);
  // Dropping back where it came from changes nothing.
  if (changes.apply(doc).eq(doc)) return false;
  const start = changes.mapPos(pos, -1) + prefix.length;
  const next = spans.map((s) =>
    s.threadId === threadId
      ? { ...s, from: start, to: start + text.length }
      : { ...s, from: changes.mapPos(s.from, 1), to: changes.mapPos(s.to, -1) },
  );
  view.dispatch({
    changes,
    effects: setSpans.of(next),
    annotations: [external.of(true), Transaction.addToHistory.of(false)],
  });
  return true;
}

interface Dragging {
  view: EditorView;
  id: string;
  fade: HTMLStyleElement;
}

let dragging: Dragging | null = null;

function endDrag(): Dragging | null {
  const d = dragging;
  dragging = null;
  d?.fade.remove();
  d?.view.dom.classList.remove("is-carrying");
  return d;
}

/** Screen box of the caret at `pos`, measured on the sentence element when `pos` is a sentence edge. */
function caretBox(view: EditorView, pos: number): { left: number; top: number; bottom: number } | null {
  for (const s of view.state.field(spansField)) {
    if (pos !== s.from && pos !== s.to) continue;
    const el = view.contentDOM.querySelector(`.cm-taken[data-thread="${CSS.escape(s.threadId)}"]`);
    const rects = el ? [...el.getClientRects()] : [];
    if (!rects.length) break;
    const r = pos === s.to ? rects[rects.length - 1] : rects[0];
    // After a sentence the caret sits in its trailing gap, as the editor's own caret does.
    return pos === s.to ? { left: r.right + 3, top: r.top, bottom: r.bottom } : { left: r.left - 3, top: r.top, bottom: r.bottom };
  }
  return view.coordsAtPos(pos, -1) ?? view.coordsAtPos(pos, 1);
}

function startDrag(e: DragEvent, view: EditorView): void {
  const el = (e.target as HTMLElement).closest?.<HTMLElement>(".cm-taken[data-thread]");
  const id = el?.dataset.thread;
  const span = id ? view.state.field(spansField).find((s) => s.threadId === id) : undefined;
  if (!id || !span || !e.dataTransfer) return;
  endDrag();
  // Safari starts a drag only when some data is set.
  e.dataTransfer.setData("text/plain", view.state.doc.sliceString(span.from, span.to));
  e.dataTransfer.effectAllowed = "move";
  // The sentence stays faded in place while it is being carried.
  const fade = document.createElement("style");
  fade.textContent = `.cm-editor .cm-taken[data-thread="${CSS.escape(id)}"] { opacity: 0.35; }`;
  document.head.appendChild(fade);
  dragging = { view, id, fade };
  // Only the drop caret shows while carrying, not the editor's own caret as well.
  view.dom.classList.add("is-carrying");
}

const dragPlugin = ViewPlugin.fromClass(
  class {
    caret: HTMLElement | null = null;
    constructor(readonly view: EditorView) {
      view.contentDOM.addEventListener("dragstart", this.start);
      view.contentDOM.addEventListener("dragend", this.end);
      view.contentDOM.addEventListener("dragleave", this.leave);
    }
    start = (e: DragEvent) => startDrag(e, this.view);
    end = () => {
      endDrag();
      this.hide();
    };
    /** True when this editor handled the event (CodeMirror then prevents the default). */
    over(e: DragEvent): boolean {
      if (!dragging || !e.dataTransfer) return false;
      // Accept the drop only inside the sentence's own day; elsewhere the pointer shows "no drop".
      const own = dragging.view === this.view;
      e.dataTransfer.dropEffect = own ? "move" : "none";
      if (own) this.show(this.view.posAtCoords({ x: e.clientX, y: e.clientY }, false));
      return own;
    }
    leave = (e: DragEvent) => {
      if (!this.view.contentDOM.contains(e.relatedTarget as Node | null)) this.hide();
    };
    drop(e: DragEvent): boolean {
      this.hide();
      const d = endDrag();
      if (!d) return false;
      // Sentences move within their own day only.
      if (d.view === this.view) moveSentence(this.view, d.id, this.view.posAtCoords({ x: e.clientX, y: e.clientY }, false));
      return true;
    }
    show(pos: number) {
      const box = caretBox(this.view, pos);
      if (!box) return this.hide();
      if (!this.caret) {
        this.caret = document.createElement("div");
        this.caret.className = "drop-caret";
        document.body.appendChild(this.caret);
      }
      this.caret.style.transform = `translate(${Math.round(box.left - 1)}px, ${Math.round(box.top)}px)`;
      this.caret.style.height = `${Math.round(box.bottom - box.top)}px`;
    }
    hide() {
      this.caret?.remove();
      this.caret = null;
    }
    destroy() {
      this.view.contentDOM.removeEventListener("dragstart", this.start);
      this.view.contentDOM.removeEventListener("dragend", this.end);
      this.view.contentDOM.removeEventListener("dragleave", this.leave);
      this.hide();
      if (dragging?.view === this.view) endDrag();
    }
  },
);

export const sentenceDrag = [
  dragPlugin,
  // Through CodeMirror's handler chain so its own drop (which would insert the text again) never runs.
  EditorView.domEventHandlers({
    dragover: (e, view) => view.plugin(dragPlugin)?.over(e) ?? false,
    drop: (e, view) => view.plugin(dragPlugin)?.drop(e) ?? false,
  }),
];
