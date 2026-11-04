// Page state: day sections (newest first) and discussions. Persisted locally so a
// closed tab never loses the draft. Transient view state is kept out of storage.
import { useSyncExternalStore } from "react";
import { dayKey } from "./dates";
import { DaySection, Message, Span, Thread, newId } from "./model";
import { AssetController } from "./assets/controller";
import { AssetRepository, RepositoryError, type AuditEvent } from "./assets/repository";
import { validateWorkspace, workspaceAssetIds, type ComposerDraft, type WorkspaceData } from "./assets/workspace";
import { attachmentTokens, splitsAttachment } from "./input/content";

export interface Reading {
  day: string;
  from: number;
  to: number;
}

export interface State extends WorkspaceData {
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
  private mutation = 0;
  private savedMutation = 0;
  private pendingSave: Promise<void> = Promise.resolve();
  private audit: AuditEvent[] = [];
  private detachExit = () => {};

  constructor(
    initial: WorkspaceData,
    private readonly key: string,
    readonly assets: AssetController,
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
  }

  static async load(seed: () => Pick<State, "sections" | "threads">, account: string | null): Promise<Store> {
    const key = storageKey(account);
    const assets = new AssetController(new AssetRepository(key));
    try {
      const saved = await assets.hydrate();
      let initial: WorkspaceData;
      if (saved) initial = validateWorkspace(saved.data);
      else {
        const legacy = localStorage.getItem(key);
        initial = validateWorkspace(legacy === null ? seed() : JSON.parse(legacy));
      }
      let store = new Store(initial, key, assets);
      if (!saved) {
        try { await store.saveNow("workspace.migrate", "system"); }
        catch (error) {
          if (!(error instanceof RepositoryError) || error.code !== "conflict") throw error;
          // Another tab won create-only migration. Adopt its canonical snapshot, never overwrite it.
          const winner = await assets.hydrate();
          if (!winner) throw error;
          store = new Store(validateWorkspace(winner.data), key, assets);
        }
      }
      // A committed pending send already exists in the canonical post list. Heal only its
      // original draft; later typed text remains an independent draft after a restart.
      for (const [draftKey, draft] of Object.entries(store.state.drafts)) {
        const pending = draft.pending;
        if (!pending || !Object.values(store.state.threads).some((thread) => thread.messages.some((message) => message.id === pending.id))) continue;
        const sameRevision = pending.revision === draft.revision;
        store.state.drafts = { ...store.state.drafts, [draftKey]: { text: sameRevision && draft.text === pending.source ? "" : draft.text, revision: draft.revision } };
        store.mutation += 1;
      }
      if (store.mutation) await store.saveNow("draft.reconcile", "system");
      store.watchPageExit();
      return store;
    } catch (error) { assets.close(); throw error; }
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  }

  private set(next: Partial<State>, persist = true, audit: AuditEvent = { actor: "owner", action: "workspace.edit", targetId: "workspace", at: Date.now() }): void {
    if (!this.assets.active) return;
    this.state = { ...this.state, ...next };
    for (const fn of this.listeners) fn();
    if (persist) { this.mutation += 1; this.audit.push(audit); this.scheduleSave(); }
  }

  private scheduleSave(): void {
    clearTimeout(this.saveTimer);
    this.saveTimer = window.setTimeout(() => { void this.saveNow().catch(() => this.publishSaveFailure()); }, 250);
  }

  /** Begin a transactional save now. Only its returned promise establishes durability. */
  async saveNow(action = "workspace.save", actor: AuditEvent["actor"] = "owner"): Promise<void> {
    clearTimeout(this.saveTimer);
    this.saveTimer = 0;
    const data = this.snapshot();
    const mutation = this.mutation;
    const events = [...this.audit];
    const audit = events.length ? events : [{ actor, action, targetId: "workspace", at: Date.now() }];
    const save = this.assets.commit(data, workspaceAssetIds(data), audit).then(() => {
      this.savedMutation = Math.max(this.savedMutation, mutation);
      this.audit = this.audit.filter((event) => !events.includes(event));
      // Text-only compatibility mirror is secondary; it never overrides canonical IDB.
      try { localStorage.setItem(this.key, JSON.stringify(data)); }
      catch { console.info("Ideas compatibility mirror unavailable; canonical content is saved."); }
    });
    this.pendingSave = save;
    return save;
  }

  snapshot(): WorkspaceData {
    const { sections, threads, drafts } = this.state;
    return { sections, threads, drafts };
  }

  flush(): Promise<void> {
    if (!this.assets.active) return Promise.reject(new Error("This workspace is no longer active."));
    return this.saveTimer || (this.savedMutation < this.mutation && !this.assets.error) ? this.saveNow() : this.pendingSave;
  }

  private publishSaveFailure(): void {
    if (!this.assets.active) return;
    // The controller exposes the typed error to the recovery UI; keep every local draft.
    for (const listener of this.listeners) listener();
  }

  draft(key: string): ComposerDraft { return this.state.drafts[key] ?? { text: "", revision: 0 }; }

  updateDraft(key: string, text: string): void {
    const previous = this.draft(key);
    this.set({ drafts: { ...this.state.drafts, [key]: { text, revision: previous.revision + 1 } } });
  }

  /** One message, dependency set, audit and conditional draft clearing per send identity. */
  async submitComment(threadId: string, source: string, draftKey: string, messageId: string, target?: { parentId: string; replyTo?: Message["author"] }): Promise<string> {
    const thread = this.state.threads[threadId];
    if (!thread) throw new Error("This discussion is no longer available.");
    const draft = this.draft(draftKey);
    const matchesDraft = draft.text === source;
    const revision = draft.pending?.id === messageId && draft.pending.source === source ? draft.pending.revision ?? draft.revision : draft.revision;
    const pending = { id: messageId, source, revision };
    const nextDraft = matchesDraft ? { ...draft, text: "", pending } : draft;
    const existing = thread.messages.find((message) => message.id === messageId);
    if (existing && existing.text !== source) throw new Error("This send identity belongs to different content.");
    const message: Message = existing ?? { id: messageId, at: Date.now(), author: "owner", kind: "text", text: source, parentId: target?.parentId, replyTo: target?.replyTo };
    this.set({
      threads: { ...this.state.threads, [threadId]: { ...thread, messages: existing ? thread.messages : [...thread.messages, message] } },
      drafts: { ...this.state.drafts, [draftKey]: nextDraft },
    }, true, { actor: "owner", action: "comment.submit", targetId: messageId, at: message.at });
    try { await this.saveNow(); }
    catch (error) {
      const current = this.draft(draftKey);
      if (this.assets.active && matchesDraft && current.revision === revision && current.pending?.id === messageId && current.pending.revision === revision && !current.text) {
        this.set({ drafts: { ...this.state.drafts, [draftKey]: { ...current, text: source, pending } } }, false);
      }
      throw error;
    }
    const current = this.draft(draftKey);
    if (current.revision === revision && current.pending?.id === messageId) {
      this.set({ drafts: { ...this.state.drafts, [draftKey]: { text: current.text, revision: current.revision } } }, false);
    }
    return messageId;
  }

  private watchPageExit(): void {
    const flush = () => {
      if (this.saveTimer) void this.saveNow().catch(() => this.publishSaveFailure());
    };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!this.assets.active || (this.savedMutation >= this.mutation && !this.assets.hasStaged && !this.assets.error)) return;
      event.preventDefault(); event.returnValue = "";
    };
    const visibility = () => { if (document.visibilityState === "hidden") flush(); };
    window.addEventListener("beforeunload", beforeUnload);
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", visibility);
    this.detachExit = () => {
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", visibility);
    };
  }

  /** Call only after durable handoff; closing is not a way to discard unsaved user content. */
  close(): void {
    clearTimeout(this.saveTimer); this.saveTimer = 0;
    this.detachExit(); this.listeners.clear(); this.assets.close();
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
    if (!s || from < 0 || from >= to || to > s.text.length || splitsAttachment(s.text, from, to)) return null;
    if (s.spans.some((sp) => sp.from < to && from < sp.to)) return null;
    const quote = s.text.slice(from, to);
    if (!quote.trim() || this.savedMutation < this.mutation || !this.assets.ready(quote)) return null;
    const id = newId("th");
    const now = Date.now();
    const attachments = attachmentTokens(s.text).filter((token) => token.from >= from && token.to <= to).map((token) => ({ ...token, from: token.from - from, to: token.to - from }));
    const thread: Thread = { ...init, id, day, quote, attachments, createdAt: now, lastReadAt: now - 1 };
    const spans = [...s.spans, { threadId: id, from, to }].sort((a, b) => a.from - b.from);
    this.set({ threads: { ...this.state.threads, [id]: thread }, sections: this.state.sections.map((section) => section.day === day ? { ...section, spans } : section) }, true,
      { actor: "system", action: "source.take_up", targetId: id, at: now });
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
