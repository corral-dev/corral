// Page shell: a quiet top bar, the day sections and the discussion sheet.
// The account only scopes saved drafts; sign-in and sign-out live at the unified login.
import { useEffect, useState } from "react";
import { DaySection } from "./canvas/DaySection";
import { Discussion } from "./discussion/Discussion";
import { t } from "./i18n";
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

  const openThread = state.openThreadId ? (state.threads[state.openThreadId] ?? null) : null;
  return (
    <div className={`app${openThread ? " has-panel" : ""}`}>
      <TopBar />
      <main className="stage">
        <div className="page">
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
      <Discussion store={store} thread={openThread} onReply={(id, text, target) => coordinator.onReply(id, text, target)} />
    </div>
  );
}

function TopBar() {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  return (
    <header className={`topbar${scrolled ? " is-scrolled" : ""}`}>
      <div className="brand">
        <img src={iconUrl} alt="" width={24} height={24} />
        <span>{t("appName")}</span>
      </div>
    </header>
  );
}
