/** Account-local persistence only. Store validates workspace data and serializes its saves. */
export interface AssetMetadata {
  id: string;
  blobId: string;
  name: string;
  type: string;
  size: number;
  createdAt: number;
}

export interface NewBlob { id: string; blob: Blob }
export interface AuditEvent { actor: "owner" | "system"; action: string; targetId: string; at: number }
export interface SavedWorkspace { schema: 2; revision: number; data: unknown }
/** Complete caller-derived references and a nonempty audit batch accompany every snapshot. */
export interface WorkspaceWrite {
  expectedRevision: number;
  data: unknown;
  assets: readonly AssetMetadata[];
  blobs: readonly NewBlob[];
  referencedIds: readonly string[];
  audit: readonly AuditEvent[];
}

/** Codes: conflict, immutable, missing-asset, missing-blob, invalid, schema, blocked, closed, quota, storage. */
export class RepositoryError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "RepositoryError";
  }
}

const VERSION = 2;
const STORES = ["workspaces", "assets", "blobs", "audits"];
const SAFE_ID = /^(?:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|asset_[a-z0-9_-]+)$/i;
const MIME = /^[a-z0-9!#$%&'*+.^_`|~-]+\/[a-z0-9!#$%&'*+.^_`|~-]+(?:;[\x20-\x7e]*)?$/;

type RecordValue = Record<string, unknown>;
type Guard = (operation: () => void) => void;

function record(value: unknown): value is RecordValue {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max && !/[\x00-\x1f\x7f]/.test(value);
}

function validId(value: unknown): value is string {
  return typeof value === "string" && value.length <= 96 && SAFE_ID.test(value);
}

function integer(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function invalid(message: string): never {
  throw new RepositoryError("invalid", message);
}

function metadata(value: unknown, code: string): AssetMetadata {
  if (!record(value) || !validId(value.id) || !validId(value.blobId)
    || typeof value.name !== "string" || !value.name.length || value.name.length > 4096 || value.name.includes("\0")
    || typeof value.type !== "string" || value.type.length > 255 || (value.type !== "" && !MIME.test(value.type))
    || !integer(value.size) || !integer(value.createdAt)) {
    throw new RepositoryError(code, "Invalid asset metadata.");
  }
  return { id: value.id, blobId: value.blobId, name: value.name, type: value.type, size: value.size, createdAt: value.createdAt };
}

function sameMetadata(a: AssetMetadata, b: AssetMetadata): boolean {
  return a.id === b.id && a.blobId === b.blobId && a.name === b.name && a.type === b.type
    && a.size === b.size && a.createdAt === b.createdAt;
}

function workspace(value: unknown, account: string): SavedWorkspace | null {
  if (value === undefined) return null;
  if (!record(value) || value.account !== account || value.schema !== 2 || !integer(value.revision)
    || value.revision === 0 || !Object.prototype.hasOwnProperty.call(value, "data")) {
    throw new RepositoryError("schema", "Unsupported or corrupt workspace snapshot.");
  }
  return { schema: 2, revision: value.revision, data: value.data };
}

function storedAsset(value: unknown, account: string, id: string): AssetMetadata {
  if (!record(value) || value.account !== account || value.id !== id) {
    throw new RepositoryError("schema", "Corrupt account asset record.");
  }
  return metadata(value, "schema");
}

function storedBlob(value: unknown, account: string, id: string): Blob {
  if (!record(value) || value.account !== account || value.id !== id || !(value.blob instanceof Blob)) {
    throw new RepositoryError("schema", "Corrupt account original record.");
  }
  return value.blob;
}

function consistent(asset: AssetMetadata, blob: Blob): void {
  if (asset.size !== blob.size || asset.type !== blob.type) {
    throw new RepositoryError("invalid", "Asset metadata does not match its original Blob.");
  }
}

function storageError(error: unknown): RepositoryError {
  if (error instanceof RepositoryError) return error;
  const name = error instanceof Error || error instanceof DOMException ? error.name : "";
  const code = name === "QuotaExceededError" ? "quota" : name === "VersionError" ? "schema"
    : name === "DataCloneError" ? "invalid" : "storage";
  return new RepositoryError(code, code === "quota" ? "Local storage quota exceeded." : "Local asset storage operation failed.");
}

/** Native request callbacks keep the transaction active; no hashing or unrelated awaits. */
function transact<T>(db: IDBDatabase, stores: string[], mode: IDBTransactionMode,
  run: (tx: IDBTransaction, result: (value: T) => void, guard: Guard) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    let tx: IDBTransaction;
    try { tx = db.transaction(stores, mode); } catch (error) { reject(storageError(error)); return; }
    let result: { value: T } | undefined;
    let failure: RepositoryError | undefined;
    const guard: Guard = operation => {
      if (failure) return;
      try { operation(); } catch (error) {
        failure = storageError(error);
        tx.abort();
      }
    };
    tx.onabort = () => reject(failure ?? storageError(tx.error));
    tx.oncomplete = () => {
      if (result) resolve(result.value);
      else reject(new RepositoryError("storage", "Storage transaction returned no result."));
    };
    guard(() => run(tx, value => { result = { value }; }, guard));
  });
}

function get(store: IDBObjectStore, key: IDBValidKey, guard: Guard, use: (value: unknown) => void): void {
  const request: IDBRequest<unknown> = store.get(key);
  request.onsuccess = () => guard(() => use(request.result));
}

function validateDatabase(db: IDBDatabase): void {
  if (db.version !== VERSION || db.objectStoreNames.length !== STORES.length || STORES.some(name => !db.objectStoreNames.contains(name))) {
    throw new RepositoryError("schema", "Unsupported asset database schema.");
  }
  const tx = db.transaction(STORES, "readonly");
  for (const name of STORES) {
    const store = tx.objectStore(name);
    const expected = name === "workspaces" ? "account" : name === "audits" ? "id" : ["account", "id"];
    if (JSON.stringify(store.keyPath) !== JSON.stringify(expected) || store.autoIncrement !== (name === "audits")) {
      throw new RepositoryError("schema", "Corrupt asset database stores.");
    }
  }
}

/**
 * close() is final (also on versionchange); in-flight transactions may finish normally.
 * Failed opens can be retried. Failed saves never advance a local revision or poison a queue.
 * Originals are insert-only: callers filter already committed blobs after queued saves finish.
 */
export class AssetRepository {
  private database: IDBDatabase | undefined;
  private opening: Promise<IDBDatabase> | undefined;
  private rejectOpening: ((error: RepositoryError) => void) | undefined;
  private closed = false;

  constructor(readonly account: string, private readonly databaseName = "corral.ideas.v2") {
    if (!safeText(account, 256) || account.trim() !== account || !safeText(databaseName, 256)) invalid("Invalid account or database name.");
  }

  private open(): Promise<IDBDatabase> {
    if (this.closed) return Promise.reject(new RepositoryError("closed", "Asset repository is closed."));
    if (this.database) return Promise.resolve(this.database);
    if (this.opening) return this.opening;
    const opening = new Promise<IDBDatabase>((resolve, reject) => {
      let settled = false;
      const fail = (error: RepositoryError) => {
        if (settled) return;
        settled = true;
        if (this.rejectOpening === fail) this.rejectOpening = undefined;
        reject(error);
      };
      this.rejectOpening = fail;
      let request: IDBOpenDBRequest;
      try {
        if (typeof indexedDB === "undefined") throw new RepositoryError("storage", "IndexedDB is unavailable.");
        request = indexedDB.open(this.databaseName, VERSION);
      } catch (error) { fail(storageError(error)); return; }
      request.onblocked = () => fail(new RepositoryError("blocked", "Asset database upgrade is blocked by another connection."));
      request.onerror = () => fail(storageError(request.error));
      request.onupgradeneeded = event => {
        if (settled || this.closed || event.oldVersion !== 0) {
          fail(new RepositoryError("schema", "Unsupported asset database version."));
          request.transaction?.abort();
          return;
        }
        try {
          const db = request.result;
          db.createObjectStore("workspaces", { keyPath: "account" });
          db.createObjectStore("assets", { keyPath: ["account", "id"] });
          db.createObjectStore("blobs", { keyPath: ["account", "id"] });
          db.createObjectStore("audits", { keyPath: "id", autoIncrement: true });
        } catch (error) { fail(storageError(error)); request.transaction?.abort(); }
      };
      request.onsuccess = () => {
        const db = request.result;
        if (settled || this.closed) { db.close(); return; }
        try { validateDatabase(db); } catch (error) { db.close(); fail(storageError(error)); return; }
        db.onversionchange = () => this.close();
        db.onclose = () => this.close();
        settled = true;
        this.rejectOpening = undefined;
        this.database = db;
        resolve(db);
      };
    });
    this.opening = opening;
    const clear = () => { if (this.opening === opening) this.opening = undefined; };
    void opening.then(clear, clear);
    return opening;
  }

  async load(): Promise<SavedWorkspace | null> {
    const db = await this.open();
    return transact(db, ["workspaces"], "readonly", (tx, result, guard) => {
      get(tx.objectStore("workspaces"), this.account, guard, value => result(workspace(value, this.account)));
    });
  }

  async list(): Promise<AssetMetadata[]> {
    const db = await this.open();
    return transact(db, ["assets"], "readonly", (tx, result, guard) => {
      const request: IDBRequest<unknown[]> = tx.objectStore("assets").getAll(IDBKeyRange.bound([this.account, ""], [this.account, "\uffff"]));
      request.onsuccess = () => guard(() => result(request.result.map(value => {
        if (!record(value) || !validId(value.id)) throw new RepositoryError("schema", "Corrupt asset identity.");
        return storedAsset(value, this.account, value.id);
      })));
    });
  }

  async readBlob(blobId: string): Promise<Blob | null> {
    if (!validId(blobId)) invalid("Invalid original identity.");
    const db = await this.open();
    return transact(db, ["blobs"], "readonly", (tx, result, guard) => {
      get(tx.objectStore("blobs"), [this.account, blobId], guard,
        value => result(value === undefined ? null : storedBlob(value, this.account, blobId)));
    });
  }

  /**
   * CAS snapshot, originals, metadata and caller-attributed audit commit or roll back together.
   * Metadata can repeat unchanged or alias existing bytes; blobs must contain only new IDs.
   */
  async save(write: WorkspaceWrite): Promise<number> {
    // Capture input before opening/awaiting; later caller mutations cannot change a queued save.
    if (!record(write) || !integer(write.expectedRevision) || write.expectedRevision === Number.MAX_SAFE_INTEGER
      || !Array.isArray(write.assets) || !Array.isArray(write.blobs) || !Array.isArray(write.referencedIds)
      || !Array.isArray(write.audit) || write.audit.length === 0) invalid("Invalid workspace write or missing audit.");
    const expectedRevision = write.expectedRevision;
    const assets = new Map<string, AssetMetadata>();
    for (const value of write.assets) {
      const asset = metadata(value, "invalid");
      if (assets.has(asset.id)) invalid("Duplicate asset identity in write.");
      assets.set(asset.id, asset);
    }
    const blobs = new Map<string, Blob>();
    for (const value of write.blobs) {
      if (!record(value) || !validId(value.id) || !(value.blob instanceof Blob)) invalid("Invalid original Blob.");
      if (blobs.has(value.id)) invalid("Duplicate original identity in write.");
      blobs.set(value.id, value.blob);
    }
    const ids = new Set(assets.keys());
    for (const id of write.referencedIds) {
      if (!validId(id)) invalid("Invalid referenced asset identity.");
      ids.add(id);
    }
    const audit = write.audit.map((event: unknown): AuditEvent => {
      if (!record(event) || (event.actor !== "owner" && event.actor !== "system")
        || !safeText(event.action, 128) || !safeText(event.targetId, 256) || !integer(event.at)) invalid("Invalid audit event.");
      return { actor: event.actor, action: event.action, targetId: event.targetId, at: event.at };
    });
    let data: unknown;
    try { data = structuredClone(write.data); } catch (error) { throw storageError(error); }
    const db = await this.open();
    return transact(db, STORES, "readwrite", (tx, result, guard) => {
      const snapshots = tx.objectStore("workspaces");
      const originals = tx.objectStore("blobs");
      const occurrences = tx.objectStore("assets");
      get(snapshots, this.account, guard, value => {
        const saved = workspace(value, this.account);
        if ((saved?.revision ?? 0) !== expectedRevision) throw new RepositoryError("conflict", "Workspace revision changed; reload before saving.");
        const revision = expectedRevision + 1;
        for (const [id, blob] of blobs) {
          get(originals, [this.account, id], guard, existing => {
            // Even equal-size or identical bytes are not proof of a new immutable identity.
            if (existing !== undefined) throw new RepositoryError("immutable", "Original identity already exists.");
            originals.add({ account: this.account, id, blob });
          });
        }
        for (const id of ids) {
          get(occurrences, [this.account, id], guard, existing => {
            const incoming = assets.get(id);
            const savedAsset = existing === undefined ? undefined : storedAsset(existing, this.account, id);
            if (incoming && savedAsset && !sameMetadata(incoming, savedAsset)) {
              throw new RepositoryError("immutable", "Asset metadata cannot change after creation.");
            }
            const asset = incoming ?? savedAsset;
            if (!asset) throw new RepositoryError("missing-asset", "Referenced asset is missing from this account.");
            const stagedBlob = blobs.get(asset.blobId);
            if (stagedBlob) consistent(asset, stagedBlob);
            else get(originals, [this.account, asset.blobId], guard, original => {
              if (original === undefined) throw new RepositoryError("missing-blob", "Asset original is missing from this account.");
              consistent(asset, storedBlob(original, this.account, asset.blobId));
            });
            if (!savedAsset) occurrences.add({ account: this.account, ...asset });
          });
        }
        snapshots.put({ account: this.account, schema: 2, revision, data });
        for (const event of audit) tx.objectStore("audits").add({ account: this.account, revision, ...event });
        result(revision);
      });
    });
  }

  close(): void {
    this.closed = true;
    this.rejectOpening?.(new RepositoryError("closed", "Asset repository is closed."));
    this.database?.close();
    this.database = undefined;
  }
}
