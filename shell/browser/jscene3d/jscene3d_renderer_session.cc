// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#include "shell/browser/jscene3d/jscene3d_renderer_session.h"

#include <iostream>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>

#include "base/functional/bind.h"
#include "base/functional/callback_helpers.h"
#include "base/process/launch.h"
#include "shell/browser/javascript_environment.h"
#include "shell/browser/jscene3d/jscene3d_control_channel.h"
#include "shell/browser/jscene3d/jscene3d_platform.h"
#include "shell/browser/jscene3d/jscene3d_renderer_launch_spec.h"
#include "shell/browser/jscene3d/jscene3d_renderer_protocol.h"
#include "shell/browser/jscene3d/jscene3d_surface.h"
#include "shell/common/node_includes.h"

namespace electron {

namespace {

void Invoke(v8::Global<v8::Function>& callback,
            int argument_count,
            v8::Local<v8::Value>* arguments) {
  if (callback.IsEmpty()) {
    return;
  }
  v8::Isolate* isolate = JavascriptEnvironment::GetIsolate();
  v8::HandleScope handle_scope(isolate);
  v8::Local<v8::Context> context = isolate->GetCurrentContext();
  if (context.IsEmpty()) {
    return;
  }
  v8::Context::Scope context_scope(context);
  v8::TryCatch try_catch(isolate);
  callback.Get(isolate)
      ->Call(context, v8::Undefined(isolate), argument_count, arguments)
      .FromMaybe(v8::Local<v8::Value>());
}

}  // namespace

JScene3DRendererSession::JScene3DRendererSession(int32_t width, int32_t height)
    : initial_width_(width), initial_height_(height) {}

JScene3DRendererSession::~JScene3DRendererSession() = default;

bool JScene3DRendererSession::Launch(
    const JScene3DRendererLaunchSpec& launch_spec) {
  if (process_.pid() != base::kNullProcessId || initial_width_ <= 0 ||
      initial_height_ <= 0) {
    return false;
  }

  surface_ = CreateJScene3DSurface(initial_width_, initial_height_);
  control_channel_ = CreateJScene3DControlChannel();
  if (!surface_ || !control_channel_) {
    surface_.reset();
    control_channel_.reset();
    return false;
  }

  base::LaunchOptions options;
  if (!ConfigureJScene3DSurfaceForLaunch(*surface_, options) ||
      !ConfigureJScene3DControlChannelForLaunch(*control_channel_, options)) {
    surface_.reset();
    control_channel_.reset();
    return false;
  }

  if (!process_.Launch(
          launch_spec, surface_->width(), surface_->height(),
          std::move(options),
          base::BindRepeating(&JScene3DRendererSession::ProcessOutputLine,
                              weak_factory_.GetWeakPtr()),
          base::BindRepeating(&JScene3DRendererSession::HandleRendererExit,
                              weak_factory_.GetWeakPtr()))) {
    surface_.reset();
    control_channel_.reset();
    return false;
  }
  return true;
}

base::ProcessId JScene3DRendererSession::pid() const {
  return process_.pid();
}

JScene3DSurface* JScene3DRendererSession::surface() const {
  return surface_.get();
}

uint64_t JScene3DRendererSession::surface_generation() const {
  return surface_generation_;
}

JScene3DRendererSession::State JScene3DRendererSession::state() const {
  return state_;
}

const char* JScene3DRendererSession::state_name() const {
  switch (state_) {
    case State::kStarting:
      return "starting";
    case State::kRunning:
      return "running";
    case State::kPaused:
      return "paused";
    case State::kFailed:
      return "failed";
    case State::kStopping:
      return "stopping";
    case State::kStopped:
      return "stopped";
  }
  return "unknown";
}

void JScene3DRendererSession::SetRendererReadyCallback(
    v8::Isolate* isolate,
    v8::Local<v8::Function> callback) {
  renderer_ready_callback_.Reset(isolate, callback);
}

void JScene3DRendererSession::SetFrameReadyCallback(
    v8::Isolate* isolate,
    v8::Local<v8::Function> callback) {
  frame_ready_callback_.Reset(isolate, callback);
}

void JScene3DRendererSession::SetRendererExitCallback(
    v8::Isolate* isolate,
    v8::Local<v8::Function> callback) {
  renderer_exit_callback_.Reset(isolate, callback);
}

void JScene3DRendererSession::SetSurfaceReadyCallback(
    v8::Isolate* isolate,
    v8::Local<v8::Function> callback) {
  surface_ready_callback_.Reset(isolate, callback);
}

void JScene3DRendererSession::SetSceneSelectionCallback(
    v8::Isolate* isolate,
    v8::Local<v8::Function> callback) {
  scene_selection_callback_.Reset(isolate, callback);
}

void JScene3DRendererSession::SetSceneTransformCallback(
    v8::Isolate* isolate,
    v8::Local<v8::Function> callback) {
  scene_transform_callback_.Reset(isolate, callback);
}

bool JScene3DRendererSession::SendCommand(const std::string& command) {
  return state_ == State::kRunning && process_.SendCommand(command);
}

bool JScene3DRendererSession::Resize(uint64_t expected_surface_generation,
                                     int32_t width,
                                     int32_t height) {
  if ((state_ != State::kRunning && state_ != State::kPaused) || width <= 0 ||
      height <= 0 || expected_surface_generation != surface_generation_ ||
      !control_channel_ || !surface_ || pending_surface_) {
    return false;
  }
  if (width == surface_->width() && height == surface_->height()) {
    return true;
  }

  pending_surface_ = CreateJScene3DSurface(width, height);
  if (!pending_surface_ ||
      !SendJScene3DSurface(*control_channel_, *pending_surface_)) {
    pending_surface_.reset();
    return false;
  }
  if (!process_.SendCommand(
          std::string(jscene3d_protocol::kCommandReceiveSurface))) {
    pending_surface_.reset();
    HandleRendererExit();
    return false;
  }
  return true;
}

bool JScene3DRendererSession::Pause() {
  if (state_ != State::kRunning) {
    return false;
  }
  state_ = State::kPaused;
  return true;
}

bool JScene3DRendererSession::Resume() {
  if (state_ != State::kPaused) {
    return false;
  }
  state_ = State::kRunning;
  return true;
}

void JScene3DRendererSession::Stop(base::OnceClosure completion) {
  const bool requested_stop = state_ != State::kFailed;
  if (state_ != State::kStopped && state_ != State::kFailed) {
    state_ = State::kStopping;
  }
  process_.Stop(base::BindOnce(&JScene3DRendererSession::CompleteStop,
                               weak_factory_.GetWeakPtr(), requested_stop,
                               std::move(completion)));
}

bool JScene3DRendererSession::PromotePendingSurface(const std::string& line) {
  if (!pending_surface_) {
    return false;
  }
  std::istringstream stream(line);
  std::string event;
  int32_t width = 0;
  int32_t height = 0;
  stream >> event >> width >> height;
  if (!stream || event != jscene3d_protocol::kEventSurfaceReady ||
      width != pending_surface_->width() ||
      height != pending_surface_->height()) {
    return false;
  }

  // Releasing the old Electron reference here is safe: the Java process has
  // acknowledged that it acquired and bound the replacement before emitting
  // SURFACE_READY. Any imported SharedTexture separately retains IOSurface.
  surface_ = std::move(pending_surface_);
  ++surface_generation_;
  InvokeSurfaceReadyCallback();
  return true;
}

void JScene3DRendererSession::ProcessOutputLine(const std::string& line) {
  if (line == jscene3d_protocol::kEventRendererReady) {
    if (state_ == State::kStarting) {
      state_ = State::kRunning;
      InvokeRendererReadyCallback();
    }
    return;
  }
  if (line == jscene3d_protocol::kEventFrameReady) {
    InvokeFrameReadyCallback();
    return;
  }
  const std::string prefix =
      std::string(jscene3d_protocol::kEventSurfaceReady) + " ";
  if (line.starts_with(prefix)) {
    if (!PromotePendingSurface(line)) {
      std::cerr << "Rejected invalid JScene3D surface-ready event\n";
    }
    return;
  }
  constexpr std::string_view kSceneSelectionPrefix = "SCENE_SELECTION ";
  if (line.starts_with(kSceneSelectionPrefix)) {
    std::istringstream stream(line);
    std::string event;
    uint64_t request_id = 0;
    uint64_t revision = 0;
    std::string selection;
    std::string trailing;
    stream >> event >> request_id >> revision >> selection;
    if (stream && !(stream >> trailing) && event == "SCENE_SELECTION" &&
        !selection.empty()) {
      InvokeSceneSelectionCallback(request_id, revision, selection);
    } else {
      std::cerr << "Rejected invalid JScene3D scene-selection event\n";
    }
    return;
  }
  const std::string transform_prefix =
      std::string(jscene3d_protocol::kEventSceneTransform) + " ";
  if (line.starts_with(transform_prefix)) {
    const std::string transform = line.substr(transform_prefix.size());
    if (!transform.empty() && transform.find(' ') == std::string::npos) {
      InvokeSceneTransformCallback(transform);
    } else {
      std::cerr << "Rejected invalid JScene3D scene-transform event\n";
    }
    return;
  }
  if (!line.empty()) {
    std::cout << "JScene3D renderer: " << line << '\n';
  }
}

void JScene3DRendererSession::HandleRendererExit() {
  if (state_ == State::kStopping || state_ == State::kStopped ||
      state_ == State::kFailed) {
    return;
  }
  state_ = State::kFailed;
  InvokeRendererExitCallback();
  process_.Stop(base::BindOnce(&JScene3DRendererSession::CompleteStop,
                               weak_factory_.GetWeakPtr(), false,
                               base::DoNothing()));
}

void JScene3DRendererSession::CompleteStop(bool requested_stop,
                                           base::OnceClosure completion) {
  pending_surface_.reset();
  surface_.reset();
  control_channel_.reset();
  renderer_ready_callback_.Reset();
  frame_ready_callback_.Reset();
  surface_ready_callback_.Reset();
  scene_selection_callback_.Reset();
  scene_transform_callback_.Reset();
  renderer_exit_callback_.Reset();
  if (requested_stop && state_ != State::kFailed) {
    state_ = State::kStopped;
  }
  std::move(completion).Run();
}

void JScene3DRendererSession::InvokeRendererReadyCallback() {
  Invoke(renderer_ready_callback_, 0, nullptr);
}

void JScene3DRendererSession::InvokeFrameReadyCallback() {
  Invoke(frame_ready_callback_, 0, nullptr);
}

void JScene3DRendererSession::InvokeRendererExitCallback() {
  Invoke(renderer_exit_callback_, 0, nullptr);
}

void JScene3DRendererSession::InvokeSurfaceReadyCallback() {
  if (surface_ready_callback_.IsEmpty() || !surface_) {
    return;
  }
  v8::Isolate* isolate = JavascriptEnvironment::GetIsolate();
  v8::HandleScope handle_scope(isolate);
  v8::Local<v8::Context> context = isolate->GetCurrentContext();
  if (context.IsEmpty()) {
    return;
  }
  v8::Context::Scope context_scope(context);
  v8::Local<v8::Value> arguments[] = {
      v8::Number::New(isolate, static_cast<double>(surface_generation_)),
      v8::Integer::New(isolate, surface_->width()),
      v8::Integer::New(isolate, surface_->height()),
  };
  v8::TryCatch try_catch(isolate);
  surface_ready_callback_.Get(isolate)
      ->Call(context, v8::Undefined(isolate), 3, arguments)
      .FromMaybe(v8::Local<v8::Value>());
}

void JScene3DRendererSession::InvokeSceneSelectionCallback(
    uint64_t request_id,
    uint64_t revision,
    const std::string& selection) {
  if (scene_selection_callback_.IsEmpty()) {
    return;
  }
  v8::Isolate* isolate = JavascriptEnvironment::GetIsolate();
  v8::HandleScope handle_scope(isolate);
  v8::Local<v8::Context> context = isolate->GetCurrentContext();
  if (context.IsEmpty()) {
    return;
  }
  v8::Context::Scope context_scope(context);
  v8::Local<v8::String> selection_value;
  if (!v8::String::NewFromUtf8(isolate, selection.c_str())
           .ToLocal(&selection_value)) {
    return;
  }
  v8::Local<v8::Value> arguments[] = {
      v8::Number::New(isolate, static_cast<double>(request_id)),
      v8::Number::New(isolate, static_cast<double>(revision)),
      selection_value,
  };
  v8::TryCatch try_catch(isolate);
  scene_selection_callback_.Get(isolate)
      ->Call(context, v8::Undefined(isolate), 3, arguments)
      .FromMaybe(v8::Local<v8::Value>());
}

void JScene3DRendererSession::InvokeSceneTransformCallback(
    const std::string& transform) {
  if (scene_transform_callback_.IsEmpty()) {
    return;
  }
  v8::Isolate* isolate = JavascriptEnvironment::GetIsolate();
  v8::HandleScope handle_scope(isolate);
  v8::Local<v8::Context> context = isolate->GetCurrentContext();
  if (context.IsEmpty()) {
    return;
  }
  v8::Context::Scope context_scope(context);
  v8::Local<v8::String> transform_value;
  if (!v8::String::NewFromUtf8(isolate, transform.c_str())
           .ToLocal(&transform_value)) {
    return;
  }
  v8::Local<v8::Value> arguments[] = {transform_value};
  v8::TryCatch try_catch(isolate);
  scene_transform_callback_.Get(isolate)
      ->Call(context, v8::Undefined(isolate), 1, arguments)
      .FromMaybe(v8::Local<v8::Value>());
}

}  // namespace electron
