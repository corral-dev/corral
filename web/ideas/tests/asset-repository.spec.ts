import { expect } from "@playwright/test";
import { test } from "./persistent-page";
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import type { WorkspaceWrite } from "../src/assets/repository";

type RepositoryModule = typeof import("../src/assets/repository");
const fixturePath = "/__asset_repository_fixture__/";
let bundle = "";

// A routed, test-only ES module exercises real browser IDB without product globals/hooks.
test.beforeAll(async () => {
  const output = await build({
    entryPoints: [fileURLToPath(new URL("../src/assets/repository.ts", import.meta.url))],
    bundle: true, write: false, format: "esm", target: "es2022",
  });
  const file = output.outputFiles.find(value => value.path.endsWith(".js") || value.path === "<stdout>");
  if (!file) throw new Error("Repository fixture bundle was not produced.");
  bundle = file.text;
});

test.beforeEach(async ({ page }) => {
  await page.route(`**${fixturePath}**`, route => route.fulfill({
    contentType: route.request().url().endsWith("repository.js") ? "text/javascript" : "text/html",
    body: route.request().url().endsWith("repository.js") ? bundle : "<!doctype html><html><title>Repository fixture</title></html>",
  }));
  await page.goto(`${fixturePath}index.html`);
});

function write(expectedRevision = 0): WorkspaceWrite {
  return {
    expectedRevision, data: { text: "original" }, assets: [], blobs: [], referencedIds: [],
    audit: [{ actor: "owner", action: "workspace.save", targetId: "workspace", at: 1 }],
  };
}

const modulePath = `${fixturePath}repository.js`;

test("account-scoped metadata, snapshots and original bytes survive reload", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository }: RepositoryModule = await import(modulePath);
    const name = `repository-${crypto.randomUUID()}`;
    const alice = new AssetRepository("alice", name);
    const bob = new AssetRepository("bob", name);
    const asset = { id: "asset_shared", blobId: "asset_original", name: "original.bin", type: "application/octet-stream", size: 4, createdAt: 1 };
    await alice.save({ ...initial, data: { text: "alice" }, assets: [asset], blobs: [{ id: asset.blobId, blob: new Blob([new Uint8Array([0, 255, 1, 42])], { type: asset.type }) }], referencedIds: [asset.id] });
    await bob.save({ ...initial, data: { text: "bob" }, assets: [{ ...asset, name: "bob.bin" }], blobs: [{ id: asset.blobId, blob: new Blob([new Uint8Array([4, 3, 2, 1])], { type: asset.type }) }], referencedIds: [asset.id] });
    await bob.save({ ...initial, expectedRevision: 1, assets: [{ ...asset, id: "asset_bob_only", blobId: "asset_private" }], blobs: [{ id: "asset_private", blob: new Blob([new Uint8Array([9, 9, 9, 9])], { type: asset.type }) }] });
    const absent = await alice.readBlob("asset_private");
    const aliceAssets = await alice.list();
    const bobAssets = await bob.list();
    const bobBlob = await bob.readBlob(asset.blobId);
    alice.close(); bob.close();
    const reloaded = new AssetRepository("alice", name);
    const snapshot = await reloaded.load();
    const blob = await reloaded.readBlob(asset.blobId);
    reloaded.close();
    return { name, snapshot, absent, aliceAssets, bobAssets, bytes: blob ? [...new Uint8Array(await blob.arrayBuffer())] : null, bobBytes: bobBlob ? [...new Uint8Array(await bobBlob.arrayBuffer())] : null };
  }, { modulePath, initial: write() });
  expect(result.snapshot).toEqual({ schema: 2, revision: 1, data: { text: "alice" } });
  expect(result.absent).toBeNull();
  expect(result.aliceAssets.map(asset => asset.name)).toEqual(["original.bin"]);
  expect(result.bobAssets).toHaveLength(2);
  expect(result.bytes).toEqual([0, 255, 1, 42]);
  expect(result.bobBytes).toEqual([4, 3, 2, 1]);
  await page.reload();
  const afterReload = await page.evaluate(async ({ modulePath, name }) => {
    const { AssetRepository }: RepositoryModule = await import(modulePath);
    const repo = new AssetRepository("alice", name);
    const snapshot = await repo.load();
    const blob = await repo.readBlob("asset_original");
    repo.close();
    return { snapshot, bytes: blob ? [...new Uint8Array(await blob.arrayBuffer())] : null };
  }, { modulePath, name: result.name });
  expect(afterReload).toEqual({ snapshot: result.snapshot, bytes: [0, 255, 1, 42] });
});

test("native CAS makes create-only and stale writes conflict across instances", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const name = `repository-${crypto.randomUUID()}`;
    const first = new AssetRepository("owner", name);
    const second = new AssetRepository("owner", name);
    const settled = await Promise.allSettled([first.save({ ...initial, data: "first" }), second.save({ ...initial, data: "second" })]);
    const outcomes = settled.map(value => value.status === "fulfilled" ? value.value : value.reason instanceof RepositoryError ? value.reason.code : "unexpected");
    let stale = "";
    try { await first.save(initial); } catch (error) { stale = error instanceof RepositoryError ? error.code : "unexpected"; }
    const loaded = await second.load();
    const healed = await first.save({ ...initial, expectedRevision: 1, data: "after conflict" });
    const final = await second.load();
    first.close(); second.close();
    return { outcomes, stale, loaded, healed, final };
  }, { modulePath, initial: write() });
  expect(result.outcomes.sort()).toEqual([1, "conflict"]);
  expect(result.stale).toBe("conflict");
  expect(result.loaded?.revision).toBe(1);
  expect(result.healed).toBe(2);
  expect(result.final).toEqual({ schema: 2, revision: 2, data: "after conflict" });
});

test("original IDs are insert-only, metadata immutable, aliases reuse original bytes", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const repo = new AssetRepository("owner", `repository-${crypto.randomUUID()}`);
    const blob = new Blob(["AB"], { type: "text/plain" });
    const asset = { id: "asset_first", blobId: "asset_original", name: "one.txt", type: blob.type, size: blob.size, createdAt: 1 };
    await repo.save({ ...initial, assets: [asset], blobs: [{ id: asset.blobId, blob }], referencedIds: [asset.id] });
    const errors: string[] = [];
    for (const next of [blob, new Blob(["CD"], { type: blob.type })]) {
      try { await repo.save({ ...initial, expectedRevision: 1, blobs: [{ id: asset.blobId, blob: next }] }); }
      catch (error) { errors.push(error instanceof RepositoryError ? error.code : "unexpected"); }
    }
    try { await repo.save({ ...initial, expectedRevision: 1, assets: [{ ...asset, name: "changed.txt" }] }); }
    catch (error) { errors.push(error instanceof RepositoryError ? error.code : "unexpected"); }
    const alias = { ...asset, id: "asset_alias", name: "copy.txt" };
    const revision = await repo.save({ ...initial, expectedRevision: 1, assets: [alias, asset], referencedIds: [alias.id, asset.id] });
    const repeated = await repo.save({ ...initial, expectedRevision: revision, assets: [asset, alias], referencedIds: [alias.id] });
    const saved = await repo.readBlob(asset.blobId);
    const assets = await repo.list();
    repo.close();
    return { errors, revision, repeated, text: saved ? await saved.text() : null, assets };
  }, { modulePath, initial: write() });
  expect(result.errors).toEqual(["immutable", "immutable", "immutable"]);
  expect(result.revision).toBe(2);
  expect(result.repeated).toBe(3);
  expect(result.text).toBe("AB");
  expect(result.assets.map(asset => asset.name).sort()).toEqual(["copy.txt", "one.txt"]);
});

test("missing account references roll back snapshots, staged originals, metadata and audit", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const name = `repository-${crypto.randomUUID()}`;
    const repo = new AssetRepository("owner", name);
    const foreign = new AssetRepository("other", name);
    const blob = new Blob(["original"], { type: "text/plain" });
    const asset = { id: "asset_staged", blobId: "asset_original", name: "one.txt", type: blob.type, size: blob.size, createdAt: 1 };
    await repo.save(initial);
    await foreign.save({ ...initial, assets: [asset], blobs: [{ id: asset.blobId, blob }], referencedIds: [asset.id] });
    const errors: string[] = [];
    for (const candidate of [
      { ...initial, expectedRevision: 1, data: "must roll back", assets: [asset], blobs: [{ id: asset.blobId, blob }], referencedIds: ["asset_missing"] },
      { ...initial, expectedRevision: 1, data: "foreign metadata", referencedIds: [asset.id] },
      { ...initial, expectedRevision: 1, data: "foreign bytes", assets: [asset], referencedIds: [asset.id] },
    ]) {
      try { await repo.save(candidate); } catch (error) { errors.push(error instanceof RepositoryError ? error.code : "unexpected"); }
    }
    const snapshot = await repo.load();
    const assets = await repo.list();
    const original = await repo.readBlob(asset.blobId);
    repo.close(); foreign.close();
    const audits = await new Promise<unknown[]>((resolve, reject) => {
      const request = indexedDB.open(name, 2);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction("audits", "readonly");
        const rows: IDBRequest<unknown[]> = tx.objectStore("audits").getAll();
        tx.oncomplete = () => { db.close(); resolve(rows.result); };
        tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
    return { errors, snapshot, assets, original, audits };
  }, { modulePath, initial: write() });
  expect(result.errors).toEqual(["missing-asset", "missing-asset", "missing-blob"]);
  expect(result.snapshot).toEqual({ schema: 2, revision: 1, data: { text: "original" } });
  expect(result.assets).toEqual([]);
  expect(result.original).toBeNull();
  expect(result.audits).toEqual([
    { id: 1, account: "owner", revision: 1, actor: "owner", action: "workspace.save", targetId: "workspace", at: 1 },
    { id: 2, account: "other", revision: 1, actor: "owner", action: "workspace.save", targetId: "workspace", at: 1 },
  ]);
});

test("request success is not commit: abort after snapshot put rejects and rolls back audit", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const name = `repository-${crypto.randomUUID()}`;
    const repo = new AssetRepository("owner", name);
    await repo.load();
    const put = IDBObjectStore.prototype.put;
    let sawSuccess = false;
    IDBObjectStore.prototype.put = function (value: unknown, key?: IDBValidKey) {
      const request = key === undefined ? put.call(this, value) : put.call(this, value, key);
      if (this.name === "workspaces") request.addEventListener("success", () => {
        sawSuccess = true;
        this.transaction.abort();
      }, { once: true });
      return request;
    };
    let errorCode = "";
    try { await repo.save(initial); } catch (error) { errorCode = error instanceof RepositoryError ? error.code : "unexpected"; }
    finally { IDBObjectStore.prototype.put = put; }
    const afterAbort = await repo.load();
    const revision = await repo.save(initial);
    repo.close();
    const audits = await new Promise<unknown[]>((resolve, reject) => {
      const request = indexedDB.open(name, 2);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction("audits", "readonly");
        const rows: IDBRequest<unknown[]> = tx.objectStore("audits").getAll();
        tx.oncomplete = () => { db.close(); resolve(rows.result); };
        tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
    return { sawSuccess, errorCode, afterAbort, revision, audits };
  }, { modulePath, initial: write() });
  expect(result.sawSuccess).toBe(true);
  expect(result.errorCode).toBe("storage");
  expect(result.afterAbort).toBeNull();
  expect(result.revision).toBe(1);
  expect(result.audits).toHaveLength(1);
});

test("invalid envelopes, missing audit and mismatched originals cannot persist", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const repo = new AssetRepository("owner", `repository-${crypto.randomUUID()}`);
    const blob = new Blob(["abc"], { type: "text/plain" });
    const asset = { id: "asset_first", blobId: "asset_original", name: "one.txt", type: blob.type, size: blob.size, createdAt: 1 };
    const errors: string[] = [];
    const candidates: WorkspaceWrite[] = [
      { ...initial, expectedRevision: -1 },
      { ...initial, audit: [] },
      { ...initial, data: () => "not cloneable" },
      { ...initial, referencedIds: ["../other"] },
      { ...initial, assets: [{ ...asset, id: `asset_${"a".repeat(91)}` }] },
      { ...initial, assets: [{ ...asset, size: 4 }], blobs: [{ id: asset.blobId, blob }] },
      { ...initial, assets: [{ ...asset, type: "application/json" }], blobs: [{ id: asset.blobId, blob }] },
    ];
    for (const candidate of candidates) {
      try { await repo.save(candidate); } catch (error) { errors.push(error instanceof RepositoryError ? error.code : "unexpected"); }
    }
    const before = await repo.load();
    const assets = await repo.list();
    const original = await repo.readBlob(asset.blobId);
    const revision = await repo.save(initial);
    repo.close();
    return { errors, before, assets, original, revision };
  }, { modulePath, initial: write() });
  expect(result.errors).toEqual(Array(7).fill("invalid"));
  expect(result.before).toBeNull();
  expect(result.assets).toEqual([]);
  expect(result.original).toBeNull();
  expect(result.revision).toBe(1);
});

test("quota errors abort the complete write and a later retry can heal it", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const repo = new AssetRepository("owner", `repository-${crypto.randomUUID()}`);
    await repo.load();
    const add = IDBObjectStore.prototype.add;
    IDBObjectStore.prototype.add = function (value: unknown, key?: IDBValidKey) {
      if (this.name === "audits") throw new DOMException("Injected quota failure", "QuotaExceededError");
      return key === undefined ? add.call(this, value) : add.call(this, value, key);
    };
    const blob = new Blob(["abc"], { type: "text/plain" });
    const asset = { id: "asset_first", blobId: "asset_original", name: "one.txt", type: blob.type, size: blob.size, createdAt: 1 };
    const candidate = { ...initial, assets: [asset], blobs: [{ id: asset.blobId, blob }], referencedIds: [asset.id] };
    let code = "";
    try { await repo.save(candidate); } catch (error) { code = error instanceof RepositoryError ? error.code : "unexpected"; }
    finally { IDBObjectStore.prototype.add = add; }
    const snapshot = await repo.load();
    const assets = await repo.list();
    const original = await repo.readBlob(asset.blobId);
    const revision = await repo.save(candidate);
    repo.close();
    return { code, snapshot, assets, original, revision };
  }, { modulePath, initial: write() });
  expect(result).toEqual({ code: "quota", snapshot: null, assets: [], original: null, revision: 1 });
});

test("save captures mutable caller input before opening the database", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository }: RepositoryModule = await import(modulePath);
    const repo = new AssetRepository("owner", `repository-${crypto.randomUUID()}`);
    const data = { text: "before" };
    const asset = { id: "asset_first", blobId: "asset_original", name: "before.txt", type: "text/plain", size: 3, createdAt: 1 };
    const event = { actor: "owner", action: "workspace.save", targetId: "workspace", at: 1 } satisfies WorkspaceWrite["audit"][number];
    const pending = repo.save({ ...initial, data, assets: [asset], blobs: [{ id: asset.blobId, blob: new Blob(["abc"], { type: asset.type }) }], audit: [event], referencedIds: [asset.id] });
    data.text = "after"; asset.name = "after.txt"; event.action = "changed";
    await pending;
    const loaded = await repo.load();
    const assets = await repo.list();
    repo.close();
    return { loaded, assets };
  }, { modulePath, initial: write() });
  expect(result.loaded?.data).toEqual({ text: "before" });
  expect(result.assets.map(asset => asset.name)).toEqual(["before.txt"]);
});

test("blocked upgrades reject, preserve old data and release late open attempts", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const name = `repository-${crypto.randomUUID()}`;
    const blocker = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(name, 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const repo = new AssetRepository("owner", name);
    let code = "";
    try { await repo.load(); } catch (error) { code = error instanceof RepositoryError ? error.code : "unexpected"; }
    blocker.close();
    const version = await new Promise<number>((resolve, reject) => {
      const request = indexedDB.open(name);
      request.onsuccess = () => { const db = request.result; const value = db.version; db.close(); resolve(value); };
      request.onerror = () => reject(request.error);
    });
    let retry = "";
    try { await repo.load(); } catch (error) { retry = error instanceof RepositoryError ? error.code : "unexpected"; }
    repo.close();
    return { code, version, retry };
  }, { modulePath });
  expect(result).toEqual({ code: "blocked", version: 1, retry: "schema" });
});

test("close during open releases late connections; versionchange closes an active repository", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const name = `repository-${crypto.randomUUID()}`;
    const setup = new AssetRepository("owner", name);
    await setup.load(); setup.close();
    const late = new AssetRepository("owner", name);
    const opening = late.load();
    late.close();
    let closed = "";
    try { await opening; } catch (error) { closed = error instanceof RepositoryError ? error.code : "unexpected"; }
    const active = new AssetRepository("owner", name);
    await active.load();
    const version = await new Promise<number>((resolve, reject) => {
      const request = indexedDB.open(name, 3);
      request.onsuccess = () => { const db = request.result; const value = db.version; db.close(); resolve(value); };
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("Repository leaked a connection."));
    });
    let changed = "";
    try { await active.load(); } catch (error) { changed = error instanceof RepositoryError ? error.code : "unexpected"; }
    const newer = new AssetRepository("owner", name);
    let unsupported = "";
    try { await newer.load(); } catch (error) { unsupported = error instanceof RepositoryError ? error.code : "unexpected"; }
    newer.close();
    return { closed, changed, version, unsupported };
  }, { modulePath });
  expect(result).toEqual({ closed: "closed", changed: "closed", version: 3, unsupported: "schema" });
});

test("load fails closed on unsupported or corrupt canonical envelopes", async ({ page }) => {
  const result = await page.evaluate(async ({ modulePath, initial }) => {
    const { AssetRepository, RepositoryError }: RepositoryModule = await import(modulePath);
    const name = `repository-${crypto.randomUUID()}`;
    const setup = new AssetRepository("owner", name);
    await setup.save(initial); setup.close();
    const errors: string[] = [];
    for (const value of [
      { schema: 3, revision: 1, data: {} },
      { schema: 2, revision: 0, data: {} },
      { schema: 2, revision: 1.5, data: {} },
      { schema: 2, revision: 1 },
    ]) {
      await new Promise<void>((resolve, reject) => {
        const request = indexedDB.open(name, 2);
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result;
          const tx = db.transaction("workspaces", "readwrite");
          tx.objectStore("workspaces").put({ account: "owner", ...value });
          tx.oncomplete = () => { db.close(); resolve(); };
          tx.onabort = () => { db.close(); reject(tx.error); };
        };
      });
      const repo = new AssetRepository("owner", name);
      try { await repo.load(); } catch (error) { errors.push(error instanceof RepositoryError ? error.code : "unexpected"); }
      repo.close();
    }
    return errors;
  }, { modulePath, initial: write() });
  expect(result).toEqual(["schema", "schema", "schema", "schema"]);
});
