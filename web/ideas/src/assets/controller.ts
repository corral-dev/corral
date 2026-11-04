// Account-bound asset facts and serialized workspace commits. Views own object URLs;
// originals are fetched lazily rather than hydrating all historical bytes into memory.
import { AssetRepository, type AssetMetadata, type AuditEvent, type NewBlob, type SavedWorkspace } from "./repository";
import { referencedAssets } from "../input/content";

export const MAX_FILE_BYTES = 50 * 1024 ** 2;
export const MAX_BATCH_BYTES = 200 * 1024 ** 2;
export const MAX_BATCH_FILES = 20;
const PREVIEW_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

export type AssetListener = () => void;

export function canPreview(asset: AssetMetadata): boolean {
  return PREVIEW_TYPES.has(asset.type);
}

function identity(): string {
  return `asset_${crypto.randomUUID().replaceAll("-", "")}`;
}

export class AssetController {
  private metadata = new Map<string, AssetMetadata>();
  private stagedAssets = new Map<string, AssetMetadata>();
  private stagedBlobs = new Map<string, NewBlob>();
  private listeners = new Set<AssetListener>();
  private acknowledgedAudit = new WeakSet<AuditEvent>();
  private tail: Promise<void> = Promise.resolve();
  private revision = 0;
  private closed = false;
  private change = 0;
  error: Error | null = null;

  constructor(readonly repository: AssetRepository) {}

  async hydrate(): Promise<SavedWorkspace | null> {
    const workspace = await this.repository.load();
    const assets = await this.repository.list();
    this.checkOpen();
    this.revision = workspace?.revision ?? 0;
    this.metadata = new Map(assets.map((asset) => [asset.id, asset]));
    this.error = null;
    return workspace;
  }

  get account(): string { return this.repository.account; }
  get active(): boolean { return !this.closed; }
  get version(): number { return this.change; }
  get hasStaged(): boolean { return this.stagedAssets.size > 0 || this.stagedBlobs.size > 0; }

  subscribe = (listener: AssetListener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  get(id: string): AssetMetadata | undefined { return this.metadata.get(id); }

  list(): AssetMetadata[] { return [...this.metadata.values()]; }

  /** Synchronous preflight runs before an editor selection is changed. */
  validate(files: readonly File[]): void {
    this.checkOpen();
    if (!files.length) throw new Error("No readable files were supplied.");
    if (files.length > MAX_BATCH_FILES) throw new Error(`Choose at most ${MAX_BATCH_FILES} files at once.`);
    if (files.reduce((sum, file) => sum + file.size, 0) > MAX_BATCH_BYTES) throw new Error("These files exceed the 200 MB import limit.");
    for (const file of files) {
      if (file.size > MAX_FILE_BYTES) throw new Error(`${file.name || "This file"} exceeds the 50 MB file limit.`);
    }
  }

  stage(files: readonly File[]): AssetMetadata[] {
    this.validate(files);
    const result = files.map((file) => {
      const blobId = identity();
      const type = file.type.toLowerCase();
      const suffix = type === "image/png" ? ".png" : type === "image/jpeg" ? ".jpg" : "";
      const asset: AssetMetadata = {
        id: identity(), blobId, name: file.name || `image${suffix}`, type,
        size: file.size, createdAt: Date.now(),
      };
      this.stagedBlobs.set(blobId, { id: blobId, blob: file });
      this.stagedAssets.set(asset.id, asset);
      this.metadata.set(asset.id, asset);
      return asset;
    });
    this.notify();
    return result;
  }

  /** Copy creates a new occurrence but does not duplicate or mutate original bytes. */
  clone(id: string): AssetMetadata {
    this.checkOpen();
    const source = this.metadata.get(id);
    if (!source) throw new Error("This attachment is not available in this account. Choose its original file.");
    const asset = { ...source, id: identity(), createdAt: Date.now() };
    this.metadata.set(asset.id, asset);
    this.stagedAssets.set(asset.id, asset);
    this.notify();
    return asset;
  }

  async blob(id: string): Promise<Blob | null> {
    this.checkOpen();
    const asset = this.metadata.get(id);
    if (!asset) return null;
    const pending = this.stagedBlobs.get(asset.blobId);
    if (pending) return pending.blob;
    const blob = await this.repository.readBlob(asset.blobId);
    this.checkOpen();
    return blob;
  }

  ready(source: string): boolean {
    return !this.closed && !this.error && referencedAssets(source).every((id) => {
      const asset = this.metadata.get(id);
      return asset !== undefined && !this.stagedAssets.has(id) && !this.stagedBlobs.has(asset.blobId);
    });
  }

  /** Each call owns its exact immutable snapshot and staged dependency set. */
  commit(data: unknown, ids: readonly string[], audit: readonly AuditEvent[]): Promise<void> {
    this.checkOpen();
    const snapshot = structuredClone(data);
    const assetIds = [...this.stagedAssets.keys()];
    const operation = this.tail.then(async () => {
      this.checkOpen();
      const assets = assetIds.flatMap((id) => {
        const asset = this.stagedAssets.get(id);
        return asset ? [asset] : [];
      });
      const blobIds = [...new Set(assets.map((asset) => asset.blobId))];
      const blobs = blobIds.flatMap((id) => {
        const blob = this.stagedBlobs.get(id);
        return blob ? [blob] : [];
      });
      const freshAudit = audit.filter((event) => !this.acknowledgedAudit.has(event));
      const revision = await this.repository.save({
        expectedRevision: this.revision, data: snapshot, referencedIds: [...ids], assets, blobs,
        audit: [...(freshAudit.length ? freshAudit : [{ actor: "system", action: "workspace.checkpoint", targetId: "workspace", at: Date.now() } satisfies AuditEvent]), ...assets.map((asset): AuditEvent => ({ actor: "owner", action: "asset.create", targetId: asset.id, at: asset.createdAt }))],
      });
      this.checkOpen();
      this.revision = revision;
      for (const event of freshAudit) this.acknowledgedAudit.add(event);
      for (const asset of assets) this.stagedAssets.delete(asset.id);
      for (const blob of blobs) this.stagedBlobs.delete(blob.id);
      this.error = null;
      this.notify();
    });
    // A failed write rejects its waiter but never poisons the account's write queue.
    this.tail = operation.catch((cause: unknown) => {
      if (this.closed) return;
      this.error = cause instanceof Error ? cause : new Error("The draft could not be saved.");
      this.notify();
    });
    return operation;
  }

  close(): void {
    this.closed = true;
    this.repository.close();
    this.listeners.clear();
    this.stagedAssets.clear();
    this.stagedBlobs.clear();
  }

  private checkOpen(): void {
    if (this.closed) throw new Error("This account is no longer active.");
  }

  private notify(): void {
    this.change += 1;
    for (const listener of this.listeners) listener();
  }
}
