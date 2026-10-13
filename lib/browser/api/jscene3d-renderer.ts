import app from '@electron/internal/browser/api/app';

export type JScene3DRendererLaunchRequest = {
  javaExecutable: string;
  workingDirectory: string;
  nativeLibraryDirectory: string;
  classPath: string[];
  mainClass: string;
  rendererArguments: string[];
  width: number;
  height: number;
};

export type JScene3DRendererObservers = {
  onReady?: () => void;
  onFrameReady?: () => void;
  onExit?: () => void;
  onSurfaceReady?: (surfaceGeneration: number, width: number, height: number) => void;
  onSceneSelection?: (requestId: number, revision: number, selection: string) => void;
  onSceneTransform?: (transform: string) => void;
};

export type JScene3DRendererSession = {
  sessionId: number;
  pid: number;
  surfaceGeneration: number;
};

export type JScene3DSurface = {
  handle: Buffer;
  width: number;
  height: number;
  surfaceGeneration: number;
};

export type JScene3DRendererSessionState = {
  sessionId: number;
  pid: number;
  state: 'starting' | 'running' | 'paused' | 'failed' | 'stopping' | 'stopped';
  surfaceGeneration: number;
};

const binding = process._linkedBinding('electron_browser_jscene3d_renderer');
const stopping = new Map<number, Promise<void>>();
const noop = () => {};

function launchRenderer(
  request: JScene3DRendererLaunchRequest,
  observers: JScene3DRendererObservers = {}
): JScene3DRendererSession {
  const result = binding.launchRenderer(request, {
    onReady: observers.onReady ?? noop,
    onFrameReady: observers.onFrameReady ?? noop,
    onExit: observers.onExit ?? noop,
    onSurfaceReady: observers.onSurfaceReady ?? noop,
    onSceneSelection: observers.onSceneSelection ?? noop,
    onSceneTransform: observers.onSceneTransform ?? noop
  }) as JScene3DRendererSession | null;
  if (result === null) {
    throw new TypeError('Invalid JScene3D renderer launch request or renderer startup failed');
  }
  return result;
}

function getSurface(sessionId: number): JScene3DSurface | null {
  return binding.getSurface(sessionId);
}

function getSessionState(sessionId: number): JScene3DRendererSessionState | null {
  return binding.getSessionState(sessionId);
}

function sendRendererMessage(sessionId: number, message: string): boolean {
  return binding.sendRendererMessage(sessionId, message);
}

function replaceSurface(sessionId: number, expectedSurfaceGeneration: number, width: number, height: number): boolean {
  return binding.replaceSurface(sessionId, expectedSurfaceGeneration, width, height);
}

function pauseRenderer(sessionId: number): boolean {
  return binding.pauseRenderer(sessionId);
}

function resumeRenderer(sessionId: number): boolean {
  return binding.resumeRenderer(sessionId);
}

function stopRenderer(sessionId: number): Promise<void> {
  const existing = stopping.get(sessionId);
  if (existing) {
    return existing;
  }
  const promise = new Promise<void>((resolve) => {
    if (!binding.stopRenderer(sessionId, resolve)) {
      resolve();
    }
  });
  stopping.set(sessionId, promise);
  promise.finally(() => stopping.delete(sessionId)).catch(noop);
  return promise;
}

async function stopAllRenderers(): Promise<void> {
  const sessionIds = binding.getRendererSessionIds() as number[];
  await Promise.all(sessionIds.map(stopRenderer));
}

// Starting stop work here is sufficient for orderly application shutdown:
// native reaping runs as BLOCK_SHUTDOWN work even though Electron does not wait
// for JavaScript promises emitted from will-quit.
app.on('will-quit', () => {
  stopAllRenderers().catch(noop);
});

export default {
  launchRenderer,
  getSurface,
  getSessionState,
  sendRendererMessage,
  replaceSurface,
  pauseRenderer,
  resumeRenderer,
  stopRenderer,
  stopAllRenderers
};
