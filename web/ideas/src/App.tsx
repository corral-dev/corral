// Page shell: top bar, "pick up here", the day sections and the discussion panel.
import { useEffect, useState } from "preact/hooks";
import { DaySection } from "./canvas/DaySection";
import { Panel } from "./discussion/Panel";
import { relativeTime, t } from "./i18n";
import { Thread, ThreadStatus, hasUnreadResult, threadStatus } from "./model";
import type { PreviewCoordinator } from "./preview";
import { Store, useStore } from "./store";
import iconUrl from "./assets/corral-icon.png";

interface Props {
  store: Store;
  coordinator: PreviewCoordinator;
  settleMs: number;
}

export function App({ store, coordinator, settleMs }: Props) {
  const state = useStore(store);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  const threads = Object.values(state.threads);
  const openThread = state.openThreadId ? state.threads[state.openThreadId] ?? null : null;
  return (
    <div class={`app${openThread ? " has-panel" : ""}`}>
      <TopBar store={store} threads={threads} />
      <main class="stage">
        <div class="page">
          <PickUp store={store} threads={threads} now={now} />
          {state.sections.map((s) => (
            <DaySection
              key={s.day}
              store={store}
              state={state}
              section={s}
              now={now}
              settleMs={settleMs}
              onSettled={(day, from, to) => coordinator.onSettled(day, from, to)}
            />
          ))}
        </div>
      </main>
      <Panel store={store} thread={openThread} now={now} onReply={(id, text) => coordinator.onReply(id, text)} />
    </div>
  );
}

function TopBar({ store, threads }: { store: Store; threads: Thread[] }) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  const by = (s: ThreadStatus) => threads.filter((th) => threadStatus(th) === s);
  const waiting = by("waiting");
  const attention = by("attention");
  const working = by("working");
  const chips: [ThreadStatus, Thread[], string][] = [];
  if (waiting.length) chips.push(["waiting", waiting, t("countWaiting", { n: waiting.length })]);
  if (attention.length) chips.push(["attention", attention, t("countAttention", { n: attention.length })]);
  if (working.length) chips.push(["working", working, t("countWorking", { n: working.length })]);
  return (
    <header class={`topbar${scrolled ? " is-scrolled" : ""}`}>
      <div class="brand">
        <img src={iconUrl} alt="" width={24} height={24} />
        <span>{t("appName")}</span>
      </div>
      <nav class="topbar-status" aria-live="polite">
        {chips.map(([s, list, text]) => (
          <button key={s} class={`chip is-${s} is-button`} onClick={() => store.openThread(newest(list).id)}>
            <span class="dot" />
            {text}
          </button>
        ))}
      </nav>
    </header>
  );
}

function newest(list: Thread[]): Thread {
  return [...list].sort((a, b) => b.createdAt - a.createdAt)[0];
}

const PICKUP_RANK: Partial<Record<ThreadStatus, number>> = { waiting: 0, attention: 1 };

function PickUp({ store, threads, now }: { store: Store; threads: Thread[]; now: Date }) {
  const items = threads
    .map((th) => ({ th, status: threadStatus(th), fresh: hasUnreadResult(th) }))
    .filter((x) => x.status === "waiting" || x.status === "attention" || x.fresh)
    .sort((a, b) => (PICKUP_RANK[a.status] ?? 2) - (PICKUP_RANK[b.status] ?? 2) || last(b.th) - last(a.th))
    .slice(0, 6);
  if (!items.length) return null;
  return (
    <section class="pickup" aria-label={t("pickUp")}>
      <h2 class="eyebrow">{t("pickUp")}</h2>
      <ul>
        {items.map(({ th, status, fresh }) => (
          <li key={th.id}>
            <button
              class={`pickup-row is-${fresh && status !== "waiting" && status !== "attention" ? "done" : status}`}
              onClick={() => store.openThread(th.id)}
              onMouseEnter={() => store.hover(th.id)}
              onMouseLeave={() => store.hover(null)}
            >
              <span class="dot" />
              <span class="pickup-quote">{th.quote}</span>
              <span class="pickup-label">{fresh && status !== "waiting" && status !== "attention" ? t("newResult") : t(`status.${status}`)}</span>
              <span class="pickup-time">{relativeTime(last(th), now.getTime())}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function last(th: Thread): number {
  return th.messages.reduce((m, x) => Math.max(m, x.at), th.createdAt);
}
