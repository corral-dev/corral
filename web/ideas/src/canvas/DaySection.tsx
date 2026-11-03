// One calendar day: a header that folds the day and, when open, its own editor.
// Status lives on the owner's words only (a marker at the sentence end); there is no
// separate rail, countdown or reading indicator (owner correction 2026-11-03).
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { dayLabel } from "../dates";
import { t } from "../i18n";
import { DaySection as Section, isUnread, presence } from "../model";
import type { State, Store } from "../store";
import { Chevron } from "../ui/icons";
import { Paragraph, SpanView, editorExtensions, external, moveParagraph, paragraphs, setDragging, setSpans, spansField } from "./editor";

interface Props {
  store: Store;
  state: State;
  section: Section;
  now: Date;
  settleMs: number;
  onSettled: (day: string, from: number, to: number) => void;
}

const FRESH_MS = 2500;

function spanViews(state: State, section: Section): SpanView[] {
  const now = Date.now();
  return section.spans
    .filter((s) => state.threads[s.threadId])
    .map((s) => {
      const th = state.threads[s.threadId];
      return {
        threadId: s.threadId,
        from: s.from,
        to: s.to,
        status: presence(th),
        active: state.openThreadId === s.threadId || state.hoverThreadId === s.threadId,
        unread: isUnread(th),
        fresh: now - th.createdAt < FRESH_MS,
      };
    });
}

function sameSpans(a: SpanView[], b: SpanView[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((x, i) => {
    const y = b[i];
    return (
      x.threadId === y.threadId &&
      x.from === y.from &&
      x.to === y.to &&
      x.status === y.status &&
      x.active === y.active &&
      x.unread === y.unread &&
      x.fresh === y.fresh
    );
  });
}

export function DaySection({ store, state, section, now, settleMs, onSettled }: Props) {
  const open = store.isOpen(section, now);
  const label = dayLabel(section.day, now);
  const isToday = label === t("today");

  return (
    <section className={`day${open ? " is-open" : " is-folded"}${isToday ? " is-today" : ""}`} aria-label={label}>
      <button
        className="day-head"
        aria-expanded={open}
        onClick={() => store.toggleSection(section.day, now)}
        title={open ? t("fold") : t("unfold")}
      >
        <Chevron open={open} />
        <span className="day-label">{label}</span>
      </button>
      {open && <DayEditor store={store} state={state} section={section} settleMs={settleMs} onSettled={onSettled} isToday={isToday} />}
    </section>
  );
}

interface EditorProps {
  store: Store;
  state: State;
  section: Section;
  settleMs: number;
  isToday: boolean;
  onSettled: (day: string, from: number, to: number) => void;
}

function DayEditor({ store, state, section, settleMs, isToday, onSettled }: EditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const [layout, setLayout] = useState(0);
  const day = section.day;

  // Create the editor once per mount; the store is the persisted copy of its text.
  useEffect(() => {
    if (!host.current) return;
    let frame = 0;
    const relayout = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setLayout((n) => n + 1));
    };
    const v = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: section.text,
        extensions: editorExtensions({
          placeholder: isToday ? t("placeholder") : "",
          settleMs,
          onSettled: (from, to) => onSettled(day, from, to),
          onPending: () => {},
          onDocChanged: (text, spans) => store.updateSection(day, text, spans.map(({ threadId, from, to }) => ({ threadId, from, to }))),
          onLayout: relayout,
          onOpenThread: (id) => store.openThread(id),
          onHoverThread: (id) => store.hover(id),
        }),
      }),
    });
    v.dispatch({ effects: setSpans.of(spanViews(store.state, section)), annotations: external.of(true) });
    view.current = v;
    const ro = new ResizeObserver(relayout);
    ro.observe(host.current);
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      v.destroy();
      view.current = null;
    };
    // The editor owns its document after creation; re-creating it would drop the caret.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [day]);

  // Store → editor: presence, activity and new take-ups.
  const views = spanViews(state, section);
  useEffect(() => {
    const v = view.current;
    if (!v) return;
    if (!sameSpans(v.state.field(spansField), views)) {
      v.dispatch({ effects: setSpans.of(views), annotations: external.of(true) });
    }
  });

  return (
    <div className="day-body">
      <div className="editor-host" ref={host} />
      <Grips view={view} host={host} layout={layout} spanKey={views.map((v) => `${v.threadId}:${v.from}`).join(",")} />
    </div>
  );
}

interface Box {
  index: number;
  top: number;
  bottom: number;
}

interface Grip extends Box {
  key: string;
}

/** Measured paragraph boxes relative to the editor host. */
function measure(v: EditorView, host: HTMLElement): { boxes: Box[]; grips: Grip[]; ps: Paragraph[] } {
  const base = host.getBoundingClientRect().top;
  const ps = paragraphs(v.state);
  const spans = v.state.field(spansField);
  const boxes: Box[] = [];
  const grips: Grip[] = [];
  ps.forEach((p, index) => {
    const a = v.coordsAtPos(p.from, 1);
    const b = v.coordsAtPos(p.to, -1);
    if (!a || !b) return;
    const box = { index, top: a.top - base, bottom: b.bottom - base };
    boxes.push(box);
    const owner = spans.find((sp) => sp.from >= p.from && sp.to <= p.to);
    if (owner) grips.push({ ...box, key: owner.threadId });
  });
  return { boxes, grips, ps };
}

/** Insertion index for a pointer at `y`: before the first paragraph whose middle is below it. */
function dropIndex(boxes: Box[], y: number): number {
  for (const b of boxes) if (y < (b.top + b.bottom) / 2) return b.index;
  return boxes.length;
}

function Grips({
  view,
  host,
  layout,
  spanKey,
}: {
  view: React.RefObject<EditorView | null>;
  host: React.RefObject<HTMLDivElement | null>;
  layout: number;
  spanKey: string;
}) {
  const [grips, setGrips] = useState<Grip[]>([]);
  const [drag, setDrag] = useState<{ index: number; target: number; line: number } | null>(null);
  const boxesRef = useRef<Box[]>([]);

  useLayoutEffect(() => {
    const v = view.current;
    const h = host.current;
    if (!v || !h) return;
    const m = measure(v, h);
    boxesRef.current = m.boxes;
    setGrips(m.grips);
  }, [view, host, layout, spanKey]);

  const lineFor = (target: number) => {
    const boxes = boxesRef.current;
    if (!boxes.length) return 0;
    if (target >= boxes.length) return boxes[boxes.length - 1].bottom + 12;
    const b = boxes[target];
    const prev = boxes[target - 1];
    return prev ? (prev.bottom + b.top) / 2 : b.top - 12;
  };

  const start = (e: React.PointerEvent, g: Grip) => {
    const v = view.current;
    if (!v) return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const p = paragraphs(v.state)[g.index];
    v.dispatch({ effects: setDragging.of(p), annotations: external.of(true) });
    setDrag({ index: g.index, target: g.index, line: lineFor(g.index) });
  };
  const move = (e: React.PointerEvent) => {
    const h = host.current;
    if (!drag || !h) return;
    const y = e.clientY - h.getBoundingClientRect().top;
    const target = dropIndex(boxesRef.current, y);
    if (target !== drag.target) setDrag({ ...drag, target, line: lineFor(target) });
  };
  const end = (commit: boolean) => {
    const v = view.current;
    if (!drag || !v) return;
    const moved = commit && moveParagraph(v, drag.index, drag.target);
    if (!moved) v.dispatch({ effects: setDragging.of(null), annotations: external.of(true) });
    setDrag(null);
  };
  const keys = (e: React.KeyboardEvent, g: Grip) => {
    const v = view.current;
    if (!v) return;
    if (e.key === "ArrowUp" && moveParagraph(v, g.index, g.index - 1)) e.preventDefault();
    if (e.key === "ArrowDown" && moveParagraph(v, g.index, g.index + 2)) e.preventDefault();
  };
  const moving = drag && drag.target !== drag.index && drag.target !== drag.index + 1;

  return (
    <>
      {grips.map((g) => (
        <button
          key={g.key}
          className={`grip${drag?.index === g.index ? " is-dragging" : ""}`}
          style={{ top: `${g.top}px`, height: `${Math.min(29, Math.max(22, g.bottom - g.top))}px` }}
          aria-label={t("move")}
          title={t("move")}
          onPointerDown={(e) => start(e, g)}
          onPointerMove={move}
          onPointerUp={() => end(true)}
          onPointerCancel={() => end(false)}
          onKeyDown={(e) => keys(e, g)}
        >
          <svg viewBox="0 0 10 16" width="10" height="16" aria-hidden="true">
            {[3, 8, 13].map((y) => [2.5, 7.5].map((x) => <circle key={`${x}-${y}`} cx={x} cy={y} r="1.15" />))}
          </svg>
        </button>
      ))}
      {moving && <div className="drop-line" style={{ top: `${drag.line}px` }} />}
    </>
  );
}
