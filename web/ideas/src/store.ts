// Page state: day sections (newest first) and discussions. Persisted locally so a
// closed tab never loses the draft. Transient view state is kept out of storage.
import { useSyncExternalStore } from "react";
import { dayKey } from "./dates";
import { DaySection, Message, Span, Thread, newId } from "./model";

export interface Reading {
  day: string;
  from: number;
  to: number;
}

export interface State {
  sections: DaySection[];
  threads: Record<string, Thread>;
  openThreadId: string | null;
  hoverThreadId: string | null;
  reading: Reading[];
  now: Date;
}

const STORAGE_PREFIX = "corral.ideas.preview.v1";

/** One saved copy per signed-in account, so accounts sharing a browser never see each other's drafts. */
function storageKey(account: string | null): string {
  return account ? `${STORAGE_PREFIX}:${account}` : STORAGE_PREFIX;
}

type Listener = () => void;

export class Store {
  state: State;
  private listeners = new Set<Listener>();
  private saveTimer = 0;
  private readonly defaultOpenDays = new Set<string>();

  constructor(
    initial: Pick<State, "sections" | "threads">,
    private readonly key: string,
  ) {
    const now = new Date();
    const today = dayKey(now);
    this.defaultOpenDays.add(today);
    const sections = initial.sections.some((s) => s.day === today)
      ? initial.sections
      : [{ day: today, text: "", spans: [] }, ...initial.sections];
    this.state = {
      ...initial,
      sections: [...sections].sort((a, b) => b.day.localeCompare(a.day)),
      openThreadId: null, hoverThreadId: null, reading: [], now,
    };
    this.watchPageExit();
  }

  static load(seed: () => Pick<State, "sections" | "threads">, account: string | null): Store {
    const key = storageKey(account);
    const params = new URLSearchParams(location.search);
    if (params.has("reset")) localStorage.removeItem(key);
    try {
      const raw = localStorage.getItem(key);
      if (raw) return new Store(JSON.parse(raw), key);
    } catch {
      // A corrupt saved copy falls back to the seed rather than blocking the page.
    }
    return new Store(seed(), key);
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  private set(next: Partial<State>, persist = true): void {
    this.state = { ...this.state, ...next };
    for (const fn of this.listeners) fn();
    if (persist) this.scheduleSave();
  }

  private scheduleSave(): void {
    clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => this.saveNow(), 250);
  }

  /** Writes immediately; also runs when the page is hidden or closed, so no edit is lost. */
  saveNow(): void {
    clearTimeout(this.saveTimer);
    this.saveTimer = 0;
    const { sections, threads } = this.state;
    localStorage.setItem(this.key, JSON.stringify({ sections, threads }));
  }

  private watchPageExit(): void {
    const flush = () => {
      if (this.saveTimer) this.saveNow();
    };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") flush();
    });
  }

  /** Update the clock and add only the current day, without changing existing view defaults. */
  refreshDay(now = new Date()): void {
    const today = dayKey(now);
    const added = !this.state.sections.some((s) => s.day === today);
    if (added) this.defaultOpenDays.add(today);
    const sections = added
      ? [{ day: today, text: "", spans: [] }, ...this.state.sections].sort((a, b) => b.day.localeCompare(a.day))
      : this.state.sections;
    // Publish date and sections together so the newly mounted editor sees the correct day.
    this.set({ now, sections }, added);
  }

  section(day: string): DaySection | undefined {
    return this.state.sections.find((s) => s.day === day);
  }

  /** Manual choices persist; default expansion is frozen for this page's lifetime. */
  isOpen(section: DaySection): boolean {
    return section.open ?? this.defaultOpenDays.has(section.day);
  }

  private patchSection(day: string, patch: Partial<DaySection>): void {
    this.set({ sections: this.state.sections.map((s) => (s.day === day ? { ...s, ...patch } : s)) });
  }

  /** Editor → store: the owner changed free text; taken spans were mapped by the editor. */
  updateSection(day: string, text: string, spans: Span[]): void {
    const reading = this.state.reading.filter((r) => r.day !== day || text.slice(r.from, r.to).trim());
    this.patchSection(day, { text, spans });
    if (reading.length !== this.state.reading.length) this.set({ reading }, false);
  }

  toggleSection(day: string): void {
    const s = this.section(day);
    if (s) this.patchSection(day, { open: !this.isOpen(s) });
  }

  /**
   * Coordinator take-up. The quote must be one contiguous run of free text in the
   * current document (§6.1); otherwise the command is rejected and nothing changes.
   */
  takeUp(day: string, from: number, to: number, init: Omit<Thread, "id" | "day" | "quote" | "createdAt" | "lastReadAt">): string | null {
    const s = this.section(day);
    if (!s || from >= to || to > s.text.length) return null;
    if (s.spans.some((sp) => sp.from < to && from < sp.to)) return null;
    const quote = s.text.slice(from, to);
    if (!quote.trim()) return null;
    const id = newId("th");
    const now = Date.now();
    const thread: Thread = { id, day, quote, createdAt: now, lastReadAt: now - 1, ...init };
    const spans = [...s.spans, { threadId: id, from, to }].sort((a, b) => a.from - b.from);
    this.set({ threads: { ...this.state.threads, [id]: thread } });
    this.patchSection(day, { spans });
    return id;
  }

  updateThread(id: string, fn: (t: Thread) => Thread): void {
    const cur = this.state.threads[id];
    if (!cur) return;
    let next = fn(cur);
    if (this.state.openThreadId === id) next = { ...next, lastReadAt: Date.now() };
    this.set({ threads: { ...this.state.threads, [id]: next } });
  }

  /** Appends a comment and returns its id. */
  addMessage(id: string, msg: Omit<Message, "id" | "at">): string {
    const mid = newId("m");
    this.updateThread(id, (t) => ({ ...t, messages: [...t.messages, { ...msg, id: mid, at: Date.now() }] }));
    return mid;
  }

  openThread(id: string | null): void {
    if (id === null) {
      this.set({ openThreadId: null }, false);
      return;
    }
    const thread = this.state.threads[id];
    if (!thread) return;
    const sections = this.state.sections.map((s) =>
      s.day === thread.day && !this.isOpen(s) ? { ...s, open: true } : s,
    );
    const threads = { ...this.state.threads, [id]: { ...thread, lastReadAt: Date.now() } };
    this.set({ openThreadId: id, threads, sections });
  }

  hover(id: string | null): void {
    if (this.state.hoverThreadId !== id) this.set({ hoverThreadId: id }, false);
  }

  setReading(r: Reading, on: boolean): void {
    const rest = this.state.reading.filter((x) => !(x.day === r.day && x.from === r.from));
    this.set({ reading: on ? [...rest, r] : rest }, false);
  }
}

/** Subscribe a component to the store; re-renders on every change. */
export function useStore(store: Store): State {
  return useSyncExternalStore(
    (fn) => store.subscribe(fn),
    () => store.state,
  );
}
