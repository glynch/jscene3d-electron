// Copyright (c) 2026
// Use of this source code is governed by the MIT license that can be
// found in the LICENSE file.

#include "shell/browser/jscene3d/jscene3d_control_channel.h"

#include <cstdint>
#include <iostream>
#include <memory>
#include <utility>

#include <mach/mach.h>

#include "base/apple/mach_port_rendezvous.h"
#include "base/apple/scoped_mach_port.h"
#include "base/process/launch.h"
#include "shell/browser/jscene3d/jscene3d_surface.h"
#include "shell/browser/jscene3d/mac/jscene3d_surface_mac.h"

namespace electron {

namespace {

constexpr uint32_t kJScene3DControlKey = 'jctl';

constexpr mach_msg_id_t kJScene3DSurfaceMessageId = 'jsrf';

constexpr mach_msg_timeout_t kJScene3DSurfaceSendTimeoutMs = 3000;

struct JScene3DSurfaceMessage {
  mach_msg_header_t header;
  mach_msg_body_t body;
  mach_msg_port_descriptor_t surface_port;
  int32_t width;
  int32_t height;
};

class JScene3DControlChannelMac final : public JScene3DControlChannel {
 public:
  JScene3DControlChannelMac(base::apple::ScopedMachReceiveRight receive_right,
                            base::apple::ScopedMachSendRight send_right)
      : receive_right_(std::move(receive_right)),
        send_right_(std::move(send_right)) {}

  ~JScene3DControlChannelMac() override = default;

  base::apple::ScopedMachReceiveRight TakeReceiveRight() {
    return std::move(receive_right_);
  }

  mach_port_t send_right() const { return send_right_.get(); }

 private:
  base::apple::ScopedMachReceiveRight receive_right_;
  base::apple::ScopedMachSendRight send_right_;
};

JScene3DControlChannelMac* GetMacChannel(JScene3DControlChannel& channel) {
  return static_cast<JScene3DControlChannelMac*>(&channel);
}

JScene3DSurfaceMac* GetMacSurface(JScene3DSurface& surface) {
  return static_cast<JScene3DSurfaceMac*>(&surface);
}

}  // namespace

std::unique_ptr<JScene3DControlChannel> CreateJScene3DControlChannel() {
  base::apple::ScopedMachReceiveRight receive_right;
  base::apple::ScopedMachSendRight send_right;

  if (!base::apple::CreateMachPort(&receive_right, &send_right)) {
    return nullptr;
  }

  return std::make_unique<JScene3DControlChannelMac>(std::move(receive_right),
                                                     std::move(send_right));
}

bool ConfigureJScene3DControlChannelForLaunch(JScene3DControlChannel& channel,
                                              base::LaunchOptions& options) {
  JScene3DControlChannelMac* mac_channel = GetMacChannel(channel);

  auto receive_right = mac_channel->TakeReceiveRight();

  if (!receive_right.is_valid()) {
    return false;
  }

  base::MachRendezvousPort rendezvous_port(std::move(receive_right));

  options.mach_ports_for_rendezvous[kJScene3DControlKey] = rendezvous_port;

  return true;
}

bool SendJScene3DSurface(JScene3DControlChannel& channel,
                         JScene3DSurface& surface) {
  JScene3DControlChannelMac* mac_channel = GetMacChannel(channel);

  JScene3DSurfaceMac* mac_surface = GetMacSurface(surface);

  if (mac_channel->send_right() == MACH_PORT_NULL) {
    std::cerr << "JScene3D control channel has no send right\n";
    return false;
  }

  auto surface_right = mac_surface->CreateMachSendRight();

  if (!surface_right.is_valid()) {
    std::cerr << "Failed to create JScene3D IOSurface "
                 "Mach send right\n";
    return false;
  }

  JScene3DSurfaceMessage message{};

  message.header.msgh_bits =
      MACH_MSGH_BITS_COMPLEX | MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);

  message.header.msgh_size = sizeof(message);

  message.header.msgh_remote_port = mac_channel->send_right();

  message.header.msgh_local_port = MACH_PORT_NULL;

  message.header.msgh_id = kJScene3DSurfaceMessageId;

  message.body.msgh_descriptor_count = 1;

  message.surface_port.name = surface_right.release();

  message.surface_port.disposition = MACH_MSG_TYPE_MOVE_SEND;

  message.surface_port.type = MACH_MSG_PORT_DESCRIPTOR;

  message.width = surface.width();

  message.height = surface.height();

  mach_msg_return_t result =
      mach_msg(&message.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
               message.header.msgh_size, 0, MACH_PORT_NULL,
               kJScene3DSurfaceSendTimeoutMs, MACH_PORT_NULL);

  if (result != MACH_MSG_SUCCESS) {
    std::cerr << "JScene3D control send failed: 0x" << std::hex << result
              << std::dec << '\n';

    if (message.surface_port.name != MACH_PORT_NULL) {
      mach_port_deallocate(mach_task_self(), message.surface_port.name);
    }

    return false;
  }

  return true;
}

}  // namespace electron
