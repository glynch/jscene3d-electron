// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_SURFACE_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_SURFACE_H_

#include <cstdint>

namespace electron {

class JScene3DSurface {
 public:
  virtual ~JScene3DSurface() = default;

  virtual int32_t width() const = 0;
  virtual int32_t height() const = 0;
};

}  // namespace electron

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_SURFACE_H_
