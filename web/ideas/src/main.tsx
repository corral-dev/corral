import { createRoot } from "react-dom/client";
import { App } from "./App";
import { loadAccount } from "./identity";
import { lang, t } from "./i18n";
import { PreviewCoordinator, seed } from "./preview";
import { Store } from "./store";
import "./styles.css";

// Settle window: 60 s by default (requirement 20). `?settle=<seconds>` shortens it for review.
const settleParam = Number(new URLSearchParams(location.search).get("settle"));
const settleMs = Number.isFinite(settleParam) && settleParam > 0 ? settleParam * 1000 : 60_000;

document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
document.title = t("appName");

let starting = false;
function startupError(message: string): void {
  const root = document.getElementById("app");
  if (!root) return;
  const box = document.createElement("section");
  box.className = "startup-error";
  box.setAttribute("role", "alert");
  const text = document.createElement("p");
  text.textContent = message;
  const retry = document.createElement("button");
  retry.textContent = t("retry");
  retry.addEventListener("click", () => { void start(); });
  box.append(text, retry);
  root.replaceChildren(box);
}

async function start(): Promise<void> {
  if (starting) return;
  starting = true;
  let account;
  try { account = await loadAccount(); }
  catch { starting = false; startupError(t("identityFailed")); return; }
  let store;
  try { store = await Store.load(seed, account?.email ?? null); }
  catch { starting = false; startupError(t("storageFailed")); return; }
  const coordinator = new PreviewCoordinator(store);
  const root = document.getElementById("app");
  if (root) createRoot(root).render(<App store={store} coordinator={coordinator} settleMs={settleMs} />);
}

void start();
