// Domain model for the Ideas page. Mirrors the state model in
// docs/design/WEB_TASK_BUTLER_DESIGN.md §3.1: spans, discussions (threads), tasks and
// questions. Discussion status is always derived, never stored.

export type TaskState =
  | "blocked"
  | "queued"
  | "running"
  | "review"
  | "needs_input"
  | "stalled"
  | "done"
  | "failed"
  | "canceled";

export const TERMINAL: ReadonlySet<TaskState> = new Set(["done", "failed", "canceled"]);

export type QuestionState = "open" | "answered" | "resolved" | "withdrawn";

export interface Task {
  id: string;
  title: string;
  state: TaskState;
  assistant?: string;
  project?: string;
  updatedAt: number;
}

export interface Question {
  id: string;
  state: QuestionState;
  blocking: boolean;
}

export interface ResultInfo {
  summary: string;
  evidence: string;
  assistant: string;
  project: string;
  minutes: number;
}

export type MessageKind = "text" | "question" | "result";

export interface Message {
  id: string;
  author: "owner" | "coordinator";
  at: number;
  kind: MessageKind;
  text: string;
  questionId?: string;
  result?: ResultInfo;
}

export interface Thread {
  id: string;
  day: string;
  quote: string;
  createdAt: number;
  summary: string;
  messages: Message[];
  tasks: Task[];
  questions: Question[];
  lastReadAt: number;
}

/** A taken-up range of owner text inside one day section. */
export interface Span {
  threadId: string;
  from: number;
  to: number;
}

export interface DaySection {
  day: string; // local calendar day, YYYY-MM-DD
  text: string;
  spans: Span[];
  /** Owner's explicit fold choice; undefined means the automatic default. */
  open?: boolean;
}

export type ThreadStatus = "waiting" | "attention" | "working" | "done" | "withdrawn" | "discussed";

/** §3.1 discussion status: first matching rule wins. */
export function threadStatus(thread: Thread): ThreadStatus {
  if (thread.questions.some((q) => q.state === "open" || q.state === "answered")) return "waiting";
  if (thread.tasks.some((t) => t.state === "failed" || t.state === "stalled")) return "attention";
  if (thread.tasks.some((t) => !TERMINAL.has(t.state))) return "working";
  if (thread.tasks.length === 0) return "discussed";
  if (thread.tasks.some((t) => t.state === "done")) return "done";
  return "withdrawn";
}

/** What the owner sees (owner correction 2026-11-03): three states plus quiet ends. */
export type Presence = "waiting" | "working" | "done" | "withdrawn" | "quiet";

export function presence(thread: Thread): Presence {
  const s = threadStatus(thread);
  if (s === "waiting" || s === "attention") return "waiting";
  if (s === "working") return "working";
  if (s === "done") return "done";
  if (s === "withdrawn") return "withdrawn";
  return "quiet";
}

export function isUnread(thread: Thread): boolean {
  return thread.messages.some((m) => m.author !== "owner" && m.at > thread.lastReadAt);
}

export function hasUnreadResult(thread: Thread): boolean {
  return thread.messages.some((m) => m.kind === "result" && m.at > thread.lastReadAt);
}

export function openQuestion(thread: Thread): Question | undefined {
  return thread.questions.find((q) => q.state === "open" || q.state === "answered");
}

let counter = 0;
export function newId(prefix: string): string {
  counter += 1;
  return `${prefix}_${Date.now().toString(36)}${counter.toString(36)}`;
}
