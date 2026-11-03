// One calendar day: a header that folds the day and, when open, its own editor with
// a margin rail of discussion annotations, settle countdowns and reading indicators.
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { dayLabel, daySubLabel } from "../dates";
import { t } from "../i18n";
import { DaySection as Section, Thread, ThreadStatus, hasUnreadResult, isUnread, threadStatus } from "../model";
import type { State, Store } from "../store";
import { Chevron } from "../ui/icons";
import { PendingBlock, SpanView, editorExtensions, external, setSpans, spansField } from "./editor";

interface Props {
  store: Store;
  state: State;
  section: Section;
  now: Date;
  settleMs: number;
  onSettled: (day: string, from: number, to: number) => void;
}

type RailItem =
  | { kind: "span"; key: string; top: number; thread: Thread; status: ThreadStatus; active: boolean; unread: boolean }
  | { kind: "pending"; key: string; top: number; fraction: number; seconds: number }
  | { kind: "reading"; key: string; top: number };

const RAIL_STATUSES: ReadonlySet<ThreadStatus> = new Set(["waiting", "attention", "working"]);
const PILL_GAP = 6;
const PILL_HEIGHT = 26;

function spanViews(state: State, section: Section): SpanView[] {
  return section.spans
    .filter((s) => state.threads[s.threadId])
    .map((s) => {
      const th = state.threads[s.threadId];
      return {
        threadId: s.threadId,
        from: s.from,
        to: s.to,
        status: threadStatus(th),
        active: state.openThreadId === s.threadId || state.hoverThreadId === s.threadId,
        unread: isUnread(th),
      };
    });
}

function sameSpans(a: SpanView[], b: SpanView[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((x, i) => {
    const y = b[i];
    return x.threadId === y.threadId && x.from === y.from && x.to === y.to && x.status === y.status && x.active === y.active && x.unread === y.unread;
  });
}

export function DaySection({ store, state, section, now, settleMs, onSettled }: Props) {
  const open = store.isOpen(section, now);
  const label = dayLabel(section.day, now);
  const sub = daySubLabel(section.day, now);
  const threads = section.spans.map((s) => state.threads[s.threadId]).filter(Boolean);

  return (
    <section class={`day${open ? " is-open" : " is-folded"}${label === t("today") ? " is-today" : ""}`} aria-label={`${label}, ${sub}`}>
      <button class="day-head" aria-expanded={open} onClick={() => store.toggleSection(section.day, now)} title={open ? t("fold") : t("unfold")}>
        <Chevron open={open} />
        <span class="day-label">{label}</span>
        <span class="day-sub">{sub}</span>
        {!open && <FoldedSummary threads={threads} />}
      </button>
      {open && <DayEditor store={store} state={state} section={section} settleMs={settleMs} onSettled={onSettled} isToday={label === t("today")} />}
    </section>
  );
}

function FoldedSummary({ threads }: { threads: Thread[] }) {
  const counts: Partial<Record<ThreadStatus, number>> = {};
  for (const th of threads) {
    const s = threadStatus(th);
    counts[s] = (counts[s] ?? 0) + 1;
  }
  const chips: [ThreadStatus, string][] = [];
  if (counts.waiting) chips.push(["waiting", t("countWaiting", { n: counts.waiting })]);
  if (counts.attention) chips.push(["attention", t("countAttention", { n: counts.attention })]);
  if (counts.working) chips.push(["working", t("countWorking", { n: counts.working })]);
  return (
    <span class="day-summary">
      {chips.map(([s, text]) => (
        <span key={s} class={`chip is-${s}`}>
          <span class="dot" />
          {text}
        </span>
      ))}
      <span class="day-count">{t("countItems", { n: threads.length })}</span>
    </span>
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
  const [pending, setPending] = useState<PendingBlock[]>([]);
  const [layoutTick, setLayoutTick] = useState(0);
  const [rail, setRail] = useState<RailItem[]>([]);
  const day = section.day;

  // Create the editor once per mount; the store is the persisted copy of its text.
  useEffect(() => {
    if (!host.current) return;
    let frame = 0;
    const v = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: section.text,
        extensions: editorExtensions({
          placeholder: isToday ? t("placeholder") : "",
          settleMs,
          onSettled: (from, to) => onSettled(day, from, to),
          onPending: setPending,
          onDocChanged: (text, spans) => store.updateSection(day, text, spans.map(({ threadId, from, to }) => ({ threadId, from, to }))),
          onLayout: () => {
            cancelAnimationFrame(frame);
            frame = requestAnimationFrame(() => setLayoutTick((n) => n + 1));
          },
          onOpenThread: (id) => store.openThread(id),
          onHoverThread: (id) => store.hover(id),
        }),
      }),
    });
    v.dispatch({ effects: setSpans.of(spanViews(store.state, section)), annotations: external.of(true) });
    view.current = v;
    const ro = new ResizeObserver(() => setLayoutTick((n) => n + 1));
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

  // Store → editor: statuses, activity and new take-ups.
  const views = spanViews(state, section);
  useEffect(() => {
    const v = view.current;
    if (!v) return;
    if (!sameSpans(v.state.field(spansField), views)) {
      v.dispatch({ effects: setSpans.of(views), annotations: external.of(true) });
    }
  });

  // Margin rail positions follow the rendered text.
  const readingHere = state.reading.filter((r) => r.day === day);
  useLayoutEffect(() => {
    const v = view.current;
    if (!v) return;
    const base = v.dom.getBoundingClientRect().top;
    const items: RailItem[] = [];
    const topOf = (pos: number) => {
      const c = v.coordsAtPos(Math.min(pos, v.state.doc.length), 1);
      if (!c) return null;
      return c.top - base + (c.bottom - c.top - PILL_HEIGHT) / 2;
    };
    for (const s of v.state.field(spansField)) {
      const th = state.threads[s.threadId];
      // The rail only carries what needs a look; done and withdrawn text already shows its state.
      if (!th || !(RAIL_STATUSES.has(s.status) || hasUnreadResult(th))) continue;
      const top = topOf(s.from);
      if (th && top !== null) items.push({ kind: "span", key: s.threadId, top, thread: th, status: s.status, active: s.active, unread: s.unread });
    }
    for (const r of readingHere) {
      const top = topOf(r.from);
      if (top !== null) items.push({ kind: "reading", key: `r${r.from}`, top });
    }
    for (const p of pending) {
      if (readingHere.some((r) => r.from >= p.from && r.from <= p.to)) continue;
      const top = topOf(p.from);
      if (top !== null) items.push({ kind: "pending", key: `p${p.from}`, top, fraction: p.remainingMs / settleMs, seconds: Math.ceil(p.remainingMs / 1000) });
    }
    items.sort((a, b) => a.top - b.top);
    let floor = -Infinity;
    for (const it of items) {
      it.top = Math.max(it.top, floor);
      floor = it.top + PILL_HEIGHT + PILL_GAP;
    }
    setRail(items);
  }, [layoutTick, pending, state.threads, state.openThreadId, state.hoverThreadId, readingHere.length, section.spans]);

  return (
    <div class="day-body">
      <div class="editor-host" ref={host} />
      <div class="rail" aria-label={t("tasks")}>
        {rail.map((it) => (
          <RailEntry key={it.key} item={it} store={store} />
        ))}
      </div>
    </div>
  );
}

function RailEntry({ item, store }: { item: RailItem; store: Store }) {
  const style = { transform: `translateY(${item.top}px)` };
  if (item.kind === "pending") {
    const r = 6;
    const c = 2 * Math.PI * r;
    return (
      <span class="rail-item settle" style={style} title={t("sendsIn", { s: item.seconds })} role="status" aria-label={t("sendsIn", { s: item.seconds })}>
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <circle cx="8" cy="8" r={r} class="settle-track" />
          <circle cx="8" cy="8" r={r} class="settle-fill" style={{ strokeDasharray: `${c}`, strokeDashoffset: `${c * (1 - item.fraction)}` }} />
        </svg>
      </span>
    );
  }
  if (item.kind === "reading") {
    return (
      <span class="rail-item pill is-reading" style={style} role="status">
        <span class="reading-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        {t("reading")}
      </span>
    );
  }
  const { thread, status, active, unread } = item;
  const live = thread.tasks.find((x) => x.state === "running" || x.state === "review");
  const fresh = hasUnreadResult(thread);
  return (
    <button
      class={`rail-item pill is-${status}${active ? " is-active" : ""}`}
      style={style}
      onClick={() => store.openThread(thread.id)}
      onMouseEnter={() => store.hover(thread.id)}
      onMouseLeave={() => store.hover(null)}
      aria-label={`${t(`status.${status}`)}: ${thread.quote}`}
    >
      <span class="dot" />
      <span class={`pill-label${fresh ? " is-fresh" : ""}`}>{fresh ? t("newResult") : t(`status.${status}`)}</span>
      {status === "working" && live?.assistant && <span class="pill-meta">{live.assistant}</span>}
      {unread && !fresh && status !== "waiting" && <span class="unread" aria-label={t("unread")} />}
    </button>
  );
}
