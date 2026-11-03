// The discussion attached to one taken span, built on assistant-ui primitives
// (Thread / Message / Composer) with our own styling. Desktop: a panel at the right.
// Phones: a full-height sheet whose composer stays above the on-screen keyboard.
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { useEffect, useRef } from "react";
import { t } from "../i18n";
import { Message, ResultInfo, Thread, openQuestion, presence } from "../model";
import type { Store } from "../store";
import { CheckCircle, CloseIcon, SendIcon } from "../ui/icons";

interface Props {
  store: Store;
  thread: Thread | null;
  onReply: (threadId: string, text: string) => void;
}

interface Custom {
  kind: Message["kind"];
  result?: ResultInfo;
  pending?: boolean;
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
          <ThreadView key={shown.id} thread={shown} onReply={onReply} onClose={() => store.openThread(null)} />
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

function ThreadView({ thread, onReply, onClose }: { thread: Thread; onReply: (id: string, text: string) => void; onClose: () => void }) {
  const question = openQuestion(thread);
  const convertMessage = (m: Message): ThreadMessageLike => {
    const pending = m.kind === "question" && question?.id === m.questionId;
    const custom: Custom = { kind: m.kind, result: m.result, pending };
    return {
      id: m.id,
      role: m.author === "owner" ? "user" : "assistant",
      content: [{ type: "text", text: m.kind === "result" ? (m.result?.summary ?? "") : m.text }],
      createdAt: new Date(m.at),
      metadata: { custom: custom as unknown as Record<string, unknown> },
    };
  };
  const runtime = useExternalStoreRuntime({
    messages: thread.messages,
    convertMessage,
    onNew: async (message: AppendMessage) => {
      const part = message.content.find((p) => p.type === "text");
      const text = part && part.type === "text" ? part.text.trim() : "";
      if (text) onReply(thread.id, text);
    },
  });
  const status = presence(thread);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="thread">
        <header className="thread-bar">
          <span className={`thread-state is-${status}`}>{status === "quiet" ? "" : t(`presence.${status}`)}</span>
          <button className="icon-button" onClick={onClose} aria-label={t("close")} title={t("close")}>
            <CloseIcon />
          </button>
        </header>
        <ThreadPrimitive.Viewport className="thread-viewport">
          <div className="thread-head">
            <blockquote className="thread-quote">{thread.quote}</blockquote>
            {thread.summary && <p className="thread-progress">{thread.summary}</p>}
          </div>
          <ThreadPrimitive.Messages>
            {({ message }) => (message.role === "user" ? <OwnerMessage /> : <CoordinatorMessage />)}
          </ThreadPrimitive.Messages>
          <ThreadPrimitive.ViewportFooter className="thread-footer">
            <ComposerPrimitive.Root className="composer">
              <ComposerPrimitive.Input className="composer-input" rows={1} maxRows={6} placeholder={question ? t("answer") : t("reply")} submitMode="enter" />
              <ComposerPrimitive.Send className="send" aria-label={t("send")} title={t("send")}>
                <SendIcon />
              </ComposerPrimitive.Send>
            </ComposerPrimitive.Root>
          </ThreadPrimitive.ViewportFooter>
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

function OwnerMessage() {
  return (
    <MessagePrimitive.Root className="msg is-owner">
      <div className="bubble">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}

function CoordinatorMessage() {
  const custom = useAuiState((s) => s.message.metadata.custom) as unknown as Custom | undefined;
  if (custom?.kind === "result" && custom.result) {
    const r = custom.result;
    return (
      <MessagePrimitive.Root className="msg is-coordinator">
        <div className="result">
          <div className="result-head">
            <CheckCircle />
            <span>{t("result")}</span>
          </div>
          <p className="result-summary">{r.summary}</p>
          <p className="result-evidence">{r.evidence}</p>
          <p className="result-meta">
            {r.assistant} · {r.project} · {t("minutes", { n: r.minutes })}
          </p>
        </div>
      </MessagePrimitive.Root>
    );
  }
  return (
    <MessagePrimitive.Root className={`msg is-coordinator${custom?.pending ? " is-pending" : ""}`}>
      <div className="msg-text">
        <MessagePrimitive.Parts />
      </div>
    </MessagePrimitive.Root>
  );
}
