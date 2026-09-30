// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_PLATFORM_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_PLATFORM_H_

#include <cstdint>
#include <memory>

#include "v8/include/v8-forward.h"

namespace base {
struct LaunchOptions;
}

namespace electron {

class JScene3DSurface;

std::unique_ptr<JScene3DSurface> CreateJScene3DSurface(int32_t width,
                                                       int32_t height);

bool ConfigureJScene3DSurfaceForLaunch(JScene3DSurface& surface,
                                       base::LaunchOptions& options);

v8::Local<v8::Value> GetJScene3DSharedTextureHandle(v8::Isolate* isolate,
                                                    JScene3DSurface& surface);

}  // namespace electron

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_PLATFORM_H_
