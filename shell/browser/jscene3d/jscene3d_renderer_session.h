// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_SESSION_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_SESSION_H_

#include <cstdint>
#include <memory>
#include <string>

#include "base/functional/callback.h"
#include "base/memory/weak_ptr.h"
#include "shell/browser/jscene3d/jscene3d_renderer_process.h"
#include "v8/include/v8-forward.h"
#include "v8/include/v8-persistent-handle.h"

namespace electron {

class JScene3DControlChannel;
struct JScene3DRendererLaunchSpec;
class JScene3DSurface;

class JScene3DRendererSession {
 public:
  enum class State {
    kStarting,
    kRunning,
    kPaused,
    kFailed,
    kStopping,
    kStopped,
  };

  JScene3DRendererSession(int32_t width, int32_t height);
  JScene3DRendererSession(const JScene3DRendererSession&) = delete;
  JScene3DRendererSession& operator=(const JScene3DRendererSession&) = delete;
  ~JScene3DRendererSession();

  bool Launch(const JScene3DRendererLaunchSpec& launch_spec);

  base::ProcessId pid() const;
  JScene3DSurface* surface() const;
  uint64_t surface_generation() const;
  State state() const;
  const char* state_name() const;

  void SetRendererReadyCallback(v8::Isolate* isolate,
                                v8::Local<v8::Function> callback);
  void SetFrameReadyCallback(v8::Isolate* isolate,
                             v8::Local<v8::Function> callback);
  void SetRendererExitCallback(v8::Isolate* isolate,
                               v8::Local<v8::Function> callback);
  void SetSurfaceReadyCallback(v8::Isolate* isolate,
                               v8::Local<v8::Function> callback);
  void SetSceneSelectionCallback(v8::Isolate* isolate,
                                 v8::Local<v8::Function> callback);
  void SetSceneTransformCallback(v8::Isolate* isolate,
                                 v8::Local<v8::Function> callback);
  void SetSceneCameraStateCallback(v8::Isolate* isolate,
                                   v8::Local<v8::Function> callback);

  bool SendCommand(const std::string& command);
  bool Resize(uint64_t expected_surface_generation,
              int32_t width,
              int32_t height);
  bool Pause();
  bool Resume();
  void Stop(base::OnceClosure completion);

 private:
  bool PromotePendingSurface(const std::string& line);
  void ProcessOutputLine(const std::string& line);
  void HandleRendererExit();
  void CompleteStop(bool requested_stop, base::OnceClosure completion);
  void InvokeRendererReadyCallback();
  void InvokeFrameReadyCallback();
  void InvokeRendererExitCallback();
  void InvokeSurfaceReadyCallback();
  void InvokeSceneSelectionCallback(uint64_t request_id,
                                    uint64_t revision,
                                    const std::string& selection);
  void InvokeSceneTransformCallback(const std::string& transform);
  void InvokeSceneCameraStateCallback(const std::string& camera_state);

  const int32_t initial_width_;
  const int32_t initial_height_;
  std::unique_ptr<JScene3DControlChannel> control_channel_;
  std::unique_ptr<JScene3DSurface> surface_;
  std::unique_ptr<JScene3DSurface> pending_surface_;
  JScene3DRendererProcess process_;
  uint64_t surface_generation_ = 1;
  State state_ = State::kStarting;
  v8::Global<v8::Function> renderer_ready_callback_;
  v8::Global<v8::Function> frame_ready_callback_;
  v8::Global<v8::Function> renderer_exit_callback_;
  v8::Global<v8::Function> surface_ready_callback_;
  v8::Global<v8::Function> scene_selection_callback_;
  v8::Global<v8::Function> scene_transform_callback_;
  v8::Global<v8::Function> scene_camera_state_callback_;
  base::WeakPtrFactory<JScene3DRendererSession> weak_factory_{this};
};

}  // namespace electron

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_SESSION_H_
