// Exact Markdown remains the ordering/source of truth. Recognize only complete asset
// links parsed as Markdown links/images, never examples inside code or escaped syntax.
import { markdownLanguage } from "@codemirror/lang-markdown";

export interface AttachmentToken {
  id: string;
  from: number;
  to: number;
  image: boolean;
  label: string;
}

export interface AttachmentLabel {
  id: string;
  name: string;
  image: boolean;
}

const REFERENCE = /^(!?)\[((?:\\.|[^\]\\])*)\]\(ideas-asset:([A-Za-z0-9_-]{1,96})\)$/;
const ID = /^[A-Za-z0-9_-]{1,96}$/;

export function attachmentTokens(source: string): AttachmentToken[] {
  const tokens: AttachmentToken[] = [];
  markdownLanguage.parser.parse(source).iterate({
    enter(node) {
      if (node.name !== "Image" && node.name !== "Link") return;
      const match = REFERENCE.exec(source.slice(node.from, node.to));
      if (!match) return;
      tokens.push({
        id: match[3], from: node.from, to: node.to, image: match[1] === "!",
        label: match[2].replace(/\\([\\\[\]])/g, "$1"),
      });
      return false;
    },
  });
  return tokens;
}

export function attachmentSource(asset: AttachmentLabel): string {
  if (!ID.test(asset.id)) throw new Error("Invalid attachment identity");
  const label = asset.name.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/[\\\[\]]/g, "\\$&");
  return `${asset.image ? "!" : ""}[${label}](ideas-asset:${asset.id})`;
}

/** Keep generated identities out of the preview coordinator's natural-language rules. */
export function contentText(source: string): string {
  let text = "";
  let from = 0;
  for (const token of attachmentTokens(source)) {
    text += source.slice(from, token.from);
    from = token.to;
  }
  return text + source.slice(from);
}

export function referencedAssets(source: string): string[] {
  return [...new Set(attachmentTokens(source).map((token) => token.id))];
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1).replace(/\.0$/, "")} MB`;
}

/** Validate atomic source boundaries without changing frozen spans or their offsets. */
export function splitsAttachment(source: string, from: number, to: number): boolean {
  return attachmentTokens(source).some((token) =>
    (from > token.from && from < token.to) || (to > token.from && to < token.to),
  );
}
