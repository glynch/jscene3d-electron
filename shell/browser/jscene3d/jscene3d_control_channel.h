// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_CONTROL_CHANNEL_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_CONTROL_CHANNEL_H_

#include <memory>

namespace base {
struct LaunchOptions;
}

namespace electron {

class JScene3DSurface;

class JScene3DControlChannel {
 public:
  virtual ~JScene3DControlChannel() = default;

  JScene3DControlChannel(const JScene3DControlChannel&) = delete;
  JScene3DControlChannel& operator=(const JScene3DControlChannel&) = delete;

 protected:
  JScene3DControlChannel() = default;
};

std::unique_ptr<JScene3DControlChannel> CreateJScene3DControlChannel();

bool ConfigureJScene3DControlChannelForLaunch(JScene3DControlChannel& channel,
                                              base::LaunchOptions& options);

bool SendJScene3DSurface(JScene3DControlChannel& channel,
                         JScene3DSurface& surface);

}  // namespace electron

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_CONTROL_CHANNEL_H_
