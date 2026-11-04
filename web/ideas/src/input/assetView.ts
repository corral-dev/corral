// One attachment presentation for CodeMirror widgets and read-only forum content.
// No copied HTML, remote image fetches, executable previews or persisted object URLs.
import { AssetController, canPreview } from "../assets/controller";
import { formatBytes, type AttachmentToken } from "./content";
import { t } from "../i18n";

export interface AssetView {
  dom: HTMLElement;
  destroy: () => void;
}

/** Header-only resource preflight; browser decoding still treats the image as untrusted. */
async function previewSize(blob: Blob): Promise<{ width: number; height: number } | null> {
  const bytes = new Uint8Array(await blob.slice(0, 512 * 1024).arrayBuffer());
  const view = new DataView(bytes.buffer);
  let width = 0;
  let height = 0;
  if (bytes.length >= 24 && view.getUint32(0) === 0x89504e47 && view.getUint32(4) === 0x0d0a1a0a) {
    width = view.getUint32(16); height = view.getUint32(20);
  } else if (bytes.length >= 10 && String.fromCharCode(...bytes.slice(0, 6)).match(/^GIF8[79]a$/)) {
    width = view.getUint16(6, true); height = view.getUint16(8, true);
  } else if (bytes.length >= 30 && view.getUint32(0) === 0x52494646 && view.getUint32(8) === 0x57454250) {
    const kind = view.getUint32(12);
    if (kind === 0x56503858) {
      width = 1 + bytes[24] + (bytes[25] << 8) + (bytes[26] << 16);
      height = 1 + bytes[27] + (bytes[28] << 8) + (bytes[29] << 16);
    } else if (kind === 0x5650384c && bytes[20] === 0x2f) {
      const bits = view.getUint32(21, true);
      width = 1 + (bits & 0x3fff); height = 1 + ((bits >>> 14) & 0x3fff);
    } else if (kind === 0x56503820 && bytes[23] === 0x9d && bytes[24] === 1 && bytes[25] === 0x2a) {
      width = view.getUint16(26, true) & 0x3fff; height = view.getUint16(28, true) & 0x3fff;
    }
  } else if (bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8) {
    let pos = 2;
    while (pos + 4 <= bytes.length) {
      if (bytes[pos] !== 0xff) break;
      const marker = bytes[pos + 1];
      if (marker === 0xff) { pos += 1; continue; }
      if (marker === 0xd9 || marker === 0xda) break;
      if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) { pos += 2; continue; }
      const length = view.getUint16(pos + 2);
      if (length < 2 || pos + 2 + length > bytes.length) break;
      if (pos + 9 <= bytes.length && marker >= 0xc0 && marker <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(marker)) {
        height = view.getUint16(pos + 5); width = view.getUint16(pos + 7); break;
      }
      pos += 2 + length;
    }
  }
  return width > 0 && height > 0 && width <= 20_000 && height <= 20_000 && width * height <= 40_000_000 ? { width, height } : null;
}

function showImage(blob: Blob, name: string): void {
  const dialog = document.createElement("dialog");
  dialog.className = "asset-dialog";
  dialog.setAttribute("aria-label", name);
  const close = document.createElement("button");
  close.className = "asset-dialog-close";
  close.textContent = "×";
  close.setAttribute("aria-label", t("close"));
  close.addEventListener("click", () => dialog.close());
  const img = document.createElement("img");
  img.alt = name;
  img.draggable = false;
  const url = URL.createObjectURL(blob);
  img.src = url;
  dialog.append(close, img);
  dialog.addEventListener("keydown", (event) => event.stopPropagation());
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
  dialog.addEventListener("close", () => { URL.revokeObjectURL(url); dialog.remove(); }, { once: true });
  document.body.append(dialog);
  dialog.showModal();
}

export function createAssetView(controller: AssetController, token: AttachmentToken, onMeasure: () => void = () => {}, onRemove?: () => void): AssetView {
  const asset = controller.get(token.id);
  const dom = document.createElement("span");
  dom.className = "attachment";
  dom.dataset.asset = token.id;
  dom.contentEditable = "false";
  dom.draggable = false;
  const caption = document.createElement("span");
  caption.className = "attachment-caption";
  const name = document.createElement("span");
  name.className = "attachment-name";
  name.textContent = asset?.name ?? token.label;
  name.title = asset?.name ?? token.label;
  caption.append(name);
  dom.append(caption);
  let alive = true;
  let url: string | null = null;
  const unavailable = () => {
    dom.classList.add("is-unavailable");
    const detail = document.createElement("span");
    detail.className = "attachment-detail";
    detail.textContent = t("attachmentUnavailable");
    caption.append(detail);
    onMeasure();
  };
  if (onRemove) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `${t("removeAttachment")} ${asset?.name ?? token.label}`);
    remove.addEventListener("click", (event) => { event.stopPropagation(); onRemove(); });
    caption.append(remove);
  }
  if (!asset) unavailable();
  else {
    const size = document.createElement("span");
    size.className = "attachment-size";
    size.textContent = formatBytes(asset.size);
    caption.insertBefore(size, caption.querySelector(".attachment-remove"));
    void controller.blob(asset.id).then(async (blob) => {
      if (!alive) return;
      if (!blob) return unavailable();
      url = URL.createObjectURL(blob);
      const download = document.createElement("a");
      download.className = "attachment-download";
      download.href = url;
      download.download = asset.name;
      download.textContent = t("download");
      download.setAttribute("aria-label", `${t("download")} ${asset.name}`);
      download.addEventListener("click", (event) => event.stopPropagation());
      caption.insertBefore(download, caption.querySelector(".attachment-remove"));
      if (!token.image || !canPreview(asset)) return onMeasure();
      const dimensions = await previewSize(blob);
      if (!alive || !dimensions) return;
      dom.classList.add("is-image");
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "attachment-preview";
      preview.setAttribute("aria-label", `${t("previewImage")} ${asset.name}`);
      const image = document.createElement("img");
      image.alt = token.label;
      image.draggable = false;
      image.decoding = "async";
      image.loading = "lazy";
      image.width = dimensions.width;
      image.height = dimensions.height;
      image.src = url ?? "";
      image.addEventListener("load", onMeasure, { once: true });
      image.addEventListener("error", () => { preview.remove(); dom.classList.remove("is-image"); onMeasure(); }, { once: true });
      preview.append(image);
      preview.addEventListener("click", (event) => { event.stopPropagation(); showImage(blob, asset.name); });
      dom.prepend(preview);
      onMeasure();
    }).catch(() => { if (alive) unavailable(); });
  }
  return { dom, destroy: () => { alive = false; if (url) URL.revokeObjectURL(url); } };
}
