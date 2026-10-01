import { expect } from 'chai';

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

import { ifdescribe } from './lib/spec-helpers';

const fixture = path.resolve(__dirname, 'fixtures', 'api', 'jscene3d-renderer', 'mock-java-renderer.js');
const fixtureDirectory = path.dirname(fixture);

type RendererApi = {
  launchRenderer(
    request: Record<string, unknown>,
    observers?: Record<string, (...args: any[]) => void>
  ): {
    sessionId: number;
    pid: number;
    surfaceGeneration: number;
  };
  getSurface(sessionId: number): {
    handle: Buffer;
    width: number;
    height: number;
    surfaceGeneration: number;
  } | null;
  getSessionState(sessionId: number): { state: string; surfaceGeneration: number } | null;
  sendRendererMessage(sessionId: number, message: string): boolean;
  replaceSurface(sessionId: number, generation: number, width: number, height: number): boolean;
  pauseRenderer(sessionId: number): boolean;
  resumeRenderer(sessionId: number): boolean;
  stopRenderer(sessionId: number): Promise<void>;
  stopAllRenderers(): Promise<void>;
};

const { jscene3dRenderer } = require('electron') as { jscene3dRenderer: RendererApi };
const { sharedTexture } = require('electron') as {
  sharedTexture: {
    importSharedTexture(options: Record<string, unknown>): {
      textureId: string;
      getVideoFrame(): VideoFrame;
      release(): void;
    };
  };
};
const ownedRendererPids: number[] = [];

function launchRenderer(request: Record<string, unknown>, observers?: Record<string, (...args: any[]) => void>) {
  const session = jscene3dRenderer.launchRenderer(request, observers);
  ownedRendererPids.push(session.pid);
  return session;
}

function launchRequest(rendererArguments: string[] = []) {
  return {
    javaExecutable: fixture,
    workingDirectory: fixtureDirectory,
    nativeLibraryDirectory: fixtureDirectory,
    classPath: [fixture],
    mainClass: 'io.github.glynch.jscene3d.test.RendererFixture',
    rendererArguments,
    width: 320,
    height: 240
  };
}

function deferred<T = void>() {
  let resolveCallback!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolve) => {
    resolveCallback = resolve;
  });
  return { promise, resolve: resolveCallback };
}

async function waitFor(predicate: () => boolean) {
  for (let attempt = 0; attempt < 500; attempt++) {
    if (predicate()) return;
    await delay(20);
  }
  throw new Error('Timed out waiting for JScene3D renderer state');
}

function isProcessAlive(pid: number) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error: any) {
    if (error?.code === 'ESRCH') return false;
    throw error;
  }
}

async function withTimeout<T>(promise: Promise<T>, operation: string): Promise<T> {
  let timeout: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_resolve, reject) => {
        timeout = setTimeout(() => reject(new Error(`Timed out waiting for ${operation}`)), 10_000);
      })
    ]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

async function cleanupOwnedRenderers() {
  await withTimeout(jscene3dRenderer.stopAllRenderers(), 'renderer cleanup');
  await waitFor(() => ownedRendererPids.every((pid) => !isProcessAlive(pid)));
  ownedRendererPids.length = 0;
}

ifdescribe(process.platform === 'darwin')('jscene3dRenderer module', () => {
  afterEach(cleanupOwnedRenderers);

  it('rejects malformed launch requests and invalid dimensions', () => {
    expect(() => launchRenderer({})).to.throw(/Invalid JScene3D renderer launch request/);
    expect(() => launchRenderer({ ...launchRequest(), width: 0 })).to.throw(/Invalid JScene3D renderer launch request/);
    expect(() => launchRenderer({ ...launchRequest(), height: 16385 })).to.throw(
      /Invalid JScene3D renderer launch request/
    );
  });

  it('cleans up startup failures without registering a session', () => {
    expect(() => launchRenderer({ ...launchRequest(), javaExecutable: '/definitely/missing/java' })).to.throw(
      /renderer startup failed/
    );
  });

  it('creates an isolated session and exposes an IOSurface handle', async () => {
    const ready = deferred();
    const session = launchRenderer(launchRequest(), { onReady: ready.resolve });
    await withTimeout(ready.promise, 'renderer readiness');

    const surface = jscene3dRenderer.getSurface(session.sessionId);
    expect(session.pid).to.be.greaterThan(0);
    expect(session.pid).not.to.equal(process.pid);
    expect(surface).to.include({ width: 320, height: 240, surfaceGeneration: 1 });
    expect(surface?.handle).to.be.instanceOf(Buffer);
    expect(jscene3dRenderer.getSessionState(session.sessionId)?.state).to.equal('running');
  });

  it('replaces a surface and rejects a stale generation', async () => {
    const ready = deferred();
    const replacement = deferred<[number, number, number]>();
    const session = launchRenderer(launchRequest(['--replacement=640x360']), {
      onReady: ready.resolve,
      onSurfaceReady: (generation, width, height) => replacement.resolve([generation, width, height])
    });
    await withTimeout(ready.promise, 'renderer readiness');

    expect(jscene3dRenderer.replaceSurface(session.sessionId, 1, 640, 360)).to.be.true();
    expect(await withTimeout(replacement.promise, 'replacement surface')).to.deep.equal([2, 640, 360]);
    expect(jscene3dRenderer.getSurface(session.sessionId)).to.include({
      width: 640,
      height: 360,
      surfaceGeneration: 2
    });
    expect(jscene3dRenderer.replaceSurface(session.sessionId, 1, 800, 600)).to.be.false();
    expect(jscene3dRenderer.replaceSurface(session.sessionId, 2, 0, 600)).to.be.false();
    expect(jscene3dRenderer.replaceSurface(session.sessionId, 2, 800, 16385)).to.be.false();
  });

  it('pauses, resumes, and stops idempotently', async () => {
    const ready = deferred();
    const session = launchRenderer(launchRequest(), { onReady: ready.resolve });
    await withTimeout(ready.promise, 'renderer readiness');

    expect(jscene3dRenderer.pauseRenderer(session.sessionId)).to.be.true();
    expect(jscene3dRenderer.getSessionState(session.sessionId)?.state).to.equal('paused');
    expect(jscene3dRenderer.sendRendererMessage(session.sessionId, 'FRAME 1')).to.be.false();
    expect(jscene3dRenderer.pauseRenderer(session.sessionId)).to.be.false();
    expect(jscene3dRenderer.resumeRenderer(session.sessionId)).to.be.true();
    const firstStop = jscene3dRenderer.stopRenderer(session.sessionId);
    const secondStop = jscene3dRenderer.stopRenderer(session.sessionId);
    expect(secondStop).to.equal(firstStop);
    await withTimeout(firstStop, 'renderer stop');
    expect(jscene3dRenderer.getSessionState(session.sessionId)).to.be.null();
  });

  it('completes the product shutdown handshake before closing pipes', async () => {
    const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'jscene3d-renderer-shutdown-'));
    const shutdownMarker = path.join(temporaryDirectory, 'command.txt');
    try {
      const ready = deferred();
      const session = launchRenderer(launchRequest([`--shutdown-marker=${shutdownMarker}`]), { onReady: ready.resolve });
      await withTimeout(ready.promise, 'renderer readiness');

      await withTimeout(jscene3dRenderer.stopRenderer(session.sessionId), 'renderer stop');
      expect(fs.readFileSync(shutdownMarker, 'utf8')).to.equal('SHUTDOWN');
    } finally {
      fs.rmSync(temporaryDirectory, { recursive: true, force: true });
    }
  });

  it('keeps concurrent sessions and surfaces independent', async () => {
    const readyA = deferred();
    const readyB = deferred();
    const resizedA = deferred<[number, number, number]>();
    const frameB = deferred();
    const sessionA = launchRenderer(launchRequest(['--replacement=800x450']), {
      onReady: readyA.resolve,
      onSurfaceReady: (generation, width, height) => resizedA.resolve([generation, width, height])
    });
    const sessionB = launchRenderer(launchRequest(), {
      onReady: readyB.resolve,
      onFrameReady: frameB.resolve
    });
    await withTimeout(Promise.all([readyA.promise, readyB.promise]), 'concurrent renderer readiness');

    expect(sessionA.sessionId).not.to.equal(sessionB.sessionId);
    expect(sessionA.pid).not.to.equal(sessionB.pid);
    expect(jscene3dRenderer.getSurface(sessionA.sessionId)?.handle).not.to.deep.equal(
      jscene3dRenderer.getSurface(sessionB.sessionId)?.handle
    );
    expect(jscene3dRenderer.pauseRenderer(sessionA.sessionId)).to.be.true();
    expect(jscene3dRenderer.getSessionState(sessionA.sessionId)?.state).to.equal('paused');
    expect(jscene3dRenderer.getSessionState(sessionB.sessionId)?.state).to.equal('running');
    expect(jscene3dRenderer.resumeRenderer(sessionA.sessionId)).to.be.true();
    expect(jscene3dRenderer.replaceSurface(sessionA.sessionId, 1, 800, 450)).to.be.true();
    await withTimeout(resizedA.promise, 'session A replacement surface');
    expect(jscene3dRenderer.getSurface(sessionB.sessionId)).to.include({
      width: 320,
      height: 240,
      surfaceGeneration: 1
    });

    await withTimeout(jscene3dRenderer.stopRenderer(sessionA.sessionId), 'session A stop');
    expect(isProcessAlive(sessionA.pid)).to.be.false();
    expect(jscene3dRenderer.sendRendererMessage(sessionB.sessionId, 'FRAME 1')).to.be.true();
    await withTimeout(frameB.promise, 'session B frame');
    expect(jscene3dRenderer.getSessionState(sessionB.sessionId)?.state).to.equal('running');
  });

  it('isolates an unexpected child exit and releases its surface', async () => {
    const readyA = deferred();
    const readyB = deferred();
    const exitedA = deferred();
    const frameB = deferred();
    const sessionA = launchRenderer(launchRequest(), {
      onReady: readyA.resolve,
      onExit: exitedA.resolve
    });
    const sessionB = launchRenderer(launchRequest(), {
      onReady: readyB.resolve,
      onFrameReady: frameB.resolve
    });
    await withTimeout(Promise.all([readyA.promise, readyB.promise]), 'concurrent renderer readiness');

    process.kill(sessionA.pid, 'SIGKILL');
    await withTimeout(exitedA.promise, 'session A failure');
    await waitFor(() => jscene3dRenderer.getSurface(sessionA.sessionId) === null);
    expect(isProcessAlive(sessionA.pid)).to.be.false();
    expect(jscene3dRenderer.getSessionState(sessionA.sessionId)?.state).to.equal('failed');
    expect(jscene3dRenderer.sendRendererMessage(sessionB.sessionId, 'FRAME 2')).to.be.true();
    await withTimeout(frameB.promise, 'session B frame after session A failure');
    expect(jscene3dRenderer.getSessionState(sessionB.sessionId)?.state).to.equal('running');
  });

  it('kills only its owned unresponsive child after the shutdown timeout', async () => {
    const readyA = deferred();
    const readyB = deferred();
    const frameB = deferred();
    const sessionA = launchRenderer(launchRequest(['--ignore-shutdown']), { onReady: readyA.resolve });
    const sessionB = launchRenderer(launchRequest(), {
      onReady: readyB.resolve,
      onFrameReady: frameB.resolve
    });
    await withTimeout(Promise.all([readyA.promise, readyB.promise]), 'shutdown-timeout renderer readiness');

    await withTimeout(jscene3dRenderer.stopRenderer(sessionA.sessionId), 'unresponsive renderer stop');
    expect(isProcessAlive(sessionA.pid)).to.be.false();
    expect(jscene3dRenderer.sendRendererMessage(sessionB.sessionId, 'FRAME 1')).to.be.true();
    await withTimeout(frameB.promise, 'surviving renderer frame');
    expect(jscene3dRenderer.getSessionState(sessionB.sessionId)?.state).to.equal('running');
  });

  it('stops every active session for controller shutdown', async () => {
    const readyA = deferred();
    const readyB = deferred();
    const sessionA = launchRenderer(launchRequest(), { onReady: readyA.resolve });
    const sessionB = launchRenderer(launchRequest(), { onReady: readyB.resolve });
    await withTimeout(Promise.all([readyA.promise, readyB.promise]), 'controller renderer readiness');

    await withTimeout(jscene3dRenderer.stopAllRenderers(), 'controller stop-all');
    expect(isProcessAlive(sessionA.pid)).to.be.false();
    expect(isProcessAlive(sessionB.pid)).to.be.false();
    expect(jscene3dRenderer.getSessionState(sessionA.sessionId)).to.be.null();
    expect(jscene3dRenderer.getSessionState(sessionB.sessionId)).to.be.null();
  });
});

// The committed lifecycle suite is self-contained. These explicit paths opt
// the same spec into validation against a current external Java checkout.
const realJavaConfiguration = {
  javaExecutable: process.env.JSCENE3D_REAL_JAVA_EXECUTABLE,
  workingDirectory: process.env.JSCENE3D_REAL_JAVA_WORKING_DIRECTORY,
  nativeLibraryDirectory: process.env.JSCENE3D_REAL_JAVA_NATIVE_LIBRARY_DIRECTORY,
  classPath: process.env.JSCENE3D_REAL_JAVA_CLASS_PATH,
  mainClass: process.env.JSCENE3D_REAL_JAVA_MAIN_CLASS
};
const hasRealJavaConfiguration = Object.values(realJavaConfiguration).every(Boolean);

function realLaunchRequest(width = 320, height = 240) {
  return {
    javaExecutable: realJavaConfiguration.javaExecutable,
    workingDirectory: realJavaConfiguration.workingDirectory,
    nativeLibraryDirectory: realJavaConfiguration.nativeLibraryDirectory,
    classPath: realJavaConfiguration.classPath!.split(path.delimiter),
    mainClass: realJavaConfiguration.mainClass,
    rendererArguments: [],
    width,
    height
  };
}

ifdescribe(process.platform === 'darwin' && hasRealJavaConfiguration)('jscene3dRenderer real Java integration', () => {
  afterEach(cleanupOwnedRenderers);

  it('renders, replaces its IOSurface, and imports it through stock SharedTexture', async () => {
    const ready = deferred();
    const surfaceReady = deferred<[number, number, number]>();
    let frames = 0;
    const session = launchRenderer(realLaunchRequest(), {
      onReady: ready.resolve,
      onFrameReady: () => frames++,
      onSurfaceReady: (generation, width, height) => surfaceReady.resolve([generation, width, height])
    });
    await withTimeout(ready.promise, 'real Java renderer readiness');

    expect(jscene3dRenderer.sendRendererMessage(session.sessionId, 'FRAME 1')).to.be.true();
    await waitFor(() => frames === 1);

    const initialSurface = jscene3dRenderer.getSurface(session.sessionId)!;
    let allReferencesReleased = false;
    const imported = sharedTexture.importSharedTexture({
      textureInfo: {
        pixelFormat: 'bgra',
        codedSize: { width: initialSurface.width, height: initialSurface.height },
        visibleRect: { x: 0, y: 0, width: initialSurface.width, height: initialSurface.height },
        handle: { ioSurface: initialSurface.handle }
      },
      allReferencesReleased: () => {
        allReferencesReleased = true;
      }
    });
    expect(imported.textureId).to.be.a('string').and.not.empty();
    expect(() => imported.getVideoFrame()).to.throw(/cannot be created at current process/);
    imported.release();
    await waitFor(() => allReferencesReleased);

    expect(jscene3dRenderer.replaceSurface(session.sessionId, 1, 640, 360)).to.be.true();
    expect(await withTimeout(surfaceReady.promise, 'real Java replacement surface')).to.deep.equal([2, 640, 360]);
    expect(jscene3dRenderer.getSurface(session.sessionId)?.handle).not.to.deep.equal(initialSurface.handle);
    expect(jscene3dRenderer.sendRendererMessage(session.sessionId, 'FRAME 2')).to.be.true();
    await waitFor(() => frames === 2);

    await withTimeout(jscene3dRenderer.stopRenderer(session.sessionId), 'real Java renderer stop');
    expect(isProcessAlive(session.pid)).to.be.false();
  });

  it('keeps two real Java renderer sessions independent', async () => {
    const readyA = deferred();
    const readyB = deferred();
    const resizedA = deferred<[number, number, number]>();
    let framesB = 0;
    const sessionA = launchRenderer(realLaunchRequest(), {
      onReady: readyA.resolve,
      onSurfaceReady: (generation, width, height) => resizedA.resolve([generation, width, height])
    });
    const sessionB = launchRenderer(realLaunchRequest(), {
      onReady: readyB.resolve,
      onFrameReady: () => framesB++
    });
    await withTimeout(Promise.all([readyA.promise, readyB.promise]), 'real Java concurrent readiness');

    expect(sessionA.sessionId).not.to.equal(sessionB.sessionId);
    expect(sessionA.pid).not.to.equal(sessionB.pid);
    expect(jscene3dRenderer.getSurface(sessionA.sessionId)?.handle).not.to.deep.equal(
      jscene3dRenderer.getSurface(sessionB.sessionId)?.handle
    );
    expect(jscene3dRenderer.replaceSurface(sessionA.sessionId, 1, 800, 450)).to.be.true();
    await withTimeout(resizedA.promise, 'real Java session A replacement surface');
    expect(jscene3dRenderer.getSurface(sessionB.sessionId)).to.include({
      width: 320,
      height: 240,
      surfaceGeneration: 1
    });

    await withTimeout(jscene3dRenderer.stopRenderer(sessionA.sessionId), 'real Java session A stop');
    expect(isProcessAlive(sessionA.pid)).to.be.false();
    expect(jscene3dRenderer.sendRendererMessage(sessionB.sessionId, 'FRAME 1')).to.be.true();
    await waitFor(() => framesB === 1);
    expect(jscene3dRenderer.getSessionState(sessionB.sessionId)?.state).to.equal('running');

    await withTimeout(jscene3dRenderer.stopRenderer(sessionB.sessionId), 'real Java session B stop');
    expect(isProcessAlive(sessionB.pid)).to.be.false();
  });

  it('isolates an exact real Java child failure', async () => {
    const readyA = deferred();
    const readyB = deferred();
    const exitedA = deferred();
    let framesB = 0;
    const sessionA = launchRenderer(realLaunchRequest(), {
      onReady: readyA.resolve,
      onExit: exitedA.resolve
    });
    const sessionB = launchRenderer(realLaunchRequest(), {
      onReady: readyB.resolve,
      onFrameReady: () => framesB++
    });
    await withTimeout(Promise.all([readyA.promise, readyB.promise]), 'real Java failure-test readiness');

    process.kill(sessionA.pid, 'SIGKILL');
    await withTimeout(exitedA.promise, 'real Java session A failure');
    await waitFor(() => jscene3dRenderer.getSurface(sessionA.sessionId) === null);
    expect(isProcessAlive(sessionA.pid)).to.be.false();
    expect(jscene3dRenderer.sendRendererMessage(sessionB.sessionId, 'FRAME 1')).to.be.true();
    await waitFor(() => framesB === 1);
    expect(jscene3dRenderer.getSessionState(sessionB.sessionId)?.state).to.equal('running');

    await withTimeout(jscene3dRenderer.stopRenderer(sessionB.sessionId), 'real Java survivor stop');
    expect(isProcessAlive(sessionB.pid)).to.be.false();
  });
});
