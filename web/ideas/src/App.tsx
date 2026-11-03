// Page shell: a quiet top bar, "pick up here" when something needs the owner, the day
// sections and the discussion sheet.
import { useEffect, useState } from "react";
import { DaySection } from "./canvas/DaySection";
import { Discussion } from "./discussion/Discussion";
import { relativeTime, t } from "./i18n";
import { Account, signOutUrl } from "./identity";
import { Thread, hasUnreadResult, presence } from "./model";
import type { PreviewCoordinator } from "./preview";
import { Store, useStore } from "./store";
import iconUrl from "./assets/corral-icon.png";

interface Props {
  store: Store;
  coordinator: PreviewCoordinator;
  settleMs: number;
  account: Account | null;
}

export function App({ store, coordinator, settleMs, account }: Props) {
  const state = useStore(store);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  const threads = Object.values(state.threads);
  const openThread = state.openThreadId ? (state.threads[state.openThreadId] ?? null) : null;
  return (
    <div className={`app${openThread ? " has-panel" : ""}`}>
      <TopBar account={account} />
      <main className="stage">
        <div className="page">
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
      <Discussion store={store} thread={openThread} onReply={(id, text) => coordinator.onReply(id, text)} />
    </div>
  );
}

function TopBar({ account }: { account: Account | null }) {
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
      {account && <AccountMenu account={account} />}
    </header>
  );
}

function AccountMenu({ account }: { account: Account }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (e: Event) => {
      if (e instanceof KeyboardEvent && e.key !== "Escape") return;
      if (e instanceof PointerEvent && (e.target as HTMLElement).closest(".account")) return;
      setOpen(false);
    };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", close);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", close);
    };
  }, [open]);
  return (
    <div className="account">
      <button
        className="account-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`${t("account")}: ${account.email}`}
        title={account.email}
        onClick={() => setOpen(!open)}
      >
        {account.email.slice(0, 1).toUpperCase()}
      </button>
      {open && (
        <div className="account-menu" role="menu">
          <div className="account-email">{account.email}</div>
          <a className="account-item" role="menuitem" href={signOutUrl()}>
            {t("signOut")}
          </a>
        </div>
      )}
    </div>
  );
}

/** Only what needs the owner: questions waiting for them, then results they have not read. */
function PickUp({ store, threads, now }: { store: Store; threads: Thread[]; now: Date }) {
  const items = threads
    .map((th) => ({ th, waiting: presence(th) === "waiting", fresh: hasUnreadResult(th) }))
    .filter((x) => x.waiting || x.fresh)
    .sort((a, b) => Number(b.waiting) - Number(a.waiting) || last(b.th) - last(a.th))
    .slice(0, 5);
  if (!items.length) return null;
  return (
    <section className="pickup" aria-label={t("pickUp")}>
      <h2 className="eyebrow">{t("pickUp")}</h2>
      <ul>
        {items.map(({ th, waiting }) => (
          <li key={th.id}>
            <button className={`pickup-row is-${waiting ? "waiting" : "done"}`} onClick={() => store.openThread(th.id)}>
              <span className="dot" />
              <span className="pickup-quote">{th.quote}</span>
              <span className="pickup-label">{waiting ? t("presence.waiting") : t("newResult")}</span>
              <span className="pickup-time">{relativeTime(last(th), now.getTime())}</span>
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
