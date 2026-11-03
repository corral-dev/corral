// One calendar day: a header that folds the day and, when open, its own editor.
// Status lives on the owner's words only (a marker at the sentence end); there is no
// separate rail, countdown or reading indicator (owner correction 2026-11-03).
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { useEffect, useRef } from "react";
import { dayLabel } from "../dates";
import { t } from "../i18n";
import { DaySection as Section, isUnread, presence } from "../model";
import type { State, Store } from "../store";
import { Chevron } from "../ui/icons";
import { SpanView, editorExtensions, external, setSpans, spansField } from "./editor";

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
  const day = section.day;

  // Create the editor once per mount; the store is the persisted copy of its text.
  useEffect(() => {
    if (!host.current) return;
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
          onLayout: () => {},
          onOpenThread: (id) => store.openThread(id),
          onHoverThread: (id) => store.hover(id),
        }),
      }),
    });
    v.dispatch({ effects: setSpans.of(spanViews(store.state, section)), annotations: external.of(true) });
    view.current = v;
    return () => {
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
    </div>
  );
}
