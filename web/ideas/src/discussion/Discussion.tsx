// The discussion attached to one taken span, laid out as a forum thread (owner correction
// 2026-11-03): the owner's sentence is the post; comments follow oldest first, each with a
// Reply action; replies sit under their comment, one level deep ("@name" when answering a
// reply). Every comment looks the same, results and questions included.
// Desktop: a panel at the right. Phones: a full-height sheet above the keyboard.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { EditorView } from "@codemirror/view";
import { MixedComposer } from "../input/MixedComposer";
import { RichContent } from "../input/RichContent";
import { relativeTime, t } from "../i18n";
import { Message, Thread, openQuestion, presence } from "../model";
import type { Store } from "../store";
import { CloseIcon } from "../ui/icons";

export interface ReplyTarget {
  parentId: string;
  replyTo?: Message["author"];
}

interface Props {
  store: Store;
  thread: Thread | null;
  onReply: (threadId: string, text: string, target: ReplyTarget | undefined, draftKey: string, operationId: string) => Promise<void>;
}

export function Discussion({ store, thread, onReply }: Props) {
  // Keep the last thread rendered while the sheet slides away.
  const last = useRef<Thread | null>(null);
  if (thread) last.current = thread;
  const shown = thread ?? last.current;
  const open = thread !== null;
  const sheet = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.isComposing) store.openThread(null);
    };
    window.addEventListener("keydown", onKey);
    document.documentElement.classList.add("has-sheet");
    return () => {
      window.removeEventListener("keydown", onKey);
      document.documentElement.classList.remove("has-sheet");
    };
  }, [open, store]);

  useKeyboardAwareHeight(sheet, open);

  return (
    <aside ref={sheet} className={`sheet${open ? " is-open" : ""}`} aria-label={t("coordinator")} inert={!open} aria-hidden={!open}>
      {shown && (
        <div className="sheet-card">
          <ThreadView key={shown.id} store={store} thread={shown} onReply={onReply} onClose={() => store.openThread(null)} />
        </div>
      )}
    </aside>
  );
}

/** Keeps the sheet inside the visual viewport so the composer rides above the keyboard (iOS Safari). */
function useKeyboardAwareHeight(ref: React.RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    const vv = window.visualViewport;
    const el = ref.current;
    if (!active || !vv || !el) return;
    const apply = () => {
      el.style.setProperty("--vv-height", `${vv.height}px`);
      el.style.setProperty("--vv-top", `${vv.offsetTop}px`);
    };
    apply();
    vv.addEventListener("resize", apply);
    vv.addEventListener("scroll", apply);
    return () => {
      vv.removeEventListener("resize", apply);
      vv.removeEventListener("scroll", apply);
      el.style.removeProperty("--vv-height");
      el.style.removeProperty("--vv-top");
    };
  }, [ref, active]);
}

function authorName(a: Message["author"]): string {
  return a === "owner" ? t("you") : t("coordinator");
}

function bodyOf(m: Message): string {
  if (m.kind === "result" && m.result) return `${m.result.summary}\n${m.result.evidence}`;
  return m.text;
}

function ThreadView({ store, thread, onReply, onClose }: { store: Store; thread: Thread; onReply: Props["onReply"]; onClose: () => void }) {
  const [target, setTarget] = useState<ReplyTarget | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<EditorView | null>(null);
  const status = presence(thread);
  const now = Date.now();

  const roots = thread.messages.filter((m) => !m.parentId);
  const repliesOf = (id: string) => thread.messages.filter((m) => m.parentId === id);

  // Start at the latest comment; afterwards bring each new comment into view.
  const count = thread.messages.length;
  const lastId = thread.messages[count - 1]?.id;
  const first = useRef(true);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (first.current) {
      first.current = false;
      el.scrollTop = el.scrollHeight;
      return;
    }
    el.querySelector(`[data-comment="${lastId}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [count, lastId]);

  const reply = (m: Message) => {
    setTarget(m.parentId ? { parentId: m.parentId, replyTo: m.author } : { parentId: m.id });
    input.current?.focus();
  };
  const targetName = target ? authorName(target.replyTo ?? thread.messages.find((m) => m.id === target.parentId)?.author ?? "coordinator") : "";

  return (
    <div className="thread">
      <header className="thread-bar">
        <span className={`thread-state is-${status}`}>{status === "quiet" ? "" : t(`presence.${status}`)}</span>
        <button className="icon-button" onClick={onClose} aria-label={t("close")} title={t("close")}>
          <CloseIcon />
        </button>
      </header>
      <div className="thread-scroll" ref={scroller}>
        <div className="post">
          <p className="post-text"><RichContent store={store} source={thread.quote} tokens={thread.attachments} /></p>
          {thread.summary && <p className="post-meta">{thread.summary}</p>}
        </div>
        <ol className="comments">
          {roots.map((root) => (
            <li key={root.id} className="comment-group">
              <Comment store={store} message={root} now={now} onReply={reply} />
              {repliesOf(root.id).length > 0 && (
                <ol className="replies">
                  {repliesOf(root.id).map((r) => (
                    <li key={r.id}>
                      <Comment store={store} message={r} now={now} onReply={reply} />
                    </li>
                  ))}
                </ol>
              )}
            </li>
          ))}
        </ol>
      </div>
      <Composer
        store={store}
        draftKey={`draft:${thread.id}:${target ? `${target.parentId}:${target.replyTo ?? "direct"}` : "root"}`}
        inputRef={input}
        target={target}
        targetName={targetName}
        placeholder={target ? t("replyTo", { name: targetName }) : openQuestion(thread) ? t("answer") : t("comment")}
        onCancelTarget={() => setTarget(null)}
        onSend={async (text, draftKey, operationId) => {
          await onReply(thread.id, text, target ?? undefined, draftKey, operationId);
          if (!store.draft(draftKey).text) setTarget(null);
        }}
      />
    </div>
  );
}

function Comment({ store, message, now, onReply }: { store: Store; message: Message; now: number; onReply: (m: Message) => void }) {
  const name = authorName(message.author);
  return (
    <article className={`comment is-${message.author}`} data-comment={message.id}>
      <span className="avatar" aria-hidden="true">
        {name.slice(0, 1)}
      </span>
      <div className="comment-main">
        <div className="comment-head">
          <span className="comment-author">{name}</span>
          <time className="comment-time" dateTime={new Date(message.at).toISOString()} title={new Date(message.at).toLocaleString()}>
            {relativeTime(message.at, now)}
          </time>
        </div>
        <p className="comment-body">
          {message.replyTo && <span className="mention">@{authorName(message.replyTo)} </span>}
          <RichContent store={store} source={bodyOf(message)} />
        </p>
        <button className="comment-action" onClick={() => onReply(message)}>
          {t("replyAction")}
        </button>
      </div>
    </article>
  );
}

function Composer(props: {
  store: Store;
  draftKey: string;
  inputRef: React.RefObject<EditorView | null>;
  target: ReplyTarget | null;
  targetName: string;
  placeholder: string;
  onCancelTarget: () => void;
  onSend: (text: string, draftKey: string, operationId: string) => Promise<void>;
}) {
  return <div className="composer-bar">
    {props.target && <div className="replying">
      <span>{t("replyingTo", { name: props.targetName })}</span>
      <button type="button" className="replying-cancel" onClick={props.onCancelTarget} aria-label={t("cancel")} title={t("cancel")}><CloseIcon /></button>
    </div>}
    <MixedComposer key={props.draftKey} store={props.store} draftKey={props.draftKey} inputRef={props.inputRef} placeholder={props.placeholder} onSend={props.onSend} />
  </div>;
}
