// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#include "shell/browser/jscene3d/jscene3d_platform.h"

#include <memory>

#include <CoreFoundation/CoreFoundation.h>
#include <CoreVideo/CoreVideo.h>
#include <IOSurface/IOSurface.h>
#include <mach/mach.h>

#include "base/apple/mach_port_rendezvous.h"
#include "base/containers/span.h"
#include "base/process/launch.h"
#include "shell/browser/jscene3d/mac/jscene3d_surface_mac.h"
#include "shell/common/node_includes.h"
#include "shell/common/node_util.h"

namespace electron {

namespace {

constexpr uint32_t kJScene3DSurfaceKey = 'js3d';
constexpr int32_t kBytesPerElement = 4;
constexpr int32_t kPixelFormat = kCVPixelFormatType_32BGRA;

JScene3DSurfaceMac* GetMacSurface(JScene3DSurface& surface) {
  return static_cast<JScene3DSurfaceMac*>(&surface);
}

}  // namespace

JScene3DSurfaceMac::JScene3DSurfaceMac(IOSurfaceRef surface,
                                       int32_t width,
                                       int32_t height)
    : surface_(surface), width_(width), height_(height) {}

JScene3DSurfaceMac::~JScene3DSurfaceMac() {
  if (surface_ != nullptr) {
    CFRelease(surface_);
  }
}

int32_t JScene3DSurfaceMac::width() const {
  return width_;
}

int32_t JScene3DSurfaceMac::height() const {
  return height_;
}

IOSurfaceRef JScene3DSurfaceMac::io_surface() const {
  return surface_;
}

base::apple::ScopedMachSendRight JScene3DSurfaceMac::CreateMachSendRight()
    const {
  if (surface_ == nullptr) {
    return {};
  }

  mach_port_t surface_port = IOSurfaceCreateMachPort(surface_);

  if (surface_port == MACH_PORT_NULL) {
    return {};
  }

  return base::apple::ScopedMachSendRight(surface_port);
}

std::unique_ptr<JScene3DSurface> CreateJScene3DSurface(int32_t width,
                                                       int32_t height) {
  if (width <= 0 || height <= 0) {
    return nullptr;
  }

  CFNumberRef width_value =
      CFNumberCreate(nullptr, kCFNumberSInt32Type, &width);

  CFNumberRef height_value =
      CFNumberCreate(nullptr, kCFNumberSInt32Type, &height);

  CFNumberRef bytes_per_element_value =
      CFNumberCreate(nullptr, kCFNumberSInt32Type, &kBytesPerElement);

  CFNumberRef pixel_format_value =
      CFNumberCreate(nullptr, kCFNumberSInt32Type, &kPixelFormat);

  const void* keys[] = {
      kIOSurfaceWidth,
      kIOSurfaceHeight,
      kIOSurfaceBytesPerElement,
      kIOSurfacePixelFormat,
  };

  const void* values[] = {
      width_value,
      height_value,
      bytes_per_element_value,
      pixel_format_value,
  };

  CFDictionaryRef properties = CFDictionaryCreate(
      nullptr, keys, values, 4, &kCFTypeDictionaryKeyCallBacks,
      &kCFTypeDictionaryValueCallBacks);

  IOSurfaceRef io_surface = IOSurfaceCreate(properties);

  CFRelease(properties);
  CFRelease(width_value);
  CFRelease(height_value);
  CFRelease(bytes_per_element_value);
  CFRelease(pixel_format_value);

  if (io_surface == nullptr) {
    return nullptr;
  }

  return std::make_unique<JScene3DSurfaceMac>(io_surface, width, height);
}

bool ConfigureJScene3DSurfaceForLaunch(JScene3DSurface& surface,
                                       base::LaunchOptions& options) {
  JScene3DSurfaceMac* mac_surface = GetMacSurface(surface);

  auto surface_port = mac_surface->CreateMachSendRight();

  if (!surface_port.is_valid()) {
    return false;
  }

  base::MachRendezvousPort rendezvous_port(std::move(surface_port));

  options.mach_ports_for_rendezvous[kJScene3DSurfaceKey] = rendezvous_port;

  return true;
}

v8::Local<v8::Value> GetJScene3DSharedTextureHandle(v8::Isolate* isolate,
                                                    JScene3DSurface& surface) {
  JScene3DSurfaceMac* mac_surface = GetMacSurface(surface);

  IOSurfaceRef handle = mac_surface->io_surface();

  if (handle == nullptr) {
    return v8::Null(isolate);
  }

  const auto handle_bytes = base::byte_span_from_ref(handle);

  v8::Local<v8::Object> buffer;

  if (!electron::Buffer::Copy(isolate, handle_bytes).ToLocal(&buffer)) {
    return v8::Null(isolate);
  }

  return buffer;
}

}  // namespace electron
