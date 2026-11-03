import { render } from "preact";
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

async function start(): Promise<void> {
  const account = await loadAccount();
  const store = Store.load(seed, account?.email ?? null);
  const coordinator = new PreviewCoordinator(store);
  const root = document.getElementById("app");
  if (root) render(<App store={store} coordinator={coordinator} settleMs={settleMs} account={account} />, root);
}

void start();
