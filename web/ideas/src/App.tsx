// Page shell: a quiet top bar, the day sections and the discussion sheet.
// The account only scopes saved drafts; sign-in and sign-out live at the unified login.
import { useEffect, useState } from "react";
import { DayCanvas } from "./canvas/DayCanvas";
import { watchCalendar } from "./calendar";
import { Discussion } from "./discussion/Discussion";
import { t } from "./i18n";
import type { PreviewCoordinator } from "./preview";
import { Store, useStore } from "./store";
import iconUrl from "./assets/corral-icon.png";
import { ExportButton } from "./input/ExportButton";
import { PersistenceNotice } from "./input/RichContent";

interface Props {
  store: Store;
  coordinator: PreviewCoordinator;
  settleMs: number;
}

export function App({ store, coordinator, settleMs }: Props) {
  const state = useStore(store);
  useEffect(() => watchCalendar((now) => store.refreshDay(now)), [store]);

  const openThread = state.openThreadId ? (state.threads[state.openThreadId] ?? null) : null;
  return (
    <div className={`app${openThread ? " has-panel" : ""}`}>
      <TopBar store={store} />
      <PersistenceNotice store={store} />
      <main className="stage">
        <DayCanvas
          store={store}
          state={state}
          settleMs={settleMs}
          onSettled={(day, from, to) => coordinator.onSettled(day, from, to)}
        />
      </main>
      <Discussion store={store} thread={openThread} onReply={(id, text, target, draftKey, operationId) => coordinator.onReply(id, text, target, draftKey, operationId)} />
    </div>
  );
}

function TopBar({ store }: { store: Store }) {
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
      <ExportButton store={store} />
    </header>
  );
}
