// The discussion attached to one taken span: the original words, the coordinator's
// current summary and tasks, the reply history and a composer.
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { dayLabel } from "../dates";
import { relativeTime, t } from "../i18n";
import { Message, Task, Thread, openQuestion, threadStatus } from "../model";
import type { Store } from "../store";
import { CheckCircle, CloseIcon, SendIcon, Spark } from "../ui/icons";

interface Props {
  store: Store;
  thread: Thread | null;
  now: Date;
  onReply: (threadId: string, text: string) => void;
}

export function Panel({ store, thread, now, onReply }: Props) {
  // Keep the last thread rendered while the panel slides out.
  const last = useRef<Thread | null>(null);
  if (thread) last.current = thread;
  const shown = thread ?? last.current;
  const open = thread !== null;
  const scroller = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.isComposing) store.openThread(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, store]);

  // New thread: start at the latest reply; focus the composer when an answer is needed.
  useLayoutEffect(() => {
    if (!thread) return;
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
    if (openQuestion(thread)) composer.current?.focus({ preventScroll: true });
  }, [thread?.id]);

  // New messages in the open thread keep the view pinned to the bottom when it was there.
  const count = thread?.messages.length ?? 0;
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [count]);

  const status = shown ? threadStatus(shown) : "discussed";
  return (
    <aside class={`panel${open ? " is-open" : ""}`} aria-label={t("coordinator")} inert={!open} aria-hidden={!open}>
      {shown && (
        <div class="panel-card">
          <header class="panel-head">
            <span class={`chip is-${status}`}>
              <span class="dot" />
              {t(`status.${status}`)}
            </span>
            <span class="panel-day">{dayLabel(shown.day, now)}</span>
            <button class="icon-button" onClick={() => store.openThread(null)} aria-label={t("close")} title={t("close")}>
              <CloseIcon />
            </button>
          </header>
          <div class="panel-scroll" ref={scroller}>
            <blockquote class={`panel-quote is-${status}`}>{shown.quote}</blockquote>
            <NowCard thread={shown} />
            <ol class="timeline">
              {shown.messages.map((m) => (
                <MessageItem key={m.id} message={m} thread={shown} now={now} />
              ))}
            </ol>
          </div>
          <Composer key={shown.id} inputRef={composer} answering={!!openQuestion(shown)} onSend={(text) => onReply(shown.id, text)} />
        </div>
      )}
    </aside>
  );
}

function NowCard({ thread }: { thread: Thread }) {
  if (!thread.summary && thread.tasks.length === 0) return null;
  return (
    <section class="now">
      <h3 class="eyebrow">{t("now")}</h3>
      {thread.summary && <p class="now-summary">{thread.summary}</p>}
      {thread.tasks.length > 0 && (
        <ul class="tasks">
          {thread.tasks.map((task) => (
            <TaskRow key={task.id} task={task} />
          ))}
        </ul>
      )}
    </section>
  );
}

function TaskRow({ task }: { task: Task }) {
  const meta = [task.assistant, task.project].filter(Boolean).join(" · ");
  return (
    <li class={`task is-${task.state}`}>
      <span class="task-glyph" aria-hidden="true">
        {task.state === "done" ? <CheckCircle /> : <span class="dot" />}
      </span>
      <span class="task-main">
        <span class="task-title">{task.title}</span>
        {meta && <span class="task-meta">{meta}</span>}
      </span>
      <span class="task-state">{t(`task.${task.state}`)}</span>
    </li>
  );
}

function MessageItem({ message, thread, now }: { message: Message; thread: Thread; now: Date }) {
  const mine = message.author === "owner";
  const q = message.questionId ? thread.questions.find((x) => x.id === message.questionId) : undefined;
  const pendingAnswer = q && (q.state === "open" || q.state === "answered");
  return (
    <li class={`msg${mine ? " is-owner" : " is-coordinator"}${message.kind === "question" ? " is-question" : ""}${pendingAnswer ? " is-pending" : ""}`}>
      <div class="msg-head">
        <span class="avatar" aria-hidden="true">
          {mine ? t("you").slice(0, 1) : <Spark />}
        </span>
        <span class="msg-author">{mine ? t("you") : t("coordinator")}</span>
        <time class="msg-time" dateTime={new Date(message.at).toISOString()} title={new Date(message.at).toLocaleString()}>
          {relativeTime(message.at, now.getTime())}
        </time>
      </div>
      {message.kind === "result" && message.result ? (
        <div class="result">
          <div class="result-head">
            <CheckCircle />
            <span>{t("result")}</span>
          </div>
          <p class="result-summary">{message.result.summary}</p>
          <div class="evidence">
            <span class="eyebrow">{t("checked")}</span>
            <p>{message.result.evidence}</p>
          </div>
          <div class="result-meta">
            {message.result.assistant} · {message.result.project} · {t("minutes", { n: message.result.minutes })}
          </div>
        </div>
      ) : (
        <p class="msg-body">{message.text}</p>
      )}
    </li>
  );
}

function Composer({ inputRef, answering, onSend }: { inputRef: { current: HTMLTextAreaElement | null }; answering: boolean; onSend: (text: string) => void }) {
  const [value, setValue] = useState("");
  const grow = () => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
  };
  useLayoutEffect(grow, [value]);
  const send = () => {
    const text = value.trim();
    if (!text) return;
    onSend(text);
    setValue("");
  };
  return (
    <form
      class="composer"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <div class="composer-field">
        <textarea
          ref={inputRef}
          rows={1}
          value={value}
          placeholder={answering ? t("answer") : t("reply")}
          onInput={(e) => setValue((e.target as HTMLTextAreaElement).value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter adds a line; never send while an IME is composing.
            if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
              e.preventDefault();
              send();
            }
          }}
          aria-label={answering ? t("answer") : t("reply")}
        />
        <button class="send" type="submit" disabled={!value.trim()} aria-label={t("send")} title={t("send")}>
          <SendIcon />
        </button>
      </div>
    </form>
  );
}
