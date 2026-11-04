import { EditorSelection, EditorState, StateField, Transaction, type Extension } from "@codemirror/state";
import { isolateHistory } from "@codemirror/commands";
import { Decoration, EditorView, WidgetType, type DecorationSet } from "@codemirror/view";
import { AssetController, canPreview } from "../assets/controller";
import { t } from "../i18n";
import { attachmentSource, attachmentTokens, type AttachmentToken } from "./content";
import { createAssetView, type AssetView } from "./assetView";
import "./attachments.css";

interface SourceRange { from: number; to: number }
export interface AttachmentOptions {
  controller: AssetController;
  locked: (state: EditorState) => readonly SourceRange[];
  onError: (message: string) => void;
}
export interface FrozenAsset {
  threadId: string;
  className: string;
}

function editable(state: EditorState, from: number, to: number, options: AttachmentOptions): boolean {
  return !options.locked(state).some((range) => from === to ? from > range.from && from < range.to : from < range.to && range.from < to);
}

const presentations = new WeakMap<HTMLElement, AssetView>();

export class AttachmentWidget extends WidgetType {
  constructor(readonly token: AttachmentToken, readonly options: AttachmentOptions, readonly frozen?: FrozenAsset) { super(); }
  eq(other: AttachmentWidget): boolean {
    return this.token.id === other.token.id && this.token.label === other.token.label && this.token.image === other.token.image &&
      this.options.controller === other.options.controller && this.frozen?.threadId === other.frozen?.threadId && this.frozen?.className === other.frozen?.className;
  }
  toDOM(view: EditorView): HTMLElement {
    const remove = this.frozen ? undefined : () => {
      const token = attachmentTokens(view.state.doc.toString()).find((item) => item.id === this.token.id);
      if (!token || !editable(view.state, token.from, token.to, this.options)) return;
      view.dispatch({ changes: { from: token.from, to: token.to }, selection: EditorSelection.cursor(token.from), annotations: [Transaction.userEvent.of("delete"), isolateHistory.of("full")] });
      view.focus();
    };
    const presentation = createAssetView(this.options.controller, this.token, () => {
      if (view.dom.isConnected) view.requestMeasure();
    }, remove);
    const dom = presentation.dom;
    presentations.set(dom, presentation);
    if (this.frozen) {
      dom.className += ` cm-taken ${this.frozen.className}`;
      dom.dataset.thread = this.frozen.threadId;
      dom.draggable = true;
    }
    return dom;
  }
  coordsAt(dom: HTMLElement, pos: number): { left: number; right: number; top: number; bottom: number } {
    const box = dom.getBoundingClientRect();
    const x = pos > 0 ? box.right + 2 : box.left - 2;
    return { left: x, right: x, top: box.top, bottom: box.bottom };
  }
  ignoreEvent(event: Event): boolean {
    const target = event.target;
    if (target instanceof Element && target.closest("a, button")) return true;
    return event.type === "mousedown" || event.type === "dragstart";
  }
  destroy(dom: HTMLElement): void { presentations.get(dom)?.destroy(); presentations.delete(dom); }
}

function insert(view: EditorView, text: string, from: number, to: number): void {
  view.dispatch({
    changes: { from, to, insert: text }, selection: EditorSelection.cursor(from + text.length),
    annotations: [Transaction.userEvent.of("input.paste"), isolateHistory.of("full")],
  });
  view.focus();
}

export function insertFiles(view: EditorView, files: readonly File[], options: AttachmentOptions, selection: SourceRange = view.state.selection.main, text = ""): boolean {
  if (!view.dom.isConnected) return false;
  const { from, to } = selection;
  if (from < 0 || to > view.state.doc.length || !editable(view.state, from, to, options)) {
    options.onError(t("frozenImport")); return false;
  }
  try {
    options.controller.validate(files);
    const assets = options.controller.stage(files);
    let insertion = text;
    for (const asset of assets) {
      const image = canPreview(asset);
      if (insertion && !insertion.endsWith("\n")) insertion += image ? "\n" : " ";
      insertion += attachmentSource({ id: asset.id, name: asset.name, image });
      if (image) insertion += "\n";
    }
    const startsWithImage = !text && assets.length > 0 && canPreview(assets[0]);
    if (startsWithImage && from > 0 && view.state.doc.sliceString(from - 1, from) !== "\n") insertion = `\n${insertion}`;
    insert(view, insertion, from, to);
    options.onError("");
    return true;
  } catch (error) {
    options.onError(error instanceof Error ? error.message : t("saveFailed"));
    return false;
  }
}

/** A copied occurrence is new ownership; only bytes already authorized here can be reused. */
function pasteReferences(view: EditorView, source: string, options: AttachmentOptions): boolean {
  const tokens = attachmentTokens(source);
  if (!tokens.length) return false;
  const selection = view.state.selection.main;
  if (!editable(view.state, selection.from, selection.to, options)) { options.onError(t("frozenImport")); return true; }
  if (tokens.some((token) => !options.controller.get(token.id))) {
    options.onError(t("attachmentUnavailable")); return true;
  }
  let result = "";
  let from = 0;
  for (const token of tokens) {
    const copied = options.controller.clone(token.id);
    result += source.slice(from, token.from) + attachmentSource({ id: copied.id, name: token.label, image: token.image });
    from = token.to;
  }
  result += source.slice(from);
  insert(view, result, selection.from, selection.to);
  options.onError("");
  return true;
}

function hasDirectory(data: DataTransfer): boolean {
  return Array.from(data.items).some((item) => item.kind === "file" && item.webkitGetAsEntry?.()?.isDirectory);
}

function filesFrom(data: DataTransfer): File[] {
  // items and files are alternative views of the same payload, not two batches to concatenate.
  const files = Array.from(data.items).filter((item) => item.kind === "file").flatMap((item) => {
    const file = item.getAsFile();
    return file ? [file] : [];
  });
  return files.length ? files : Array.from(data.files);
}

function snapPosition(view: EditorView, pos: number, options: AttachmentOptions): number {
  const ranges = [...options.locked(view.state), ...attachmentTokens(view.state.doc.toString())];
  for (const range of ranges) {
    if (pos > range.from && pos < range.to) pos = pos - range.from <= range.to - pos ? range.from : range.to;
  }
  return pos;
}

export function attachmentExtensions(options: AttachmentOptions): Extension[] {
  const build = (state: EditorState): DecorationSet => {
    const locks = options.locked(state);
    const decorations = attachmentTokens(state.doc.toString())
      .filter((token) => !locks.some((range) => token.from < range.to && range.from < token.to))
      .map((token) => Decoration.replace({ widget: new AttachmentWidget(token, options) }).range(token.from, token.to));
    return Decoration.set(decorations, true);
  };
  const field = StateField.define<DecorationSet>({
    create: build,
    update: (value, tr) => tr.docChanged || tr.effects.length ? build(tr.state) : value,
    provide: (value) => EditorView.decorations.from(value),
  });
  return [
    field,
    EditorView.atomicRanges.of((view) => view.state.field(field)),
    EditorView.domEventHandlers({
      paste(event, view) {
        const data = event.clipboardData;
        if (!data) return false;
        if (hasDirectory(data)) { event.preventDefault(); options.onError(t("folderUnsupported")); return true; }
        const files = filesFrom(data);
        const text = data.getData("text/plain");
        if (files.length) {
          event.preventDefault();
          insertFiles(view, files, options, view.state.selection.main, text);
          return true;
        }
        const missingImage = /<img\b/i.test(data.getData("text/html"));
        const missingFile = Array.from(data.types).includes("Files") || Array.from(data.items).some((item) => item.kind === "file");
        if (missingImage || missingFile) {
          options.onError(t(missingFile ? "fileBytesUnavailable" : "imageBytesUnavailable"));
          if (!text) { event.preventDefault(); return true; }
        }
        // The Markdown fallback is authorized exactly like a custom fragment; MIME is not trust.
        if (attachmentTokens(text).length) {
          event.preventDefault();
          return pasteReferences(view, text, options);
        }
        return false;
      },
      dragover(event) {
        if (!Array.from(event.dataTransfer?.types ?? []).includes("Files")) return false;
        if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
        event.preventDefault();
        return true;
      },
      drop(event, view) {
        if (!event.dataTransfer) return false;
        if (hasDirectory(event.dataTransfer)) { event.preventDefault(); options.onError(t("folderUnsupported")); return true; }
        const files = filesFrom(event.dataTransfer);
        if (!files.length) return false;
        event.preventDefault();
        const pos = snapPosition(view, view.posAtCoords({ x: event.clientX, y: event.clientY }, false), options);
        insertFiles(view, files, options, EditorSelection.range(pos, pos));
        return true;
      },
    }),
  ];
}
