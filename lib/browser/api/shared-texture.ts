import ipcMain from '@electron/internal/browser/api/ipc-main';
import {
  SharedTextureReferenceTracker,
  trackSharedTextureAcknowledgement
} from '@electron/internal/browser/api/shared-texture-reference-tracker';
import * as ipcMainInternalUtils from '@electron/internal/browser/ipc-main-internal-utils';
import { IPC_MESSAGES } from '@electron/internal/common/ipc-messages';

import { randomUUID } from 'crypto';

const transferTimeout = 1000;
const sharedTextureNative = process._linkedBinding('electron_common_shared_texture');
const referenceTracker = new SharedTextureReferenceTracker<Electron.SharedTextureImported, Electron.WebFrameMain>(
  (texture, released) => texture.subtle.release(released)
);

ipcMain.handle(
  IPC_MESSAGES.IMPORT_SHARED_TEXTURE_RELEASE_RENDERER_TO_MAIN,
  (event: Electron.IpcMainInvokeEvent, textureId: string) => {
    const frameTreeNodeId = event.frameTreeNodeId ?? event.sender.mainFrame.frameTreeNodeId;
    referenceTracker.releaseFromRenderer(textureId, frameTreeNodeId);
  }
);

let checkManagedSharedTexturesInterval: NodeJS.Timeout | null = null;

function scheduleCheckManagedSharedTextures() {
  if (checkManagedSharedTexturesInterval === null) {
    checkManagedSharedTexturesInterval = setInterval(checkManagedSharedTextures, 1000);
  }
}

function unscheduleCheckManagedSharedTextures() {
  if (checkManagedSharedTexturesInterval !== null) {
    clearInterval(checkManagedSharedTexturesInterval);
    checkManagedSharedTexturesInterval = null;
  }
}

function checkManagedSharedTextures() {
  referenceTracker.reapDestroyedFrames((textureId, ownership) => {
    console.error(
      `The imported shared texture ${textureId} has a dangling ${ownership} owned by a destroyed webContents/webFrameMain. Releasing that reference now.`
    );
  });
  if (referenceTracker.size === 0) {
    unscheduleCheckManagedSharedTextures();
  }
}

async function sendSharedTexture(options: Electron.SendSharedTextureOptions, ...args: any[]) {
  const imported = options.importedSharedTexture;
  const targetFrame: Electron.WebFrameMain | undefined = options.frame;
  if (!targetFrame) {
    throw new Error('`frame` should be provided');
  }

  const transfer = imported.subtle.startTransferSharedTexture();
  const pending = referenceTracker.beginRendererTransfer(imported.textureId, targetFrame);
  scheduleCheckManagedSharedTextures();
  let invokePromise: Promise<Electron.SharedTextureSyncToken>;
  try {
    invokePromise = ipcMainInternalUtils.invokeInWebFrameMain<Electron.SharedTextureSyncToken>(
      targetFrame,
      IPC_MESSAGES.IMPORT_SHARED_TEXTURE_TRANSFER_MAIN_TO_RENDERER,
      transfer,
      imported.textureId,
      ...args
    );
  } catch (error) {
    referenceTracker.failRendererTransfer(pending);
    throw error;
  }

  const trackedInvokePromise = trackSharedTextureAcknowledgement(
    referenceTracker,
    pending,
    invokePromise,
    (syncToken) => imported.subtle.setReleaseSyncToken(syncToken)
  );
  let timeoutHandle: NodeJS.Timeout | null = null;
  const timeoutPromise = new Promise<never>((resolve, reject) => {
    timeoutHandle = setTimeout(() => {
      reject(
        new Error(
          `transfer shared texture timed out after ${transferTimeout}ms, ensure you have registered receiver at renderer process.`
        )
      );
    }, transferTimeout);
  });

  try {
    await Promise.race([trackedInvokePromise, timeoutPromise]);
  } finally {
    if (timeoutHandle) {
      clearTimeout(timeoutHandle);
    }
  }
}

function importSharedTexture(options: Electron.ImportSharedTextureOptions) {
  const id = randomUUID();
  const imported = sharedTextureNative.importSharedTexture(Object.assign(options.textureInfo, { id }));
  const ret: Electron.SharedTextureImported = {
    textureId: id,
    subtle: imported,
    getVideoFrame: imported.getVideoFrame,
    release: () => {
      referenceTracker.releaseFromMain(id);
    }
  };

  referenceTracker.track(ret, options.allReferencesReleased);

  return ret;
}

const sharedTexture = {
  subtle: sharedTextureNative,
  importSharedTexture,
  sendSharedTexture
};

export default sharedTexture;
