// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#include "shell/browser/jscene3d/jscene3d_renderer_process.h"

#include <array>
#include <cerrno>
#include <csignal>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <unistd.h>

#include "base/apple/foundation_util.h"
#include "base/containers/span.h"
#include "base/files/file_path.h"
#include "base/functional/bind.h"
#include "base/functional/callback_helpers.h"
#include "base/posix/eintr_wrapper.h"
#include "base/process/launch.h"
#include "base/task/sequenced_task_runner.h"
#include "base/task/thread_pool.h"
#include "base/time/time.h"
#include "shell/browser/jscene3d/jscene3d_renderer_launch_spec.h"
#include "shell/browser/jscene3d/jscene3d_renderer_protocol.h"

namespace electron {

namespace {

constexpr base::TimeDelta kShutdownGracePeriod = base::Seconds(3);
constexpr base::TimeDelta kForcedShutdownReapPeriod = base::Seconds(3);

void ReapRendererProcess(base::Process process) {
  const base::ProcessId pid = process.Pid();
  int exit_code = 0;
  if (!process.WaitForExitWithTimeout(kShutdownGracePeriod, &exit_code)) {
    std::cerr << "JScene3D renderer PID " << pid
              << " did not exit after SHUTDOWN; killing it\n";
    if (kill(pid, SIGKILL) != 0 && errno != ESRCH) {
      std::cerr << "Failed to kill JScene3D renderer PID " << pid << ": errno "
                << errno << '\n';
    }
    if (!process.WaitForExitWithTimeout(kForcedShutdownReapPeriod,
                                        &exit_code)) {
      std::cerr << "Timed out reaping JScene3D renderer PID " << pid << '\n';
      return;
    }
  }
  std::cout << "JScene3D renderer PID reaped: " << pid << " exit " << exit_code
            << '\n';
}

}  // namespace

JScene3DRendererLaunchSpec::JScene3DRendererLaunchSpec() = default;

JScene3DRendererLaunchSpec::JScene3DRendererLaunchSpec(
    const JScene3DRendererLaunchSpec&) = default;

JScene3DRendererLaunchSpec& JScene3DRendererLaunchSpec::operator=(
    const JScene3DRendererLaunchSpec&) = default;

JScene3DRendererLaunchSpec::JScene3DRendererLaunchSpec(
    JScene3DRendererLaunchSpec&&) noexcept = default;

JScene3DRendererLaunchSpec& JScene3DRendererLaunchSpec::operator=(
    JScene3DRendererLaunchSpec&&) noexcept = default;

JScene3DRendererLaunchSpec::~JScene3DRendererLaunchSpec() = default;

JScene3DRendererProcess::JScene3DRendererProcess() = default;

JScene3DRendererProcess::~JScene3DRendererProcess() {
  if (process_.IsValid()) {
    Stop(base::DoNothing());
  }
}

bool JScene3DRendererProcess::Launch(
    const JScene3DRendererLaunchSpec& launch_spec,
    int32_t width,
    int32_t height,
    base::LaunchOptions options,
    OutputLineCallback output_line_callback,
    ExitCallback exit_callback) {
  if (process_.IsValid() || stopping_ || stopped_) {
    return false;
  }

  int stdin_pipe[2];
  if (HANDLE_EINTR(pipe(stdin_pipe)) < 0) {
    return false;
  }
  base::ScopedFD stdin_read(stdin_pipe[0]);
  base::ScopedFD stdin_write(stdin_pipe[1]);

  const int stdin_write_flags = fcntl(stdin_write.get(), F_GETFL);
  if (stdin_write_flags < 0 ||
      fcntl(stdin_write.get(), F_SETFL, stdin_write_flags | O_NONBLOCK) < 0) {
    return false;
  }

  int stdout_pipe[2];
  if (HANDLE_EINTR(pipe(stdout_pipe)) < 0) {
    return false;
  }
  base::ScopedFD stdout_read(stdout_pipe[0]);
  base::ScopedFD stdout_write(stdout_pipe[1]);

  options.current_directory = base::FilePath(launch_spec.working_directory);
  options.fds_to_remap.emplace_back(stdin_read.get(), STDIN_FILENO);
  options.fds_to_remap.emplace_back(stdout_write.get(), STDOUT_FILENO);

  std::string class_path;
  for (const auto& entry : launch_spec.class_path) {
    if (!class_path.empty()) {
      class_path.push_back(':');
    }
    class_path.append(entry);
  }

  std::vector<std::string> argv = {
      launch_spec.java_executable,
      "-XstartOnFirstThread",
      "-Djava.library.path=" + launch_spec.native_library_directory,
      "-cp",
      class_path,
      launch_spec.main_class,
  };
  argv.insert(argv.end(), launch_spec.renderer_arguments.begin(),
              launch_spec.renderer_arguments.end());

  // The native transport owns these final arguments. They identify the
  // Electron parent whose Mach rendezvous ports the renderer must acquire and
  // the physical dimensions of the initially transferred IOSurface.
  argv.emplace_back(base::apple::BaseBundleID());
  argv.emplace_back(std::to_string(width));
  argv.emplace_back(std::to_string(height));

  base::Process process = base::LaunchProcess(argv, options);
  if (!process.IsValid()) {
    return false;
  }

  stdin_read.reset();
  stdout_write.reset();
  process_ = std::move(process);
  stdin_write_ = std::move(stdin_write);
  stdout_read_ = std::move(stdout_read);
  output_line_callback_ = std::move(output_line_callback);
  exit_callback_ = std::move(exit_callback);
  stdout_watch_ = base::FileDescriptorWatcher::WatchReadable(
      stdout_read_.get(),
      base::BindRepeating(&JScene3DRendererProcess::ProcessOutput,
                          base::Unretained(this)));
  return true;
}

base::ProcessId JScene3DRendererProcess::pid() const {
  return process_.IsValid() ? process_.Pid() : base::kNullProcessId;
}

bool JScene3DRendererProcess::SendCommand(const std::string& command) {
  if (!stdin_write_.is_valid() || stopping_ || stopped_) {
    return false;
  }
  return QueueCommand(command);
}

bool JScene3DRendererProcess::QueueCommand(const std::string& command) {
  if (!stdin_write_.is_valid()) {
    return false;
  }
  stdin_buffer_.append(command);
  if (stdin_buffer_.empty() || stdin_buffer_.back() != '\n') {
    stdin_buffer_.push_back('\n');
  }
  return FlushInput();
}

bool JScene3DRendererProcess::FlushInput() {
  while (stdin_buffer_offset_ < stdin_buffer_.size()) {
    base::span<const char> remaining(stdin_buffer_);
    remaining = remaining.subspan(stdin_buffer_offset_);
    const ssize_t written = HANDLE_EINTR(
        write(stdin_write_.get(), remaining.data(), remaining.size()));
    if (written > 0) {
      stdin_buffer_offset_ += static_cast<size_t>(written);
      continue;
    }
    if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
      if (!stdin_watch_) {
        stdin_watch_ = base::FileDescriptorWatcher::WatchWritable(
            stdin_write_.get(),
            base::BindRepeating(&JScene3DRendererProcess::HandleInputWritable,
                                base::Unretained(this)));
      }
      return true;
    }
    std::cerr << "JScene3D renderer command write failed: errno " << errno
              << '\n';
    stdin_watch_.reset();
    stdin_write_.reset();
    stdin_buffer_.clear();
    stdin_buffer_offset_ = 0;
    return false;
  }
  stdin_watch_.reset();
  stdin_buffer_.clear();
  stdin_buffer_offset_ = 0;
  if (close_stdin_when_drained_) {
    stdin_write_.reset();
  }
  return true;
}

void JScene3DRendererProcess::HandleInputWritable() {
  FlushInput();
}

void JScene3DRendererProcess::Stop(base::OnceClosure completion) {
  if (stopped_) {
    std::move(completion).Run();
    return;
  }
  stop_callbacks_.push_back(std::move(completion));
  if (stopping_) {
    return;
  }
  stopping_ = true;

  if (stdin_write_.is_valid()) {
    // Preserve command ordering and close only after the cooperative shutdown
    // command has left the nonblocking pipe buffer.
    close_stdin_when_drained_ = true;
    QueueCommand(std::string(jscene3d_protocol::kCommandShutdown));
  }
  // Keep stdout open while reaping so the renderer can complete its
  // SHUTDOWN_READY write and exit normally.
  output_line_callback_.Reset();
  exit_callback_.Reset();

  if (!process_.IsValid()) {
    FinishStop();
    return;
  }

  base::ThreadPool::PostTaskAndReply(
      FROM_HERE,
      {base::MayBlock(), base::WithBaseSyncPrimitives(),
       base::TaskShutdownBehavior::BLOCK_SHUTDOWN},
      base::BindOnce(&ReapRendererProcess, std::move(process_)),
      base::BindOnce(&JScene3DRendererProcess::FinishStop,
                     base::Unretained(this)));
}

void JScene3DRendererProcess::FinishStop() {
  stdin_watch_.reset();
  stdin_write_.reset();
  stdin_buffer_.clear();
  stdin_buffer_offset_ = 0;
  close_stdin_when_drained_ = false;
  stdout_watch_.reset();
  stdout_read_.reset();
  stdout_buffer_.clear();
  stopped_ = true;
  stopping_ = false;
  auto callbacks = std::move(stop_callbacks_);
  for (auto& callback : callbacks) {
    std::move(callback).Run();
  }
}

void JScene3DRendererProcess::ProcessOutput() {
  if (!stdout_read_.is_valid()) {
    return;
  }
  std::array<char, 4096> buffer;
  const ssize_t bytes_read =
      HANDLE_EINTR(read(stdout_read_.get(), buffer.data(), buffer.size()));
  if (bytes_read <= 0) {
    if (bytes_read == 0 && !exit_notified_) {
      exit_notified_ = true;
      stdout_watch_.reset();
      stdout_read_.reset();
      if (!exit_callback_.is_null()) {
        ExitCallback callback = exit_callback_;
        base::SequencedTaskRunner::GetCurrentDefault()->PostTask(
            FROM_HERE,
            base::BindOnce([](ExitCallback callback) { callback.Run(); },
                           std::move(callback)));
      }
    }
    return;
  }

  stdout_buffer_.append(buffer.data(), static_cast<size_t>(bytes_read));
  while (true) {
    const size_t newline = stdout_buffer_.find('\n');
    if (newline == std::string::npos) {
      break;
    }
    std::string line = stdout_buffer_.substr(0, newline);
    stdout_buffer_.erase(0, newline + 1);
    if (!line.empty() && line.back() == '\r') {
      line.pop_back();
    }
    ProcessOutputLine(line);
  }
}

void JScene3DRendererProcess::ProcessOutputLine(const std::string& line) {
  if (!output_line_callback_.is_null()) {
    output_line_callback_.Run(line);
  }
}

}  // namespace electron
