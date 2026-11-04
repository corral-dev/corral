// Bundled/routed only by browser tests. No production debug globals or test endpoints.
import { EditorView } from "@codemirror/view";
export { Store } from "../src/store";
export { AssetController } from "../src/assets/controller";
export { AssetRepository, RepositoryError } from "../src/assets/repository";
export { exportIdeas } from "../src/assets/export";
export { validateWorkspace } from "../src/assets/workspace";
export { attachmentTokens, attachmentSource } from "../src/input/content";
export { PreviewCoordinator } from "../src/preview";

export function editor(selector: string): EditorView {
  const element = document.querySelector(selector);
  const view = element instanceof HTMLElement ? EditorView.findFromDOM(element) : null;
  if (!view) throw new Error(`Editor not found: ${selector}`);
  return view;
}

export function select(selector: string, anchor: number, head = anchor): string {
  const view = editor(selector);
  view.dispatch({ selection: { anchor, head } });
  view.focus();
  return view.state.doc.toString();
}

export interface FileFixture { name: string; type: string; bytes: number[] }
export function paste(selector: string, files: FileFixture[], text = "", html = ""): boolean {
  const view = editor(selector);
  const data = new DataTransfer();
  for (const file of files) data.items.add(new File([new Uint8Array(file.bytes)], file.name, { type: file.type }));
  if (text) data.setData("text/plain", text);
  if (html) data.setData("text/html", html);
  const event = new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: data });
  // Firefox ignores the constructor's clipboardData initializer. This test-only fixture
  // supplies the explicitly constructed payload; it does not claim OS clipboard access.
  if (event.clipboardData?.items.length !== data.items.length || event.clipboardData?.getData("text/plain") !== text) {
    Object.defineProperty(event, "clipboardData", { value: data });
  }
  return view.contentDOM.dispatchEvent(event);
}

export function drop(selector: string, files: FileFixture[], x: number, y: number): void {
  const view = editor(selector);
  const data = new DataTransfer();
  for (const file of files) data.items.add(new File([new Uint8Array(file.bytes)], file.name, { type: file.type }));
  view.contentDOM.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, clientX: x, clientY: y, dataTransfer: data }));
}
