// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#include <cmath>
#include <cstdint>
#include <limits>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "base/containers/flat_map.h"
#include "base/files/file_path.h"
#include "base/functional/bind.h"
#include "base/location.h"
#include "base/no_destructor.h"
#include "base/task/sequenced_task_runner.h"
#include "gin/dictionary.h"
#include "gin/function_template.h"
#include "shell/browser/javascript_environment.h"
#include "shell/browser/jscene3d/jscene3d_platform.h"
#include "shell/browser/jscene3d/jscene3d_renderer_launch_spec.h"
#include "shell/browser/jscene3d/jscene3d_renderer_session.h"
#include "shell/browser/jscene3d/jscene3d_surface.h"
#include "shell/common/node_includes.h"

namespace {

constexpr int32_t kMaximumSurfaceDimension = 16384;
constexpr size_t kMaximumControlMessageLength = 16 * 1024 * 1024;

using RendererSessions =
    base::flat_map<int32_t, std::unique_ptr<electron::JScene3DRendererSession>>;
using JavaScriptCallback = v8::Global<v8::Function>;

RendererSessions& GetRendererSessions() {
  static base::NoDestructor<RendererSessions> sessions;
  return *sessions;
}

int32_t& NextRendererSessionId() {
  static int32_t next_id = 1;
  return next_id;
}

electron::JScene3DRendererSession* FindRendererSession(int32_t session_id) {
  auto& sessions = GetRendererSessions();
  const auto it = sessions.find(session_id);
  return it == sessions.end() ? nullptr : it->second.get();
}

bool IsValidDimension(int32_t dimension) {
  return dimension > 0 && dimension <= kMaximumSurfaceDimension;
}

bool IsAbsoluteNonemptyPath(const std::string& path) {
  return !path.empty() && path.find('\0') == std::string::npos &&
         base::FilePath(path).IsAbsolute();
}

bool IsValidMainClass(const std::string& main_class) {
  if (main_class.empty()) {
    return false;
  }
  for (const char character : main_class) {
    const bool valid = (character >= 'a' && character <= 'z') ||
                       (character >= 'A' && character <= 'Z') ||
                       (character >= '0' && character <= '9') ||
                       character == '_' || character == '$' || character == '.';
    if (!valid) {
      return false;
    }
  }
  return true;
}

bool ParseLaunchRequest(v8::Isolate* isolate,
                        v8::Local<v8::Value> value,
                        electron::JScene3DRendererLaunchSpec* launch_spec,
                        int32_t* width,
                        int32_t* height) {
  if (!value->IsObject()) {
    return false;
  }
  gin::Dictionary request(isolate, value.As<v8::Object>());
  if (!request.Get("javaExecutable", &launch_spec->java_executable) ||
      !request.Get("workingDirectory", &launch_spec->working_directory) ||
      !request.Get("nativeLibraryDirectory",
                   &launch_spec->native_library_directory) ||
      !request.Get("classPath", &launch_spec->class_path) ||
      !request.Get("mainClass", &launch_spec->main_class) ||
      !request.Get("rendererArguments", &launch_spec->renderer_arguments) ||
      !request.Get("width", width) || !request.Get("height", height)) {
    return false;
  }
  if (!IsAbsoluteNonemptyPath(launch_spec->java_executable) ||
      !IsAbsoluteNonemptyPath(launch_spec->working_directory) ||
      !IsAbsoluteNonemptyPath(launch_spec->native_library_directory) ||
      launch_spec->class_path.empty() ||
      !IsValidMainClass(launch_spec->main_class) || !IsValidDimension(*width) ||
      !IsValidDimension(*height)) {
    return false;
  }
  for (const auto& entry : launch_spec->class_path) {
    if (!IsAbsoluteNonemptyPath(entry)) {
      return false;
    }
  }
  for (const auto& argument : launch_spec->renderer_arguments) {
    if (argument.find('\0') != std::string::npos) {
      return false;
    }
  }
  return true;
}

void InvokeJavaScriptCallback(JavaScriptCallback callback) {
  if (callback.IsEmpty()) {
    return;
  }
  v8::Isolate* isolate = electron::JavascriptEnvironment::GetIsolate();
  v8::HandleScope handle_scope(isolate);
  const v8::Local<v8::Context> context = isolate->GetCurrentContext();
  if (context.IsEmpty()) {
    return;
  }
  v8::Context::Scope context_scope(context);
  v8::TryCatch try_catch(isolate);
  callback.Get(isolate)
      ->Call(context, v8::Undefined(isolate), 0, nullptr)
      .FromMaybe(v8::Local<v8::Value>());
}

void RemoveStoppedRenderer(int32_t session_id, JavaScriptCallback callback) {
  auto& sessions = GetRendererSessions();
  sessions.erase(session_id);
  InvokeJavaScriptCallback(std::move(callback));
}

void RendererStopped(int32_t session_id, JavaScriptCallback callback) {
  // The completion originates from a member of the session. Defer erasure so
  // the session is not destroyed while its callback stack is still active.
  base::SequencedTaskRunner::GetCurrentDefault()->PostTask(
      FROM_HERE,
      base::BindOnce(&RemoveStoppedRenderer, session_id, std::move(callback)));
}

v8::Local<v8::Value> LaunchRenderer(v8::Isolate* isolate,
                                    v8::Local<v8::Value> request,
                                    v8::Local<v8::Value> observers) {
  electron::JScene3DRendererLaunchSpec launch_spec;
  int32_t width = 0;
  int32_t height = 0;
  if (!ParseLaunchRequest(isolate, request, &launch_spec, &width, &height) ||
      !observers->IsObject() || NextRendererSessionId() <= 0) {
    return v8::Null(isolate);
  }

  gin::Dictionary observer_dictionary(isolate, observers.As<v8::Object>());
  v8::Local<v8::Function> renderer_ready_callback;
  v8::Local<v8::Function> frame_ready_callback;
  v8::Local<v8::Function> renderer_exit_callback;
  v8::Local<v8::Function> surface_ready_callback;
  v8::Local<v8::Function> scene_selection_callback;
  v8::Local<v8::Function> scene_transform_callback;
  if (!observer_dictionary.Get("onReady", &renderer_ready_callback) ||
      !observer_dictionary.Get("onFrameReady", &frame_ready_callback) ||
      !observer_dictionary.Get("onExit", &renderer_exit_callback) ||
      !observer_dictionary.Get("onSurfaceReady", &surface_ready_callback) ||
      !observer_dictionary.Get("onSceneSelection", &scene_selection_callback) ||
      !observer_dictionary.Get("onSceneTransform", &scene_transform_callback)) {
    return v8::Null(isolate);
  }

  const int32_t session_id = NextRendererSessionId();
  NextRendererSessionId() =
      session_id == std::numeric_limits<int32_t>::max() ? 0 : session_id + 1;

  auto session =
      std::make_unique<electron::JScene3DRendererSession>(width, height);
  session->SetRendererReadyCallback(isolate, renderer_ready_callback);
  session->SetFrameReadyCallback(isolate, frame_ready_callback);
  session->SetRendererExitCallback(isolate, renderer_exit_callback);
  session->SetSurfaceReadyCallback(isolate, surface_ready_callback);
  session->SetSceneSelectionCallback(isolate, scene_selection_callback);
  session->SetSceneTransformCallback(isolate, scene_transform_callback);
  if (!session->Launch(launch_spec)) {
    return v8::Null(isolate);
  }

  const base::ProcessId pid = session->pid();
  GetRendererSessions().emplace(session_id, std::move(session));
  v8::Local<v8::Object> object = v8::Object::New(isolate);
  gin::Dictionary result(isolate, object);
  result.Set("sessionId", session_id);
  result.Set("pid", static_cast<int32_t>(pid));
  result.Set("surfaceGeneration", 1);
  return object;
}

v8::Local<v8::Value> GetSurface(v8::Isolate* isolate, int32_t session_id) {
  auto* session = FindRendererSession(session_id);
  if (!session || !session->surface()) {
    return v8::Null(isolate);
  }
  electron::JScene3DSurface* surface = session->surface();
  v8::Local<v8::Value> handle =
      electron::GetJScene3DSharedTextureHandle(isolate, *surface);
  if (handle->IsNull()) {
    return v8::Null(isolate);
  }
  v8::Local<v8::Object> object = v8::Object::New(isolate);
  gin::Dictionary result(isolate, object);
  result.Set("handle", handle);
  result.Set("width", surface->width());
  result.Set("height", surface->height());
  result.Set("surfaceGeneration",
             static_cast<double>(session->surface_generation()));
  return object;
}

v8::Local<v8::Value> GetSessionState(v8::Isolate* isolate, int32_t session_id) {
  auto* session = FindRendererSession(session_id);
  if (!session) {
    return v8::Null(isolate);
  }
  v8::Local<v8::Object> object = v8::Object::New(isolate);
  gin::Dictionary result(isolate, object);
  result.Set("sessionId", session_id);
  result.Set("pid", static_cast<int32_t>(session->pid()));
  result.Set("state", std::string(session->state_name()));
  result.Set("surfaceGeneration",
             static_cast<double>(session->surface_generation()));
  return object;
}

bool SendRendererMessage(int32_t session_id, const std::string& message) {
  auto* session = FindRendererSession(session_id);
  return session && !message.empty() &&
         message.size() <= kMaximumControlMessageLength &&
         message.find('\0') == std::string::npos &&
         message.find('\n') == std::string::npos &&
         message.find('\r') == std::string::npos &&
         session->SendCommand(message);
}

bool ReplaceSurface(int32_t session_id,
                    double expected_surface_generation,
                    int32_t width,
                    int32_t height) {
  auto* session = FindRendererSession(session_id);
  if (!session || !std::isfinite(expected_surface_generation) ||
      expected_surface_generation < 1 ||
      std::floor(expected_surface_generation) != expected_surface_generation ||
      !IsValidDimension(width) || !IsValidDimension(height)) {
    return false;
  }
  return session->Resize(static_cast<uint64_t>(expected_surface_generation),
                         width, height);
}

bool PauseRenderer(int32_t session_id) {
  auto* session = FindRendererSession(session_id);
  return session && session->Pause();
}

bool ResumeRenderer(int32_t session_id) {
  auto* session = FindRendererSession(session_id);
  return session && session->Resume();
}

bool StopRenderer(v8::Isolate* isolate,
                  int32_t session_id,
                  v8::Local<v8::Function> callback) {
  auto* session = FindRendererSession(session_id);
  if (!session) {
    return false;
  }
  session->Stop(base::BindOnce(&RendererStopped, session_id,
                               JavaScriptCallback(isolate, callback)));
  return true;
}

std::vector<int32_t> GetRendererSessionIds() {
  std::vector<int32_t> result;
  result.reserve(GetRendererSessions().size());
  for (const auto& [session_id, session] : GetRendererSessions()) {
    result.push_back(session_id);
  }
  return result;
}

void Initialize(v8::Local<v8::Object> exports,
                v8::Local<v8::Value> unused,
                v8::Local<v8::Context> context,
                void* priv) {
  v8::Isolate* isolate = electron::JavascriptEnvironment::GetIsolate();
  gin::Dictionary dict{isolate, exports};
  dict.Set("launchRenderer", gin::CreateFunctionTemplate(
                                 isolate, base::BindRepeating(&LaunchRenderer))
                                 ->GetFunction(context)
                                 .ToLocalChecked());
  dict.Set("getSurface", gin::CreateFunctionTemplate(
                             isolate, base::BindRepeating(&GetSurface))
                             ->GetFunction(context)
                             .ToLocalChecked());
  dict.Set("getSessionState",
           gin::CreateFunctionTemplate(isolate,
                                       base::BindRepeating(&GetSessionState))
               ->GetFunction(context)
               .ToLocalChecked());
  dict.Set("sendRendererMessage",
           gin::CreateFunctionTemplate(
               isolate, base::BindRepeating(&SendRendererMessage))
               ->GetFunction(context)
               .ToLocalChecked());
  dict.Set("replaceSurface", gin::CreateFunctionTemplate(
                                 isolate, base::BindRepeating(&ReplaceSurface))
                                 ->GetFunction(context)
                                 .ToLocalChecked());
  dict.Set("pauseRenderer", gin::CreateFunctionTemplate(
                                isolate, base::BindRepeating(&PauseRenderer))
                                ->GetFunction(context)
                                .ToLocalChecked());
  dict.Set("resumeRenderer", gin::CreateFunctionTemplate(
                                 isolate, base::BindRepeating(&ResumeRenderer))
                                 ->GetFunction(context)
                                 .ToLocalChecked());
  dict.Set("stopRenderer", gin::CreateFunctionTemplate(
                               isolate, base::BindRepeating(&StopRenderer))
                               ->GetFunction(context)
                               .ToLocalChecked());
  dict.Set("getRendererSessionIds",
           gin::CreateFunctionTemplate(
               isolate, base::BindRepeating(&GetRendererSessionIds))
               ->GetFunction(context)
               .ToLocalChecked());
}

}  // namespace

NODE_LINKED_BINDING_CONTEXT_AWARE(electron_browser_jscene3d_renderer,
                                  Initialize)
