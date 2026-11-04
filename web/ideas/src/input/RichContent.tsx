import { useLayoutEffect, useRef, useSyncExternalStore } from "react";
import type { Store } from "../store";
import { RepositoryError } from "../assets/repository";
import { t } from "../i18n";
import { attachmentTokens, type AttachmentToken } from "./content";
import { createAssetView } from "./assetView";

export function RichContent({ store, source, tokens, className = "" }: { store: Store; source: string; tokens?: readonly AttachmentToken[]; className?: string }) {
  const host = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const element = host.current;
    if (!element) return;
    const fragment = document.createDocumentFragment();
    const views: ReturnType<typeof createAssetView>[] = [];
    let from = 0;
    for (const token of tokens ?? attachmentTokens(source)) {
      fragment.append(document.createTextNode(source.slice(from, token.from)));
      const view = createAssetView(store.assets, token);
      views.push(view);
      fragment.append(view.dom);
      from = token.to;
    }
    fragment.append(document.createTextNode(source.slice(from)));
    element.replaceChildren(fragment);
    return () => { for (const view of views) view.destroy(); element.replaceChildren(); };
  }, [store, source, tokens]);
  return <span ref={host} className={`rich-content ${className}`} />;
}

export function PersistenceNotice({ store }: { store: Store }) {
  useSyncExternalStore(store.assets.subscribe, () => store.assets.version);
  const error = store.assets.error;
  if (!error) return null;
  const conflict = error instanceof RepositoryError && error.code === "conflict";
  return <div role="alert" className="asset-error persistence-error">
    {conflict ? t("workspaceConflict") : t("saveFailed")}
    {!conflict && <button type="button" onClick={() => { void store.saveNow().catch(() => { /* The same recoverable error remains visible. */ }); }}>{t("retry")}</button>}
  </div>;
}
