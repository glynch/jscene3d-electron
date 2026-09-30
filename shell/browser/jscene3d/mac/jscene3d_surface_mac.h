// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_MAC_JSCENE3D_SURFACE_MAC_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_MAC_JSCENE3D_SURFACE_MAC_H_

#include <cstdint>

#include <IOSurface/IOSurface.h>

#include "base/apple/scoped_mach_port.h"
#include "shell/browser/jscene3d/jscene3d_surface.h"

namespace electron {

class JScene3DSurfaceMac final : public JScene3DSurface {
 public:
  JScene3DSurfaceMac(IOSurfaceRef surface, int32_t width, int32_t height);

  JScene3DSurfaceMac(const JScene3DSurfaceMac&) = delete;
  JScene3DSurfaceMac& operator=(const JScene3DSurfaceMac&) = delete;

  ~JScene3DSurfaceMac() override;

  int32_t width() const override;
  int32_t height() const override;

  IOSurfaceRef io_surface() const;

  base::apple::ScopedMachSendRight CreateMachSendRight() const;

 private:
  IOSurfaceRef surface_;
  const int32_t width_;
  const int32_t height_;
};

}  // namespace electron

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_MAC_JSCENE3D_SURFACE_MAC_H_
