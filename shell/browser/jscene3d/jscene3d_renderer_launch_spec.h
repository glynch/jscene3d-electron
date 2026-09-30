// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_LAUNCH_SPEC_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_LAUNCH_SPEC_H_

#include <string>
#include <vector>

namespace electron {

// Java-specific launch data for an isolated JScene3D renderer. The native
// layer deliberately constructs the JVM invocation itself: callers cannot
// supply an environment, shell command, inherited descriptors, or arbitrary
// executable arguments.
struct JScene3DRendererLaunchSpec {
  JScene3DRendererLaunchSpec();
  JScene3DRendererLaunchSpec(const JScene3DRendererLaunchSpec&);
  JScene3DRendererLaunchSpec& operator=(const JScene3DRendererLaunchSpec&);
  JScene3DRendererLaunchSpec(JScene3DRendererLaunchSpec&&) noexcept;
  JScene3DRendererLaunchSpec& operator=(JScene3DRendererLaunchSpec&&) noexcept;
  ~JScene3DRendererLaunchSpec();

  std::string java_executable;
  std::string working_directory;
  std::string native_library_directory;
  std::vector<std::string> class_path;
  std::string main_class;
  std::vector<std::string> renderer_arguments;
};

}  // namespace electron

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_LAUNCH_SPEC_H_
