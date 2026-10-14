import {
  SharedTextureReferenceTracker,
  trackSharedTextureAcknowledgement
} from '@electron/internal/browser/api/shared-texture-reference-tracker';

import { expect } from 'chai';

describe('shared texture reference tracking', () => {
  interface TestTexture {
    readonly textureId: string;
  }

  class TestFrame {
    constructor(
      readonly frameTreeNodeId: number,
      private destroyed = false
    ) {}

    isDestroyed() {
      return this.destroyed;
    }

    destroy() {
      this.destroyed = true;
    }
  }

  const fixture = () => {
    const releases: string[] = [];
    const callbacks: string[] = [];
    const tracker = new SharedTextureReferenceTracker<TestTexture, TestFrame>((texture, released) => {
      releases.push(texture.textureId);
      released();
    });
    const texture = { textureId: 'texture-a' };
    tracker.track(texture, (released) => callbacks.push(released.textureId));
    return { tracker, texture, releases, callbacks };
  };

  const deferred = <T>() => {
    let resolveDeferred!: (value: T) => void;
    let rejectDeferred!: (error: Error) => void;
    const promise = new Promise<T>((resolve, reject) => {
      resolveDeferred = resolve;
      rejectDeferred = reject;
    });
    return { promise, resolve: resolveDeferred, reject: rejectDeferred };
  };

  const expectRejection = async (promise: Promise<unknown>, message: string) => {
    try {
      await promise;
      expect.fail('Expected promise to reject');
    } catch (error) {
      expect(error).to.be.instanceOf(Error);
      expect((error as Error).message).to.equal(message);
    }
  };

  it('keeps a main-released texture alive until a late acknowledgement is released by the renderer', () => {
    const { tracker, texture, releases, callbacks } = fixture();
    const frame = new TestFrame(7);
    const pending = tracker.beginRendererTransfer(texture.textureId, frame);

    tracker.releaseFromMain(texture.textureId);
    expect(releases).to.deep.equal([]);
    expect(tracker.completeRendererTransfer(pending, () => undefined)).to.equal(true);
    expect(releases).to.deep.equal([]);

    tracker.releaseFromRenderer(texture.textureId, frame.frameTreeNodeId);
    expect(releases).to.deep.equal(['texture-a']);
    expect(callbacks).to.deep.equal(['texture-a']);
    expect(tracker.size).to.equal(0);
  });

  it('keeps normal main and renderer ownership independent', () => {
    const { tracker, texture, releases } = fixture();
    const frame = new TestFrame(7);
    const pending = tracker.beginRendererTransfer(texture.textureId, frame);

    expect(tracker.completeRendererTransfer(pending, () => undefined)).to.equal(true);
    tracker.releaseFromRenderer(texture.textureId, frame.frameTreeNodeId);
    expect(releases).to.deep.equal([]);

    tracker.releaseFromMain(texture.textureId);
    expect(releases).to.deep.equal(['texture-a']);
    expect(tracker.size).to.equal(0);
  });

  it('releases exactly once when a late acknowledgement fails', () => {
    const { tracker, texture, releases, callbacks } = fixture();
    const pending = tracker.beginRendererTransfer(texture.textureId, new TestFrame(7));

    tracker.releaseFromMain(texture.textureId);
    tracker.failRendererTransfer(pending);
    tracker.failRendererTransfer(pending);

    expect(releases).to.deep.equal(['texture-a']);
    expect(callbacks).to.deep.equal(['texture-a']);
    expect(tracker.size).to.equal(0);
  });

  it('reaps an acknowledgement that never settles after its receiver exits', () => {
    const { tracker, texture, releases } = fixture();
    const frame = new TestFrame(7);
    const pending = tracker.beginRendererTransfer(texture.textureId, frame);
    tracker.releaseFromMain(texture.textureId);

    frame.destroy();
    tracker.reapDestroyedFrames();
    tracker.failRendererTransfer(pending);

    expect(releases).to.deep.equal(['texture-a']);
    expect(tracker.size).to.equal(0);
  });

  it('reports and reaps a renderer reference whose receiver exits without releasing it', () => {
    const { tracker, texture, releases } = fixture();
    const frame = new TestFrame(7);
    const pending = tracker.beginRendererTransfer(texture.textureId, frame);
    const dangling: string[] = [];
    tracker.completeRendererTransfer(pending, () => undefined);
    tracker.releaseFromMain(texture.textureId);

    frame.destroy();
    tracker.reapDestroyedFrames((textureId, ownership) => dangling.push(`${textureId}:${ownership}`));

    expect(dangling).to.deep.equal(['texture-a:renderer reference']);
    expect(releases).to.deep.equal(['texture-a']);
    expect(tracker.size).to.equal(0);
  });

  it('does not double-release after multiple transfers settle in different orders', () => {
    const { tracker, texture, releases } = fixture();
    const firstFrame = new TestFrame(7);
    const secondFrame = new TestFrame(8);
    const first = tracker.beginRendererTransfer(texture.textureId, firstFrame);
    const second = tracker.beginRendererTransfer(texture.textureId, secondFrame);

    tracker.releaseFromMain(texture.textureId);
    expect(tracker.completeRendererTransfer(second, () => undefined)).to.equal(true);
    tracker.failRendererTransfer(first);
    tracker.releaseFromRenderer(texture.textureId, secondFrame.frameTreeNodeId);

    expect(releases).to.deep.equal(['texture-a']);
    expect(tracker.size).to.equal(0);
  });

  it('retains late acknowledgement handling after the public wait has timed out', async () => {
    const { tracker, texture, releases } = fixture();
    const frame = new TestFrame(7);
    const pending = tracker.beginRendererTransfer(texture.textureId, frame);
    const acknowledgement = deferred<string>();
    const timeout = deferred<never>();
    const tokens: string[] = [];
    const tracked = trackSharedTextureAcknowledgement(tracker, pending, acknowledgement.promise, (token) =>
      tokens.push(token)
    );

    const publicWait = Promise.race([tracked, timeout.promise]);
    timeout.reject(new Error('timed out'));
    await expectRejection(publicWait, 'timed out');
    tracker.releaseFromMain(texture.textureId);

    acknowledgement.resolve('sync-token');
    await tracked;
    tracker.releaseFromRenderer(texture.textureId, frame.frameTreeNodeId);

    expect(tokens).to.deep.equal(['sync-token']);
    expect(releases).to.deep.equal(['texture-a']);
  });

  it('cleans up a late rejected acknowledgement without an unhandled rejection after timeout', async () => {
    const { tracker, texture, releases } = fixture();
    const pending = tracker.beginRendererTransfer(texture.textureId, new TestFrame(7));
    const acknowledgement = deferred<string>();
    const timeout = deferred<never>();
    const tracked = trackSharedTextureAcknowledgement(tracker, pending, acknowledgement.promise, () => undefined);

    const publicWait = Promise.race([tracked, timeout.promise]);
    timeout.reject(new Error('timed out'));
    await expectRejection(publicWait, 'timed out');
    tracker.releaseFromMain(texture.textureId);

    acknowledgement.reject(new Error('renderer exited'));
    await new Promise<void>((resolve) => setImmediate(resolve));

    expect(releases).to.deep.equal(['texture-a']);
    expect(tracker.size).to.equal(0);
  });
});
