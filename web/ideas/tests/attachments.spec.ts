import { expect, type Page, type Locator } from "@playwright/test";
import { test } from "./persistent-page";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import { unzipSync } from "fflate";
import type * as Helpers from "./browser-helpers";
import type { WorkspaceData } from "../src/assets/workspace";

const DAY = "2026-11-04";
const ACCOUNT = "attachments@example.test";
const KEY = `corral.ideas.preview.v1:${ACCOUNT}`;
const MODULE = "/__ideas_test_helpers__/helpers.js";
const CANVAS = `[data-day="${DAY}"] .cm-editor`;
const COMPOSER = ".mixed-composer .cm-editor";
const QUOTE = "Frozen sentence.";
const PNG = [...Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==", "base64")];
const picture = { name: "small-phone.png", type: "image/png", bytes: PNG };
const document = { name: "response.json", type: "application/json", bytes: [...Buffer.from('{"marker":"original-73","ok":true}')] };
let bundle = "";

test.beforeAll(async () => {
  const result = await build({ entryPoints: [fileURLToPath(new URL("./browser-helpers.ts", import.meta.url))], bundle: true, write: false, format: "esm", target: "es2022" });
  const file = result.outputFiles[0];
  if (!file) throw new Error("Browser helper bundle missing");
  bundle = file.text;
});

test.beforeEach(async ({ page }) => {
  await page.route(`**${MODULE}`, (route) => route.fulfill({ contentType: "text/javascript", body: bundle }));
  await page.route("**/oauth2/userinfo", (route) => route.fulfill({ json: { email: ACCOUNT } }));
});

function fixture(text = `${QUOTE}\n\nEditable note.`): WorkspaceData {
  const at = new Date("2026-11-04T10:00:00+08:00").getTime();
  return { sections: [{ day: DAY, text, spans: [{ threadId: "discussion", from: 0, to: QUOTE.length }] }], threads: { discussion: {
    id: "discussion", day: DAY, quote: QUOTE, createdAt: at, lastReadAt: at, summary: "", tasks: [], questions: [],
    messages: [{ id: "comment", author: "coordinator", at, kind: "text", text: "A comment awaiting your reply." }],
  } }, drafts: {} };
}

async function open(page: Page, data = fixture(), settle = 3600) {
  await page.clock.install({ time: new Date("2026-11-04T12:00:00+08:00") });
  await page.addInitScript(({ key, data }) => { if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify(data)); }, { key: KEY, data });
  await page.goto(`/index.html?lang=en&settle=${settle}`);
  await expect(page.locator(CANVAS)).toBeVisible();
}

async function source(page: Page, selector = CANVAS): Promise<string> {
  return page.evaluate(async ({ path, selector }) => {
    const helper: typeof Helpers = await import(path);
    return helper.editor(selector).state.doc.toString();
  }, { path: MODULE, selector });
}

async function select(page: Page, anchor: number, head = anchor, selector = CANVAS) {
  return page.evaluate(async ({ path, selector, anchor, head }) => {
    const helper: typeof Helpers = await import(path);
    return helper.select(selector, anchor, head);
  }, { path: MODULE, selector, anchor, head });
}

async function paste(page: Page, files = [picture], selector = CANVAS, text = "") {
  await page.evaluate(async ({ path, selector, files, text }) => {
    const helper: typeof Helpers = await import(path);
    helper.paste(selector, files, text);
  }, { path: MODULE, selector, files, text });
}

async function saved(page: Page): Promise<WorkspaceData> {
  return page.evaluate(async ({ path, key }) => {
    const helper: typeof Helpers = await import(path);
    const repository = new helper.AssetRepository(key);
    try {
      const workspace = await repository.load();
      if (!workspace || typeof workspace.data !== "object" || workspace.data === null || !("sections" in workspace.data) || !("threads" in workspace.data) || !("drafts" in workspace.data)) throw new Error("No canonical workspace");
      // The application validated this content before mounting; reuse its validator in tests.
      return helper.validateWorkspace(workspace.data);
    } finally { repository.close(); }
  }, { path: MODULE, key: KEY });
}

async function currentSaved(page: Page): Promise<string> {
  return page.evaluate(async ({ path, key, day }) => {
    const helper: typeof Helpers = await import(path);
    const repository = new helper.AssetRepository(key);
    const entry = await repository.load(); repository.close();
    if (!entry || typeof entry.data !== "object" || entry.data === null || !("sections" in entry.data) || !Array.isArray(entry.data.sections)) return "";
    const section: unknown = entry.data.sections.find((item: unknown) => typeof item === "object" && item !== null && "day" in item && item.day === day);
    return typeof section === "object" && section !== null && "text" in section && typeof section.text === "string" ? section.text : "";
  }, { path: MODULE, key: KEY, day: DAY });
}

async function openDiscussion(page: Page) {
  await page.locator('.cm-taken[data-thread="discussion"]').first().click();
  await expect(page.locator(COMPOSER)).toBeVisible();
}

const mod = process.platform === "darwin" ? "Meta" : "Control";

async function dragToEnd(page: Page, dragged: Locator): Promise<void> {
  const box = await dragged.evaluate((element) => {
    const rect = element.getClientRects()[0];
    if (!rect) throw new Error("Frozen sentence bounds unavailable");
    return { x: rect.x, y: rect.y, height: rect.height };
  });
  const target = await page.evaluate(async ({ path, selector }) => {
    const helper: typeof Helpers = await import(path); const view = helper.editor(selector);
    const coords = view.coordsAtPos(view.state.doc.length);
    if (!coords) throw new Error("Drop caret unavailable");
    return { x: coords.left, y: (coords.top + coords.bottom) / 2 };
  }, { path: MODULE, selector: CANVAS });
  await page.mouse.move(box.x + 8, box.y + box.height / 2); await page.mouse.down();
  for (let step = 1; step <= 8; step++) {
    await page.mouse.move(box.x + 8 + (target.x - box.x - 8) * step / 8, box.y + box.height / 2 + (target.y - box.y - box.height / 2) * step / 8);
    await page.waitForTimeout(50);
  }
  await page.mouse.move(target.x + 1, target.y); await page.waitForTimeout(150); await page.mouse.up();
}

test("HTML-only images preserve the selection, explain the fallback and never fetch remote originals", async ({ page }) => {
  await open(page); const before = await source(page);
  await select(page, QUOTE.length + 2, before.length);
  const remoteReads: string[] = [];
  page.on("request", (request) => { if (request.url().includes("example.invalid")) remoteReads.push(request.url()); });
  const html = '<p>A note</p><img src="https://example.invalid/private.png">';
  await page.evaluate(async ({ path, selector, html }) => {
    const helper: typeof Helpers = await import(path); helper.paste(selector, [], "", html);
  }, { path: MODULE, selector: CANVAS, html });
  await expect(page.locator('.day-input-actions .asset-error')).toContainText("original image");
  expect(await source(page)).toBe(before);
  await page.evaluate(async ({ path, selector, html }) => {
    const helper: typeof Helpers = await import(path); helper.paste(selector, [], "A note", html);
  }, { path: MODULE, selector: CANVAS, html });
  expect(await source(page)).toBe(`${QUOTE}\n\nA note`);
  await expect(page.locator('.attachment')).toHaveCount(0);
  expect(remoteReads).toEqual([]);
});

test("directory drops are rejected before source changes or asset allocation", async ({ page }) => {
  await open(page); const before = await source(page);
  await page.evaluate(async ({ path, selector }) => {
    const helper: typeof Helpers = await import(path); const view = helper.editor(selector);
    const data = new DataTransfer(); data.items.add(new File([], "folder"));
    const descriptor = Object.getOwnPropertyDescriptor(DataTransferItem.prototype, "webkitGetAsEntry");
    if (!descriptor) throw new Error("Missing directory-entry API");
    // The guard consumes the native entry API, not a zero-byte file-name heuristic.
    Object.defineProperty(DataTransferItem.prototype, "webkitGetAsEntry", { configurable: true, value: () => ({ isDirectory: true }) });
    try { view.contentDOM.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: data })); }
    finally { Object.defineProperty(DataTransferItem.prototype, "webkitGetAsEntry", descriptor); }
  }, { path: MODULE, selector: CANVAS });
  await expect(page.locator('.day-input-actions .asset-error')).toContainText("ZIP");
  expect(await source(page)).toBe(before);
  await expect(page.locator('.attachment')).toHaveCount(0);
});

test("preview URLs follow reused widget DOM through edits, removal, undo and redo", async ({ page }) => {
  await page.addInitScript(() => {
    const live = new Set<string>(); const create = URL.createObjectURL.bind(URL); const revoke = URL.revokeObjectURL.bind(URL);
    URL.createObjectURL = (blob) => { const url = create(blob); live.add(url); return url; };
    URL.revokeObjectURL = (url) => { live.delete(url); revoke(url); };
    window.addEventListener("inspect-preview-urls", () => { window.document.documentElement.dataset.activePreviewUrls = String(live.size); });
  });
  const liveURLs = () => page.evaluate(() => { window.dispatchEvent(new Event("inspect-preview-urls")); return Number(window.document.documentElement.dataset.activePreviewUrls); });
  await open(page); await select(page, (await source(page)).length); await paste(page);
  await expect.poll(liveURLs).toBe(1);
  await select(page, QUOTE.length + 2); await page.keyboard.type("Prefix editing ");
  await page.locator(`${CANVAS} .attachment-remove`).click();
  await expect.poll(liveURLs).toBe(0);
  await page.locator(`${CANVAS} .cm-content`).focus(); await page.keyboard.press(`${mod}+z`);
  await expect.poll(liveURLs).toBe(1);
  await page.keyboard.press(`${mod}+Shift+z`); await expect.poll(liveURLs).toBe(0);
});

test("a conflicting tab exports its unsaved snapshot and staged original without overwriting the winning tab", async ({ page }) => {
  await open(page); await expect.poll(() => currentSaved(page)).toBe(await source(page));
  const loser = await page.context().newPage();
  try {
    await loser.clock.install({ time: new Date("2026-11-04T12:00:00+08:00") });
    await loser.route(`**${MODULE}`, (route) => route.fulfill({ contentType: "text/javascript", body: bundle }));
    await loser.route("**/oauth2/userinfo", (route) => route.fulfill({ json: { email: ACCOUNT } }));
    await loser.goto('/index.html?lang=en&settle=3600'); await expect(loser.locator(CANVAS)).toBeVisible();
    await select(page, (await source(page)).length); await page.keyboard.type(" Winning tab.");
    const winning = await source(page); await expect.poll(() => currentSaved(page)).toBe(winning);
    await select(loser, (await source(loser)).length); await paste(loser, [document]);
    await expect(loser.locator('.persistence-error')).toContainText("another tab");
    const download = loser.waitForEvent("download"); await loser.getByRole("button", { name: "Export", exact: true }).click();
    const exported = await download; const file = await exported.path();
    if (!file) throw new Error("Conflict export was not downloaded");
    const zip = unzipSync(await readFile(file));
    const markdown = zip["ideas.md"]; if (!markdown) throw new Error("Conflict export lacks Markdown");
    expect(new TextDecoder().decode(markdown)).toContain("response.json");
    expect(Object.values(zip).some((bytes) => new TextDecoder().decode(bytes).includes('"marker":"original-73"'))).toBe(true);
    expect(await currentSaved(page)).toBe(winning);
  } finally { await loser.close(); }
});

test("paste keeps text, image and file at the caret; originals survive reload/download/export", async ({ page }) => {
  await open(page);
  const before = await source(page);
  await select(page, before.length);
  await paste(page, [picture, document], CANVAS, " Screenshot details.");
  await expect(page.locator(`${CANVAS} .attachment`)).toHaveCount(2);
  const inserted = await source(page);
  expect(inserted.startsWith(before)).toBe(true);
  expect(inserted.indexOf("small-phone.png")).toBeLessThan(inserted.indexOf("response.json"));
  await expect.poll(() => currentSaved(page)).toBe(inserted);
  await page.reload();
  await expect(page.locator(`${CANVAS} .attachment`)).toHaveCount(2);
  expect(await source(page)).toBe(inserted);
  const fileDownload = page.waitForEvent("download");
  await page.locator(`${CANVAS} .attachment`).filter({ hasText: "response.json" }).getByRole("link", { name: "Download response.json" }).click();
  const originalPath = await (await fileDownload).path();
  if (!originalPath) throw new Error("Original download missing");
  expect([...await readFile(originalPath)]).toEqual(document.bytes);
  const archiveDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export", exact: true }).click();
  const archivePath = await (await archiveDownload).path();
  if (!archivePath) throw new Error("Export missing");
  const entries = unzipSync(await readFile(archivePath));
  expect(Object.keys(entries)).toEqual(expect.arrayContaining(["ideas.md", "discussions.md", "workspace.json", "manifest.json"]));
  const original = Object.entries(entries).find(([name]) => name.endsWith("response.json"));
  expect(original ? [...original[1]] : null).toEqual(document.bytes);
  expect(new TextDecoder().decode(entries["ideas.md"])).not.toContain("ideas-asset:");
});

test("export preserves URI-looking labels and portable paths for unmatched filename parentheses", async ({ page }) => {
  await open(page); await select(page, (await source(page)).length);
  await paste(page, [{ ...document, name: "response).json" }]);
  const id = await page.evaluate(async ({ path, selector }) => {
    const helper: typeof Helpers = await import(path); const view = helper.editor(selector);
    const token = helper.attachmentTokens(view.state.doc.toString())[0];
    if (!token) throw new Error("Imported reference missing");
    view.dispatch({ changes: { from: token.from, to: token.to, insert: `[ideas-asset:${token.id}](ideas-asset:${token.id})` } });
    return token.id;
  }, { path: MODULE, selector: CANVAS });
  const pending = page.waitForEvent("download"); await page.getByRole("button", { name: "Export", exact: true }).click();
  const download = await pending; const file = await download.path(); if (!file) throw new Error("Missing archive");
  const zip = unzipSync(await readFile(file)); const markdown = zip["ideas.md"];
  if (!markdown) throw new Error("Missing Markdown");
  const text = new TextDecoder().decode(markdown);
  expect(text).toContain(`[ideas-asset:${id}](assets/`);
  expect(text).toContain("response%29.json"); expect(text).not.toContain("](ideas-asset:");
  expect(Object.keys(zip).some((name) => name.endsWith("response).json"))).toBe(true);
});

test("one mixed paste is one undo step and late persistence never resurrects it", async ({ page }) => {
  await open(page);
  const before = await source(page);
  await select(page, before.length);
  await paste(page, [picture, document], CANVAS, "extra");
  const imported = await source(page);
  await page.keyboard.press(`${mod}+z`);
  expect(await source(page)).toBe(before);
  await expect(page.locator(`${CANVAS} .attachment`)).toHaveCount(0);
  await expect.poll(() => currentSaved(page)).toBe(before);
  await page.keyboard.press(`${mod}+Shift+z`);
  expect(await source(page)).toBe(imported);
  await expect(page.locator(`${CANVAS} .attachment`)).toHaveCount(2);
});

test("frozen selection rejects the whole import without source, selection, history or asset side effects", async ({ page }) => {
  await open(page);
  const before = await source(page);
  await select(page, before.length, 2);
  await paste(page);
  expect(await source(page)).toBe(before);
  await expect(page.locator('.day-input-actions [role="alert"]')).toContainText("frozen");
  const state = await page.evaluate(async ({ path, selector, key }) => {
    const helper: typeof Helpers = await import(path);
    const view = helper.editor(selector);
    const repository = new helper.AssetRepository(key);
    const count = (await repository.list()).length; repository.close();
    return { anchor: view.state.selection.main.anchor, head: view.state.selection.main.head, count };
  }, { path: MODULE, selector: CANVAS, key: KEY });
  expect(state).toEqual({ anchor: before.length, head: 2, count: 0 });
  await page.keyboard.press(`${mod}+z`);
  expect(await source(page)).toBe(before);
});

test("ordinary typing, frozen edge carets and protected history remain intact", async ({ page }) => {
  await open(page);
  await select(page, QUOTE.length);
  await page.keyboard.insertText(" next");
  expect(await source(page)).toContain(`${QUOTE} next`);
  await page.keyboard.press(`${mod}+z`);
  expect(await source(page)).toBe(fixture().sections[0].text);
  await select(page, 0, QUOTE.length);
  await page.keyboard.press("Backspace");
  expect((await source(page)).startsWith(QUOTE)).toBe(true);
});

test("root comments, replies and reply-to-reply share attachment input and independent durable drafts", async ({ page }) => {
  await open(page);
  await openDiscussion(page);
  await paste(page, [picture, document], COMPOSER, "Root comment");
  const rootDraft = await source(page, COMPOSER);
  await expect.poll(async () => JSON.stringify((await saved(page)).drafts)).toContain("Root comment");
  await page.locator('.comment[data-comment="comment"]').getByRole("button", { name: "Reply", exact: true }).click();
  await expect.poll(() => source(page, COMPOSER)).toBe("");
  await paste(page, [document], COMPOSER, "Reply draft");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect.poll(() => source(page, COMPOSER)).toBe(rootDraft);
  await page.locator('.thread-bar button[aria-label="Close"]').click();
  await expect.poll(async () => JSON.stringify((await saved(page)).drafts)).toContain("Reply draft");
  await page.reload();
  await openDiscussion(page);
  expect(await source(page, COMPOSER)).toBe(rootDraft);
  await page.locator('.comment[data-comment="comment"]').getByRole("button", { name: "Reply", exact: true }).click();
  await expect.poll(() => source(page, COMPOSER)).toContain("Reply draft");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.locator('.comment.is-owner .attachment')).toHaveCount(1);
  await expect.poll(() => source(page, COMPOSER)).toBe(rootDraft);
  await page.locator('.comment.is-owner').getByRole("button", { name: "Reply", exact: true }).click();
  await paste(page, [picture], COMPOSER, "Follow-up reply");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.locator('.comment.is-owner')).toHaveCount(2);
  const workspace = await saved(page);
  const posts = workspace.threads.discussion.messages.filter((message) => message.author === "owner");
  expect(posts.map((message) => message.parentId)).toEqual(["comment", "comment"]);
});

test("attachment-only comments submit once and preserve the original in the posted comment", async ({ page }) => {
  await open(page); await openDiscussion(page);
  await paste(page, [picture], COMPOSER);
  const button = page.getByRole("button", { name: "Send", exact: true });
  await button.evaluate((element) => { if (!(element instanceof HTMLButtonElement)) throw new Error("Send button missing"); element.click(); element.click(); });
  await expect(page.locator('.comment.is-owner .attachment')).toHaveCount(1);
  await expect.poll(() => source(page, COMPOSER)).toBe("");
  await page.reload(); await openDiscussion(page);
  await expect(page.locator('.comment.is-owner .attachment')).toHaveCount(1);
  expect((await saved(page)).threads.discussion.messages.filter((message) => message.author === "owner")).toHaveLength(1);
});

test("same-account copy into another editor gets new occurrence identity but reuses bytes", async ({ page }) => {
  await open(page); await select(page, (await source(page)).length);
  await paste(page, [document]);
  const body = await source(page);
  const fragment = body.slice(body.indexOf("[response.json]"));
  await openDiscussion(page); await paste(page, [], COMPOSER, fragment);
  const copied = await source(page, COMPOSER);
  expect(copied).toContain("response.json"); expect(copied).not.toBe(fragment);
  await expect.poll(async () => JSON.stringify((await saved(page)).drafts)).toContain("response.json");
  const assets = await page.evaluate(async ({ path, key }) => {
    const helper: typeof Helpers = await import(path); const repo = new helper.AssetRepository(key);
    const list = await repo.list(); repo.close(); return list;
  }, { path: MODULE, key: KEY });
  expect(assets).toHaveLength(2);
  expect(new Set(assets.map((asset) => asset.blobId)).size).toBe(1);
});

test("unavailable cross-account references are rejected rather than resolving private IDs", async ({ page }) => {
  await open(page); await select(page, (await source(page)).length);
  const before = await source(page);
  await paste(page, [], CANVAS, "![private](ideas-asset:asset_other_account)");
  expect(await source(page)).toBe(before);
  await expect(page.locator('.day-input-actions [role="alert"]')).toContainText("unavailable");
});

test("picker returns to its captured caret and supports arbitrary originals", async ({ page }) => {
  await open(page); const before = await source(page); await select(page, before.length);
  const chooser = page.waitForEvent("filechooser");
  await page.locator('.day-input-actions').getByRole("button", { name: "Add files", exact: true }).click();
  await (await chooser).setFiles({ name: "archive.zip", mimeType: "application/zip", buffer: Buffer.from([0, 255, 2, 3]) });
  await expect(page.locator(`${CANVAS} .attachment`)).toContainText("archive.zip");
  expect((await source(page)).startsWith(before)).toBe(true);
  await expect.poll(() => currentSaved(page)).toContain("archive.zip");
});

test("a frozen mixed unit retains images, original source and its thread when dragged", async ({ page, isMobile }) => {
  test.skip(isMobile, "Native mouse drag is desktop-only; physical touch acceptance is separate.");
  await open(page, fixture(`${QUOTE}\n\nEditable note.\n\nTarget text.`), 0.1);
  await select(page, `${QUOTE}\n\nEditable note.`.length);
  await paste(page, [picture, document]);
  await expect(page.locator(`${CANVAS} .attachment.cm-taken`)).toHaveCount(2);
  await expect.poll(async () => Object.values((await saved(page)).threads).some((thread) => thread.quote.includes("ideas-asset:"))).toBe(true);
  const workspace = await saved(page);
  const frozen = Object.values(workspace.threads).find((thread) => thread.quote.includes("ideas-asset:"));
  if (!frozen) throw new Error("Mixed source was not accepted");
  await page.goto("/index.html?lang=en&settle=3600");
  const dragged = page.locator(`.cm-taken[data-thread="${frozen.id}"]`).first();
  await expect(dragged).toBeVisible();
  await dragToEnd(page, dragged);
  await expect.poll(() => source(page)).not.toBe(workspace.sections[0].text);
  const after = await source(page);
  expect(after.split(frozen.quote)).toHaveLength(2);
  expect(after.endsWith(frozen.quote)).toBe(true);
  await expect.poll(() => currentSaved(page)).toBe(after);
  expect((await saved(page)).threads[frozen.id].quote).toBe(frozen.quote);
  await expect(page.locator(`${CANVAS} .attachment.cm-taken`)).toHaveCount(2);
  // A following external drop must not delete a stale dragged range or duplicate its assets.
  await select(page, 0); await paste(page, [document]);
  expect((await source(page)).split(frozen.quote)).toHaveLength(2);
  await expect(page.locator(`${CANVAS} .attachment`)).toHaveCount(3);
});

test("storage rejection retains content and originals; retry heals the current revision after undo", async ({ page }) => {
  await open(page); const before = await source(page); await select(page, before.length);
  await page.evaluate(() => {
    const original = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (value, key) {
      if (this.name === "workspaces") throw new DOMException("Test quota exhausted", "QuotaExceededError");
      return key === undefined ? original.call(this, value) : original.call(this, value, key);
    };
    window.addEventListener("restore-test-storage", () => { IDBObjectStore.prototype.put = original; }, { once: true });
  });
  await paste(page, [picture]);
  await expect(page.locator('.persistence-error')).toContainText("could not be saved");
  await page.keyboard.press(`${mod}+z`);
  expect(await source(page)).toBe(before);
  await page.evaluate(() => window.dispatchEvent(new Event("restore-test-storage")));
  await page.locator('.persistence-error').getByRole("button", { name: "Retry" }).click();
  await expect(page.locator('.persistence-error')).toHaveCount(0);
  await expect.poll(() => currentSaved(page)).toBe(before);
  await page.locator(`${CANVAS} .cm-content`).focus();
  await page.keyboard.press(`${mod}+Shift+z`);
  await expect(page.locator(`${CANVAS} .attachment`)).toHaveCount(1);
  const download = page.waitForEvent("download");
  await page.locator(`${CANVAS} .attachment`).getByRole("link", { name: "Download small-phone.png" }).click();
  const path = await (await download).path(); if (!path) throw new Error("Original unavailable after recovery");
  expect([...await readFile(path)]).toEqual(PNG);
});

test("failed identity never opens the shared anonymous workspace", async ({ page }) => {
  await page.route("**/oauth2/userinfo", (route) => route.fulfill({ status: 401, body: "not signed in" }));
  const original = JSON.stringify(fixture());
  await page.addInitScript((value) => localStorage.setItem("corral.ideas.preview.v1", value), original);
  await page.goto("/index.html?lang=en");
  await expect(page.locator('.startup-error')).toContainText("account could not be verified");
  await expect(page.locator('.cm-editor')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("corral.ideas.preview.v1"))).toBe(original);
});

test("source delimiters cannot turn part of a frozen sentence into a new attachment", async ({ page }) => {
  await open(page);
  const original = await source(page);
  await page.evaluate(async ({ path, selector, end }) => {
    const helper: typeof Helpers = await import(path);
    helper.editor(selector).dispatch({ changes: [{ from: 0, insert: "![" }, { from: end, insert: "](ideas-asset:asset_other)" }] });
  }, { path: MODULE, selector: CANVAS, end: QUOTE.length });
  expect(await source(page)).toBe(original);
  await expect(page.locator(`${CANVAS} .attachment`)).toHaveCount(0);
});

test("desktop and mobile history events cannot undo an accepted attachment", async ({ page }) => {
  await open(page, fixture(), 0.1); await select(page, (await source(page)).length);
  await paste(page, [picture]);
  await expect(page.locator(`${CANVAS} .attachment.cm-taken`)).toHaveCount(1);
  const accepted = await source(page);
  for (const inputType of ["historyUndo", "historyRedo"]) {
    await page.locator(`${CANVAS} .cm-content`).evaluate((element, inputType) => element.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType })), inputType);
    expect(await source(page)).toBe(accepted);
  }
});

test("a failed durability gate is retried after storage recovery instead of losing take-up eligibility", async ({ page }) => {
  await open(page, fixture(), 0.1); await select(page, (await source(page)).length);
  await page.evaluate(() => {
    const original = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (value, key) {
      if (this.name === "workspaces") throw new DOMException("Test quota", "QuotaExceededError");
      return key === undefined ? original.call(this, value) : original.call(this, value, key);
    };
    window.addEventListener("recover-gate-storage", () => { IDBObjectStore.prototype.put = original; }, { once: true });
  });
  await paste(page, [picture]);
  await expect(page.locator('.persistence-error')).toBeVisible();
  await expect(page.locator(`${CANVAS} .attachment.cm-taken`)).toHaveCount(0);
  await page.evaluate(() => window.dispatchEvent(new Event("recover-gate-storage")));
  await page.locator('.persistence-error').getByRole("button", { name: "Retry" }).click();
  await expect(page.locator(`${CANVAS} .attachment.cm-taken`)).toHaveCount(1);
});

test("typing during a pending send survives, and retrying the same operation never duplicates its post", async ({ page }) => {
  await open(page);
  const result = await page.evaluate(async ({ path, day, initial }) => {
    const helper: typeof Helpers = await import(path);
    const store = await helper.Store.load(() => initial, "send-race@example.test");
    const key = "draft:discussion:root";
    store.updateDraft(key, "First submission");
    const original = store.assets.repository.save.bind(store.assets.repository);
    let first = true;
    const release: { run: () => void } = { run: () => { throw new Error("Save gate was not entered"); } };
    const entered = { resolve: () => {} };
    const gate = new Promise<void>((resolve) => { entered.resolve = resolve; });
    store.assets.repository.save = (write) => {
      if (!first) return original(write);
      first = false; entered.resolve();
      return new Promise<number>((resolve, reject) => { release.run = () => { void original(write).then(resolve, reject); }; });
    };
    const sending = store.submitComment("discussion", "First submission", key, "message_send_race");
    await gate;
    store.updateDraft(key, "New typing while the earlier send is pending");
    release.run(); await sending; await store.flush();
    await store.submitComment("discussion", "First submission", key, "message_send_race");
    const record = await store.assets.repository.load();
    if (!record) throw new Error("Send snapshot absent");
    const data = helper.validateWorkspace(record.data);
    // Equal text typed again is a new revision, not the already-sent draft.
    store.updateDraft(key, "First submission"); await store.flush();
    const reopened = await helper.Store.load(() => initial, "send-race@example.test");
    const retyped = reopened.draft(key).text;
    reopened.close(); store.close();
    return { text: data.drafts[key].text, posts: data.threads.discussion.messages.filter((message) => message.author === "owner"), retyped, day };
  }, { path: MODULE, day: DAY, initial: fixture() });
  expect(result.text).toBe("New typing while the earlier send is pending");
  expect(result.posts).toHaveLength(1);
  expect(result.posts[0].text).toBe("First submission");
  expect(result.retyped).toBe("First submission");
});

test("delayed take-up refuses stale offsets and later accepts the mapped current source", async ({ page }) => {
  await open(page);
  const result = await page.evaluate(async ({ path, day, initial }) => {
    const helper: typeof Helpers = await import(path);
    const store = await helper.Store.load(() => initial, "takeup-race@example.test");
    const coordinator = new helper.PreviewCoordinator(store);
    const section = store.section(day);
    if (!section) throw new Error("Source absent");
    const candidate = "Corral: repair the current button.";
    const original = `${section.text}\n\n${candidate}`;
    store.updateSection(day, original, section.spans); await store.flush();
    let unsubscribe = () => {};
    const reading = new Promise<void>((resolve) => { unsubscribe = store.subscribe(() => { if (store.state.reading.length) { unsubscribe(); resolve(); } }); });
    const delayed = coordinator.onSettled(day, original.indexOf(candidate), original.length);
    await reading;
    const prefix = "An earlier note.\n\n";
    store.updateSection(day, prefix + original, section.spans.map((span) => ({ ...span, from: span.from + prefix.length, to: span.to + prefix.length })));
    const staleAccepted = await delayed;
    await store.flush();
    const currentAccepted = await coordinator.onSettled(day, prefix.length + original.indexOf(candidate), prefix.length + original.length);
    const quotes = Object.values(store.state.threads).map((thread) => thread.quote);
    await store.flush();
    // This test uses a private controller; close its database after the exact source assertion.
    store.close();
    return { staleAccepted, currentAccepted, quotes };
  }, { path: MODULE, day: DAY, initial: fixture() });
  expect(result.staleAccepted).toBe(false);
  expect(result.currentAccepted).toBe(true);
  expect(result.quotes).toEqual([QUOTE, "Corral: repair the current button."]);
});

test("an old failed send cannot restore text after the user intentionally clears a newer draft", async ({ page }) => {
  await open(page);
  const result = await page.evaluate(async ({ path, initial }) => {
    const helper: typeof Helpers = await import(path);
    const store = await helper.Store.load(() => initial, "failed-send-race@example.test");
    const key = "draft:discussion:root";
    store.updateDraft(key, "Old submission");
    const release: { run: () => void } = { run: () => { throw new Error("Gate missing"); } };
    let enter: () => void = () => {};
    const entered = new Promise<void>((resolve) => { enter = resolve; });
    store.assets.repository.save = () => { enter(); return new Promise<number>((_resolve, reject) => { release.run = () => reject(new helper.RepositoryError("quota", "Test quota")); }); };
    const pending = store.submitComment("discussion", "Old submission", key, "message_old_submission");
    await entered;
    store.updateDraft(key, "A newer draft"); store.updateDraft(key, "");
    release.run();
    let rejected = false; try { await pending; } catch { rejected = true; }
    const draft = store.draft(key); store.close();
    return { rejected, draft };
  }, { path: MODULE, initial: fixture() });
  expect(result.rejected).toBe(true);
  expect(result.draft.text).toBe("");
  expect(result.draft.pending).toBeUndefined();
});

test("a wrapped text-only frozen sentence still drags with no added spaces and keeps its last-line caret", async ({ page, isMobile }) => {
  test.skip(isMobile, "Native mouse drag is desktop-only.");
  const quote = "冻结句的文字与光标位置都必须保留。".repeat(9);
  const data = fixture();
  data.threads.discussion.quote = quote;
  data.sections[0].text = `${quote}\n\nTarget text.`;
  data.sections[0].spans[0].to = quote.length;
  await open(page, data);
  const frozen = page.locator('.cm-taken[data-thread="discussion"]');
  expect(await frozen.evaluate((element) => element.getClientRects().length)).toBeGreaterThan(1);
  await dragToEnd(page, frozen);
  await expect.poll(() => source(page)).toBe(`Target text.${quote}`);
  const caret = await page.evaluate(async ({ path, selector }) => {
    const helper: typeof Helpers = await import(path); const view = helper.editor(selector);
    const element = view.contentDOM.querySelector('.cm-taken[data-thread="discussion"]');
    const lines = element?.getClientRects();
    const last = lines?.[lines.length - 1]; const position = view.coordsAtPos(view.state.doc.length, -1);
    if (!last || !position) throw new Error("Wrapped caret not measurable");
    return { lastTop: last.top, caretTop: position.top };
  }, { path: MODULE, selector: CANVAS });
  expect(Math.abs(caret.lastTop - caret.caretTop)).toBeLessThanOrEqual(2);
});

test("a frozen asset unit spanning paragraphs moves as exact source without leaving duplicate separators", async ({ page, isMobile }) => {
  test.skip(isMobile, "Native mouse drag is desktop-only.");
  await open(page);
  const quote = await page.evaluate(async ({ path, key, day, picture }) => {
    const helper: typeof Helpers = await import(path); const repository = new helper.AssetRepository(key);
    const record = await repository.load(); if (!record) throw new Error("Canonical fixture missing");
    const data = helper.validateWorkspace(record.data);
    const asset = { id: "asset_multiline", blobId: "asset_multiline_blob", name: picture.name, type: picture.type, size: picture.bytes.length, createdAt: Date.now() };
    const quote = `First line.\n\n${helper.attachmentSource({ id: asset.id, name: asset.name, image: true })}\n\nLast line.`;
    const before = data.sections[0].text.slice(0, data.sections[0].spans[0].to);
    const from = before.length + 2;
    data.sections[0].text = `${before}\n\n${quote}\n\nTarget text.`;
    data.sections[0].spans.push({ threadId: "mixed_discussion", from, to: from + quote.length });
    data.threads.mixed_discussion = { id: "mixed_discussion", day, quote, attachments: helper.attachmentTokens(quote), createdAt: Date.now() - 5000, lastReadAt: 0, summary: "", messages: [], questions: [], tasks: [] };
    await repository.save({ expectedRevision: record.revision, data, assets: [asset], blobs: [{ id: asset.blobId, blob: new Blob([new Uint8Array(picture.bytes)], { type: picture.type }) }], referencedIds: [asset.id], audit: [{ actor: "system", action: "fixture.create", targetId: "mixed_discussion", at: Date.now() }] });
    repository.close(); return quote;
  }, { path: MODULE, key: KEY, day: DAY, picture });
  await page.reload();
  const frozen = page.locator('.cm-taken[data-thread="mixed_discussion"]').first();
  await expect(frozen).toBeVisible();
  await dragToEnd(page, frozen);
  await expect.poll(() => source(page)).toBe(`${QUOTE}\n\nTarget text.${quote}`);
  await expect(page.locator(`${CANVAS} .attachment.cm-taken`)).toHaveCount(1);
});
