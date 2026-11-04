import { EditorState, Prec, Transaction } from "@codemirror/state";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { EditorView, drawSelection, keymap, placeholder } from "@codemirror/view";
import { LanguageSupport } from "@codemirror/language";
import { markdownLanguage } from "@codemirror/lang-markdown";
import { useEffect, useRef, useState } from "react";
import type { Store } from "../store";
import { t } from "../i18n";
import { SendIcon } from "../ui/icons";
import { attachmentExtensions, insertFiles, type AttachmentOptions } from "./attachments";

export function AttachmentPicker({ view, options }: { view: React.RefObject<EditorView | null>; options: AttachmentOptions }) {
  const input = useRef<HTMLInputElement>(null);
  const bookmark = useRef<{ view: EditorView; source: string; from: number; to: number } | null>(null);
  return <>
    <button type="button" className="attach-files" onClick={() => {
      const editor = view.current;
      if (!editor) return;
      const { from, to } = editor.state.selection.main;
      bookmark.current = { view: editor, source: editor.state.doc.toString(), from, to };
      input.current?.click();
    }}>{t("attachFiles")}</button>
    <input ref={input} type="file" multiple hidden aria-label={t("attachFiles")} onChange={(event) => {
      const captured = bookmark.current;
      bookmark.current = null;
      const files = Array.from(event.currentTarget.files ?? []);
      event.currentTarget.value = "";
      if (!files.length || !captured) return;
      if (captured.view !== view.current || !captured.view.dom.isConnected || captured.source !== captured.view.state.doc.toString()) {
        options.onError(t("inputChanged")); return;
      }
      insertFiles(captured.view, files, options, { from: captured.from, to: captured.to });
    }} />
  </>;
}

interface Props {
  store: Store;
  draftKey: string;
  inputRef: React.RefObject<EditorView | null>;
  placeholder: string;
  onSend: (source: string, draftKey: string, operationId: string) => Promise<void>;
}

export function MixedComposer({ store, draftKey, inputRef, placeholder: hint, onSend }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [value, setValue] = useState(() => store.draft(draftKey).text);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const sendCurrent = useRef<() => void>(() => {});
  const options: AttachmentOptions = { controller: store.assets, locked: () => [], onError: setError };

  sendCurrent.current = () => {
    const editor = inputRef.current;
    if (!editor || inFlight.current) return;
    const source = editor.state.doc.toString();
    if (!source.trim()) return;
    const draft = store.draft(draftKey);
    const submittedRevision = draft.revision;
    const pending = draft.pending;
    const operationId = pending?.source === source && pending.revision === submittedRevision ? pending.id : `m_${crypto.randomUUID()}`;
    inFlight.current = true;
    setSending(true);
    setError("");
    void onSend(source, draftKey, operationId).then(() => {
      // A late commit may clear only the exact submitted source, never subsequent typing.
      if (inputRef.current === editor && store.draft(draftKey).revision === submittedRevision && editor.state.doc.toString() === source) {
        editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: "" }, annotations: Transaction.addToHistory.of(false) });
      }
    }).catch((cause: unknown) => {
      if (mounted.current) setError(cause instanceof Error ? cause.message : t("saveFailed"));
    }).finally(() => { inFlight.current = false; if (mounted.current) setSending(false); });
  };

  useEffect(() => {
    if (!host.current) return;
    mounted.current = true;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: store.draft(draftKey).text,
        extensions: [
          Prec.high(EditorView.domEventHandlers({ keydown(event, view) {
            if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229 || view.composing || window.matchMedia("(pointer: coarse)").matches) return false;
            event.preventDefault(); sendCurrent.current(); return true;
          } })),
          history(), keymap.of([...defaultKeymap, ...historyKeymap]), drawSelection(),
          new LanguageSupport(markdownLanguage), EditorView.lineWrapping,
          EditorView.contentAttributes.of({ "aria-label": hint, spellcheck: "true", autocapitalize: "sentences" }),
          placeholder(hint), attachmentExtensions(options),
          EditorView.updateListener.of((update) => {
            if (!update.docChanged) return;
            const text = update.state.doc.toString();
            store.updateDraft(draftKey, text);
            setValue(text);
          }),
        ],
      }),
    });
    inputRef.current = editor;
    return () => { mounted.current = false; if (inputRef.current === editor) inputRef.current = null; editor.destroy(); };
    // Source identity owns the editor; repository notifications must not reset selection/history.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, draftKey]);

  return <div className="composer">
    <div className="mixed-input">
      <div className="mixed-composer" ref={host} />
      <AttachmentPicker view={inputRef} options={options} />
      {error && <div role="alert" className="asset-error">{error}</div>}
    </div>
    <button className="send" type="button" disabled={!value.trim() || sending} aria-label={t("send")} title={t("send")} onClick={() => sendCurrent.current()}><SendIcon /></button>
  </div>;
}
