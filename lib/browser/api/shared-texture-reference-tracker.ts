type SharedTextureFrame = {
  readonly frameTreeNodeId: number;
  isDestroyed(): boolean;
};

type SharedTextureResource = {
  readonly textureId: string;
};

type SharedTextureRelease<TTexture> = (texture: TTexture, released: () => void) => void;

type SharedTextureEntry<TTexture, TFrame> = {
  readonly texture: TTexture;
  readonly allReferencesReleased: ((texture: TTexture) => void) | undefined;
  readonly pendingTransfers: Map<number, TFrame>;
  readonly rendererFrameReferences: Map<number, { count: number; reference: TFrame }>;
  mainReference: boolean;
  released: boolean;
};

export type SharedTexturePendingTransfer<TFrame> = {
  readonly id: number;
  readonly textureId: string;
  readonly frame: TFrame;
};

/** Keeps ownership bookkeeping attached to an acknowledgement even after its caller times out. */
export function trackSharedTextureAcknowledgement<
  TTexture extends SharedTextureResource,
  TFrame extends SharedTextureFrame,
  TToken
>(
  tracker: SharedTextureReferenceTracker<TTexture, TFrame>,
  transfer: SharedTexturePendingTransfer<TFrame>,
  acknowledgement: Promise<TToken>,
  beforeReference: (token: TToken) => void
): Promise<TToken> {
  return acknowledgement.then(
    (token) => {
      tracker.completeRendererTransfer(transfer, () => beforeReference(token));
      return token;
    },
    (error) => {
      tracker.failRendererTransfer(transfer);
      throw error;
    }
  );
}

/** Tracks main, pending-transfer, and renderer ownership of imported shared textures. */
export class SharedTextureReferenceTracker<TTexture extends SharedTextureResource, TFrame extends SharedTextureFrame> {
  private readonly entries = new Map<string, SharedTextureEntry<TTexture, TFrame>>();
  private nextTransferId = 0;

  constructor(private readonly releaseTexture: SharedTextureRelease<TTexture>) {}

  get size() {
    return this.entries.size;
  }

  track(texture: TTexture, allReferencesReleased?: (texture: TTexture) => void) {
    if (this.entries.has(texture.textureId)) {
      throw new Error(`Shared texture with id ${texture.textureId} is already tracked`);
    }
    this.entries.set(texture.textureId, {
      texture,
      allReferencesReleased,
      mainReference: true,
      pendingTransfers: new Map(),
      rendererFrameReferences: new Map(),
      released: false
    });
  }

  beginRendererTransfer(textureId: string, frame: TFrame): SharedTexturePendingTransfer<TFrame> {
    const entry = this.get(textureId);
    const transfer = { id: ++this.nextTransferId, textureId, frame };
    entry.pendingTransfers.set(transfer.id, frame);
    return transfer;
  }

  completeRendererTransfer(transfer: SharedTexturePendingTransfer<TFrame>, beforeReference: () => void): boolean {
    const entry = this.entries.get(transfer.textureId);
    if (!entry || entry.pendingTransfers.get(transfer.id) !== transfer.frame) {
      return false;
    }

    try {
      beforeReference();
    } catch (error) {
      entry.pendingTransfers.delete(transfer.id);
      this.releaseIfUnreferenced(entry);
      throw error;
    }

    entry.pendingTransfers.delete(transfer.id);
    const frameTreeNodeId = transfer.frame.frameTreeNodeId;
    const existing = entry.rendererFrameReferences.get(frameTreeNodeId);
    if (existing) {
      existing.count += 1;
    } else {
      entry.rendererFrameReferences.set(frameTreeNodeId, { count: 1, reference: transfer.frame });
    }
    this.releaseIfUnreferenced(entry);
    return true;
  }

  failRendererTransfer(transfer: SharedTexturePendingTransfer<TFrame>) {
    const entry = this.entries.get(transfer.textureId);
    if (!entry || entry.pendingTransfers.get(transfer.id) !== transfer.frame) {
      return;
    }
    entry.pendingTransfers.delete(transfer.id);
    this.releaseIfUnreferenced(entry);
  }

  releaseFromRenderer(textureId: string, frameTreeNodeId: number) {
    const entry = this.get(textureId);
    const reference = entry.rendererFrameReferences.get(frameTreeNodeId);
    if (!reference) {
      throw new Error(`Shared texture ${textureId} is not referenced by renderer frame ${frameTreeNodeId}`);
    }
    reference.count -= 1;
    if (reference.count === 0) {
      entry.rendererFrameReferences.delete(frameTreeNodeId);
    }
    this.releaseIfUnreferenced(entry);
  }

  releaseFromMain(textureId: string) {
    const entry = this.get(textureId);
    entry.mainReference = false;
    this.releaseIfUnreferenced(entry);
  }

  reapDestroyedFrames(
    onDanglingReference?: (textureId: string, ownership: 'pending transfer' | 'renderer reference') => void
  ) {
    for (const entry of this.entries.values()) {
      for (const [transferId, frame] of entry.pendingTransfers) {
        if (frame.isDestroyed()) {
          onDanglingReference?.(entry.texture.textureId, 'pending transfer');
          entry.pendingTransfers.delete(transferId);
        }
      }
      for (const [frameTreeNodeId, reference] of entry.rendererFrameReferences) {
        if (reference.reference.isDestroyed()) {
          onDanglingReference?.(entry.texture.textureId, 'renderer reference');
          entry.rendererFrameReferences.delete(frameTreeNodeId);
        }
      }
      this.releaseIfUnreferenced(entry);
    }
  }

  private get(textureId: string) {
    const entry = this.entries.get(textureId);
    if (!entry) {
      throw new Error(`Shared texture with id ${textureId} not found`);
    }
    return entry;
  }

  private releaseIfUnreferenced(entry: SharedTextureEntry<TTexture, TFrame>) {
    if (
      entry.released ||
      entry.mainReference ||
      entry.pendingTransfers.size !== 0 ||
      entry.rendererFrameReferences.size !== 0
    ) {
      return;
    }
    entry.released = true;
    this.entries.delete(entry.texture.textureId);
    this.releaseTexture(entry.texture, () => entry.allReferencesReleased?.(entry.texture));
  }
}
