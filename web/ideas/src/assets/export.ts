// Streaming store-only ZIP: archive originals verbatim without base64 or a second editor
// model. The returned Blob is an explicit user export, not a background upload/backup.
import { Zip, ZipPassThrough } from "fflate";
import type { Store } from "../store";
import { workspaceAssetIds } from "./workspace";
import { attachmentTokens } from "../input/content";

function rewrite(source: string, paths: Map<string, string>): string {
  let result = "";
  let from = 0;
  for (const token of attachmentTokens(source)) {
    const path = paths.get(token.id);
    if (!path) throw new Error(`Original file unavailable: ${token.label}`);
    const encoded = path.split("/").map(encodeURIComponent).join("/");
    result += source.slice(from, token.from) + source.slice(token.from, token.to).replace(`ideas-asset:${token.id}`, encoded);
    from = token.to;
  }
  return result + source.slice(from);
}

export async function exportIdeas(store: Store): Promise<Blob> {
  const snapshot = structuredClone(store.snapshot());
  const originals = new Map<string, { path: string; blob: Blob }>();
  const paths = new Map<string, string>();
  const manifest = [];
  let totalBytes = 0;
  for (const id of workspaceAssetIds(snapshot)) {
    const asset = store.assets.get(id);
    if (!asset) throw new Error("An original file is unavailable. The export has not been completed.");
    let original = originals.get(asset.blobId);
    if (!original) {
      const blob = await store.assets.blob(id);
      if (!blob || blob.size !== asset.size) throw new Error(`Original file unavailable: ${asset.name}`);
      const name = asset.name.replace(/[\\/\u0000-\u001f\u007f:*?"<>|]/g, "_").slice(0, 100) || "file";
      original = { path: `assets/${asset.blobId}-${name}`, blob };
      totalBytes += blob.size;
      if (totalBytes > 1024 ** 3 || originals.size >= 5000) throw new Error("This export exceeds the 1 GB or 5,000-file limit. Download the originals individually.");
      originals.set(asset.blobId, original);
    }
    paths.set(id, original.path);
    manifest.push({ ...asset, path: original.path });
  }
  const days = snapshot.sections.map((section) => `## ${section.day}\n\n${rewrite(section.text, paths)}`).join("\n\n");
  const discussions = Object.values(snapshot.threads).map((thread) => {
    const comments = thread.messages.map((message) => {
      const body = message.result ? `${message.result.summary}\n${message.result.evidence}` : message.text;
      const target = message.parentId ? ` (reply to ${message.parentId})` : "";
      return `### ${message.author} — ${new Date(message.at).toISOString()}${target}\n\n${rewrite(body, paths)}`;
    });
    return `## ${thread.id}\n\n${rewrite(thread.quote, paths)}\n\n${comments.join("\n\n")}`;
  }).join("\n\n");
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let failure: Error | null = null;
  let complete = false;
  const zip = new Zip((error, bytes, final) => {
    if (error) { failure = error; return; }
    const copy = new Uint8Array(bytes.length);
    copy.set(bytes);
    chunks.push(copy);
    complete = final;
  });
  const add = async (name: string, blob: Blob) => {
    const entry = new ZipPassThrough(name);
    zip.add(entry);
    const reader = blob.stream().getReader();
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        entry.push(part.value);
        if (failure) throw failure;
      }
      entry.push(new Uint8Array(0), true);
    } finally { reader.releaseLock(); }
    if (failure) throw failure;
  };
  await add("ideas.md", new Blob([`# Ideas\n\n${days}\n`], { type: "text/markdown" }));
  await add("discussions.md", new Blob([`# Discussions\n\n${discussions}\n`], { type: "text/markdown" }));
  await add("workspace.json", new Blob([JSON.stringify({ schema: 2, ...snapshot }, null, 2)], { type: "application/json" }));
  await add("manifest.json", new Blob([JSON.stringify({ schema: 1, assets: manifest }, null, 2)], { type: "application/json" }));
  for (const original of originals.values()) await add(original.path, original.blob);
  zip.end();
  if (failure) throw failure;
  if (!complete) throw new Error("The export archive did not finish.");
  return new Blob(chunks, { type: "application/zip" });
}
