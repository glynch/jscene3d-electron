// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#ifndef ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_PROCESS_H_
#define ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_PROCESS_H_

#include <memory>
#include <string>
#include <vector>

#include "base/files/file_descriptor_watcher_posix.h"
#include "base/files/scoped_file.h"
#include "base/functional/callback.h"
#include "base/process/process.h"

namespace base {
struct LaunchOptions;
}

namespace electron {

struct JScene3DRendererLaunchSpec;

class JScene3DRendererProcess {
 public:
  using OutputLineCallback = base::RepeatingCallback<void(const std::string&)>;
  using ExitCallback = base::RepeatingClosure;

  JScene3DRendererProcess();
  JScene3DRendererProcess(const JScene3DRendererProcess&) = delete;
  JScene3DRendererProcess& operator=(const JScene3DRendererProcess&) = delete;
  ~JScene3DRendererProcess();

  bool Launch(const JScene3DRendererLaunchSpec& launch_spec,
              int32_t width,
              int32_t height,
              base::LaunchOptions options,
              OutputLineCallback output_line_callback,
              ExitCallback exit_callback);

  base::ProcessId pid() const;
  bool SendCommand(const std::string& command);

  // Sends the cooperative shutdown command, then reaps on a blocking worker.
  // A child that does not exit within the bounded grace period is killed. All
  // callbacks, including callbacks added by repeated calls, run on the calling
  // sequence after the child has been reaped.
  void Stop(base::OnceClosure completion);

 private:
  bool QueueCommand(const std::string& command);
  bool FlushInput();
  void HandleInputWritable();
  void ProcessOutput();
  void ProcessOutputLine(const std::string& line);
  void FinishStop();

  base::Process process_;
  base::ScopedFD stdin_write_;
  std::unique_ptr<base::FileDescriptorWatcher::Controller> stdin_watch_;
  std::string stdin_buffer_;
  size_t stdin_buffer_offset_ = 0;
  base::ScopedFD stdout_read_;
  std::unique_ptr<base::FileDescriptorWatcher::Controller> stdout_watch_;
  std::string stdout_buffer_;
  OutputLineCallback output_line_callback_;
  ExitCallback exit_callback_;
  std::vector<base::OnceClosure> stop_callbacks_;
  bool stopping_ = false;
  bool stopped_ = false;
  bool exit_notified_ = false;
  bool close_stdin_when_drained_ = false;
};

}  // namespace electron

#endif  // ELECTRON_SHELL_BROWSER_JSCENE3D_JSCENE3D_RENDERER_PROCESS_H_
