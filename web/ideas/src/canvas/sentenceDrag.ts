// Drag a taken sentence to another place in the same day (owner, 2026-11-03: no grip; the
// sentence itself is the handle). A mouse drags as soon as it moves; a finger or pen must hold
// first so an ordinary swipe still scrolls. A press without a drag still opens the
// discussion. The sentence lands exactly at the caret position under the pointer; a move
// never creates paragraphs or blank lines (owner, 2026-11-03).
import { EditorState, Transaction } from "@codemirror/state";
import { EditorView, ViewPlugin } from "@codemirror/view";
import { SpanView, external, paragraphs, setSpans, spansField } from "./editor";

/** Touch and pen hold before lifting; a mouse lifts on movement instead. */
const TOUCH_HOLD_MS = 380;
const SLOP_PX = 8;
const MOUSE_SLOP_PX = 4;

interface Drop {
  pos: number;
}

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

/** The caret position under the pointer, kept out of taken sentences; null when the move would change nothing. */
function dropAt(view: EditorView, x: number, y: number, moving: SpanView): Drop | null {
  let pos = view.posAtCoords({ x, y }, false);
  const other = view.state.field(spansField).find((s) => s.threadId !== moving.threadId && pos > s.from && pos < s.to);
  if (other) {
    const a = view.coordsAtPos(other.from, 1);
    const b = view.coordsAtPos(other.to, -1);
    const da = a ? Math.hypot(a.left - x, (a.top + a.bottom) / 2 - y) : Infinity;
    const db = b ? Math.hypot(b.left - x, (b.top + b.bottom) / 2 - y) : Infinity;
    pos = da <= db ? other.from : other.to;
  }
  const rem = removalRange(view.state, moving);
  if (!rem || (pos >= rem.from && pos <= rem.to)) return null;
  return { pos };
}

const latin = (c: string | undefined) => !!c && /[A-Za-z0-9.,!?;:)"']/.test(c);

/** One removal plus one insertion at the drop caret; the moved span keeps its words, others map normally. */
export function moveSentence(view: EditorView, threadId: string, drop: Drop): boolean {
  const state = view.state;
  const doc = state.doc;
  const spans = state.field(spansField);
  const sp = spans.find((s) => s.threadId === threadId);
  if (!sp) return false;
  const rem = removalRange(state, sp);
  if (!rem || (drop.pos >= rem.from && drop.pos <= rem.to)) return false;
  const text = doc.sliceString(sp.from, sp.to);
  // The only added text: a space where the sentence would otherwise touch a Latin word.
  const left = doc.sliceString(drop.pos - 1, drop.pos);
  const right = doc.sliceString(drop.pos, drop.pos + 1);
  const prefix = latin(left) && latin(text[0]) ? " " : "";
  const suffix = latin(right) && latin(text[text.length - 1]) ? " " : "";
  const changes = state.changes([
    { from: rem.from, to: rem.to },
    { from: drop.pos, insert: prefix + text + suffix },
  ]);
  const start = changes.mapPos(drop.pos, -1) + prefix.length;
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

/** Set when a drag ends so the click that follows does not open the discussion. */
export let suppressClickUntil = 0;

export const sentenceDrag = ViewPlugin.fromClass(
  class {
    pending: { id: string; x: number; y: number; timer: number; pointerId: number; mouse: boolean } | null = null;
    lifted: { id: string; span: SpanView; drop: Drop | null } | null = null;
    ghost: HTMLElement | null = null;
    marker: HTMLElement | null = null;
    liftStyle: HTMLStyleElement | null = null;

    constructor(readonly view: EditorView) {
      view.contentDOM.addEventListener("pointerdown", this.down);
      window.addEventListener("pointermove", this.move, { passive: false });
      window.addEventListener("pointerup", this.up);
      window.addEventListener("pointercancel", this.cancel);
      window.addEventListener("touchmove", this.touchMove, { passive: false });
      window.addEventListener("keydown", this.key);
      view.contentDOM.addEventListener("contextmenu", this.menu);
      document.addEventListener("selectstart", this.select);
    }

    select = (e: Event) => {
      // A press on a taken sentence is a tap or a drag, never the start of a text selection
      // (including iOS falling through to nearby editable text).
      const t = e.target instanceof Element ? e.target : (e.target as Node | null)?.parentElement;
      if (this.pending || this.lifted || t?.closest(".cm-taken")) e.preventDefault();
    };

    down = (e: PointerEvent) => {
      if (e.button !== 0) return;
      const el = (e.target as HTMLElement).closest<HTMLElement>(".cm-taken[data-thread]");
      if (!el?.dataset.thread) return;
      const id = el.dataset.thread;
      const mouse = e.pointerType === "mouse";
      const timer = mouse ? 0 : window.setTimeout(() => this.lift(), TOUCH_HOLD_MS);
      this.pending = { id, x: e.clientX, y: e.clientY, pointerId: e.pointerId, mouse, timer };
    };

    lift(at?: { x: number; y: number }) {
      const p = this.pending;
      this.pending = null;
      if (!p) return;
      const span = this.view.state.field(spansField).find((s) => s.threadId === p.id);
      if (!span) return;
      this.lifted = { id: p.id, span, drop: null };
      window.getSelection()?.removeAllRanges();
      this.view.dom.classList.add("is-sentence-dragging");
      // A rule keyed by id survives the editor re-rendering the sentence during the drag.
      this.liftStyle = document.createElement("style");
      this.liftStyle.textContent = `.cm-editor .cm-taken[data-thread="${CSS.escape(p.id)}"] { opacity: 0.35; }`;
      document.head.appendChild(this.liftStyle);
      const text = this.view.state.doc.sliceString(span.from, span.to);
      this.ghost = document.createElement("div");
      this.ghost.className = "drag-ghost";
      this.ghost.textContent = text.length > 28 ? `${text.slice(0, 27)}…` : text;
      document.body.appendChild(this.ghost);
      this.marker = document.createElement("div");
      this.marker.className = "drop-marker";
      document.body.appendChild(this.marker);
      this.place(at?.x ?? p.x, at?.y ?? p.y);
      if (!p.mouse) navigator.vibrate?.(8);
    }

    move = (e: PointerEvent) => {
      if (this.pending) {
        const p = this.pending;
        if (e.pointerId !== p.pointerId) return;
        const dist = Math.hypot(e.clientX - p.x, e.clientY - p.y);
        // Mouse: moving is the drag. Touch: moving before the hold is a scroll, so give up.
        if (p.mouse && dist > MOUSE_SLOP_PX) {
          e.preventDefault();
          this.lift({ x: e.clientX, y: e.clientY });
        } else if (!p.mouse && dist > SLOP_PX) this.clearPending();
        return;
      }
      if (!this.lifted) return;
      e.preventDefault();
      this.place(e.clientX, e.clientY);
      // Keep the page moving when the pointer reaches the top or bottom edge.
      const edge = 72;
      if (e.clientY < edge) window.scrollBy(0, -12);
      else if (e.clientY > window.innerHeight - edge) window.scrollBy(0, 12);
    };

    place(x: number, y: number) {
      if (!this.lifted || !this.ghost || !this.marker) return;
      // Keep the ghost fully on screen near the right and bottom edges.
      const gw = this.ghost.offsetWidth;
      const gh = this.ghost.offsetHeight;
      const gx = Math.min(x + 14, window.innerWidth - gw - 8);
      const gy = Math.min(y + 16, window.innerHeight - gh - 8);
      this.ghost.style.transform = `translate(${Math.max(8, gx)}px, ${gy}px)`;
      const drop = dropAt(this.view, x, y, this.lifted.span);
      this.lifted.drop = drop;
      if (!drop) {
        this.marker.style.display = "none";
        return;
      }
      const c = this.view.coordsAtPos(drop.pos, 1) ?? this.view.coordsAtPos(drop.pos, -1);
      if (!c) {
        this.marker.style.display = "none";
        return;
      }
      this.marker.style.display = "block";
      this.marker.style.transform = `translate(${c.left - 1}px, ${c.top}px)`;
      this.marker.style.height = `${c.bottom - c.top}px`;
    }

    up = () => {
      if (this.pending) {
        this.clearPending();
        return;
      }
      if (!this.lifted) return;
      const { id, drop } = this.lifted;
      this.finish();
      suppressClickUntil = Date.now() + 400;
      if (drop) moveSentence(this.view, id, drop);
    };

    cancel = () => {
      this.clearPending();
      if (this.lifted) this.finish();
    };

    key = (e: KeyboardEvent) => {
      if (e.key === "Escape" && this.lifted) this.finish();
    };

    touchMove = (e: TouchEvent) => {
      // Once lifted, the finger moves the sentence instead of scrolling the page.
      if (this.lifted) e.preventDefault();
    };

    menu = (e: Event) => {
      if (this.pending || this.lifted || (e.target as HTMLElement).closest(".cm-taken")) e.preventDefault();
    };

    clearPending() {
      if (this.pending) clearTimeout(this.pending.timer);
      this.pending = null;
    }

    finish() {
      if (!this.lifted) return;
      this.liftStyle?.remove();
      this.liftStyle = null;
      this.view.dom.classList.remove("is-sentence-dragging");
      this.ghost?.remove();
      this.marker?.remove();
      this.ghost = null;
      this.marker = null;
      this.lifted = null;
    }

    destroy() {
      this.clearPending();
      this.finish();
      this.view.contentDOM.removeEventListener("pointerdown", this.down);
      window.removeEventListener("pointermove", this.move);
      window.removeEventListener("pointerup", this.up);
      window.removeEventListener("pointercancel", this.cancel);
      window.removeEventListener("touchmove", this.touchMove);
      window.removeEventListener("keydown", this.key);
      this.view.contentDOM.removeEventListener("contextmenu", this.menu);
      document.removeEventListener("selectstart", this.select);
    }
  },
);
