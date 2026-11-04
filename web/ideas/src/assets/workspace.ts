import type { DaySection, Message, Question, ResultInfo, Task, Thread } from "../model";
import { attachmentTokens, referencedAssets, type AttachmentToken } from "../input/content";

export interface ComposerDraft {
  text: string;
  revision: number;
  /** Retry identity survives a failed send and later panel remount. */
  pending?: { id: string; source: string; revision?: number };
}
export interface WorkspaceData {
  sections: DaySection[];
  threads: Record<string, Thread>;
  drafts: Record<string, ComposerDraft>;
}

function record(value: unknown): value is Record<string, unknown> { return typeof value === "object" && value !== null && !Array.isArray(value); }
function number(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0; }
function id(value: unknown): value is string { return typeof value === "string" && /^[A-Za-z][A-Za-z0-9_-]{0,159}$/.test(value); }
function optionalString(value: unknown): boolean { return value === undefined || typeof value === "string"; }
const TASK_STATES = new Set(["blocked", "queued", "running", "review", "needs_input", "stalled", "done", "failed", "canceled"]);
const QUESTION_STATES = new Set(["open", "answered", "resolved", "withdrawn"]);

function result(value: unknown): value is ResultInfo {
  return record(value) && typeof value.summary === "string" && typeof value.evidence === "string" && typeof value.assistant === "string" && typeof value.project === "string" && typeof value.minutes === "number" && Number.isFinite(value.minutes) && value.minutes >= 0;
}
function message(value: unknown): value is Message {
  return record(value) && id(value.id) && (value.author === "owner" || value.author === "coordinator") && number(value.at) &&
    (value.kind === "text" || value.kind === "question" || value.kind === "result") && typeof value.text === "string" &&
    (value.questionId === undefined || id(value.questionId)) && (value.parentId === undefined || id(value.parentId)) &&
    (value.replyTo === undefined || value.replyTo === "owner" || value.replyTo === "coordinator") && (value.result === undefined || result(value.result));
}
function task(value: unknown): value is Task {
  return record(value) && id(value.id) && typeof value.title === "string" && typeof value.state === "string" && TASK_STATES.has(value.state) && number(value.updatedAt) && optionalString(value.assistant) && optionalString(value.project);
}
function question(value: unknown): value is Question {
  return record(value) && id(value.id) && typeof value.state === "string" && QUESTION_STATES.has(value.state) && typeof value.blocking === "boolean";
}
function token(value: unknown): value is AttachmentToken {
  return record(value) && id(value.id) && number(value.from) && number(value.to) && value.to > value.from && typeof value.image === "boolean" && typeof value.label === "string";
}
function thread(value: unknown): value is Thread {
  return record(value) && id(value.id) && typeof value.day === "string" && typeof value.quote === "string" && number(value.createdAt) &&
    typeof value.summary === "string" && number(value.lastReadAt) && Array.isArray(value.messages) && value.messages.every(message) &&
    Array.isArray(value.tasks) && value.tasks.every(task) && Array.isArray(value.questions) && value.questions.every(question) &&
    (value.attachments === undefined || (Array.isArray(value.attachments) && value.attachments.every(token)));
}
function section(value: unknown): value is DaySection {
  return record(value) && typeof value.day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value.day) && typeof value.text === "string" &&
    (value.open === undefined || typeof value.open === "boolean") && Array.isArray(value.spans) && value.spans.every((span: unknown) => record(span) && id(span.threadId) && number(span.from) && number(span.to) && span.from < span.to);
}
function draft(value: unknown): value is ComposerDraft {
  return record(value) && typeof value.text === "string" && number(value.revision) &&
    (value.pending === undefined || (record(value.pending) && id(value.pending.id) && typeof value.pending.source === "string" && (value.pending.revision === undefined || number(value.pending.revision))));
}

/** Reject corrupt content without falling back to seeds or overwriting recoverable source. */
export function validateWorkspace(value: unknown): WorkspaceData {
  if (!record(value) || !Array.isArray(value.sections) || !value.sections.every(section) || !record(value.threads)) throw new Error("Invalid saved Ideas content.");
  const sections: DaySection[] = value.sections;
  const threads: Record<string, Thread> = {};
  for (const [key, entry] of Object.entries(value.threads)) {
    if (!thread(entry) || key !== entry.id) throw new Error("Invalid saved discussion.");
    const tokens = entry.attachments ?? attachmentTokens(entry.quote);
    if (tokens.some((item) => item.to > entry.quote.length || !attachmentTokens(entry.quote.slice(item.from, item.to)).some((parsed) => parsed.id === item.id && parsed.to === item.to - item.from))) throw new Error("Invalid frozen attachment binding.");
    threads[key] = entry;
  }
  if (new Set(sections.map((entry) => entry.day)).size !== sections.length) throw new Error("Duplicate saved calendar day.");
  for (const entry of sections) {
    let end = 0;
    for (const span of entry.spans) {
      const discussion = threads[span.threadId];
      if (!discussion || discussion.day !== entry.day || span.from < end || span.to > entry.text.length || entry.text.slice(span.from, span.to) !== discussion.quote) throw new Error("Invalid frozen source range.");
      end = span.to;
    }
  }
  const drafts: Record<string, ComposerDraft> = {};
  if (value.drafts !== undefined) {
    if (!record(value.drafts)) throw new Error("Invalid saved comment drafts.");
    for (const [key, entry] of Object.entries(value.drafts)) {
      if (!key.startsWith("draft:") || key.length > 512 || !draft(entry)) throw new Error("Invalid saved comment draft.");
      drafts[key] = entry;
    }
  }
  return { sections, threads, drafts };
}

export function workspaceAssetIds(data: WorkspaceData): string[] {
  const ids = new Set<string>();
  const add = (source: string) => { for (const id of referencedAssets(source)) ids.add(id); };
  for (const section of data.sections) add(section.text);
  for (const thread of Object.values(data.threads)) {
    add(thread.quote);
    for (const token of thread.attachments ?? []) ids.add(token.id);
    for (const message of thread.messages) { add(message.text); if (message.result) { add(message.result.summary); add(message.result.evidence); } }
  }
  for (const draft of Object.values(data.drafts)) add(draft.text);
  return [...ids];
}
