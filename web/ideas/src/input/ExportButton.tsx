import { useState } from "react";
import { exportIdeas } from "../assets/export";
import type { Store } from "../store";
import { dayKey } from "../dates";
import { t } from "../i18n";

export function ExportButton({ store }: { store: Store }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <div className="export-control">
    <button type="button" className="attach-files" disabled={busy} onClick={() => {
      setBusy(true); setError("");
      void exportIdeas(store).then((blob) => {
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url; link.download = `ideas-${dayKey(new Date())}.zip`;
        document.body.append(link); link.click(); link.remove();
        // Keep the source alive while Safari starts consuming the download.
        window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      }).catch((cause: unknown) => setError(cause instanceof Error ? cause.message : t("exportFailed")))
        .finally(() => setBusy(false));
    }}>{t("exportIdeas")}</button>
    {error && <span role="alert" className="asset-error">{error}</span>}
  </div>;
}
