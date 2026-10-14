// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_PROTOCOL_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_PROTOCOL_H_

#include <string_view>

namespace electron::jscene3d_protocol {

inline constexpr std::string_view kCommandDrag = "DRAG";

inline constexpr std::string_view kCommandFrame = "FRAME";

inline constexpr std::string_view kCommandReceiveSurface = "RECEIVE_SURFACE";

inline constexpr std::string_view kCommandShutdown = "SHUTDOWN";

inline constexpr std::string_view kEventRendererReady = "RENDERER_READY";

inline constexpr std::string_view kEventFrameReady = "FRAME_READY";

inline constexpr std::string_view kEventSurfaceReady = "SURFACE_READY";

inline constexpr std::string_view kEventSceneTransform = "SCENE_TRANSFORM";

inline constexpr std::string_view kEventSceneCameraState =
    "SCENE_CAMERA_STATE";

}  // namespace electron::jscene3d_protocol

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_PROTOCOL_H_
