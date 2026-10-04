#pragma once
#include "pipeline/Security.h"
#include <map>

namespace cocoon::pipeline {
enum class GroupAction { None, Start, Stop };
struct ProtocolReply {
  Json frame;
  GroupAction action = GroupAction::None;
  bool renew_lease = false;
};
Json descriptor(const Config &config, const PeerIdentity &identity, const std::string &boot,
                const std::string &network_key);
void validate_descriptor(const Json &value, const Config &config, int rank, const PeerIdentity &identity);
void validate_roster(const Json &roster, const Config &config, const Json &head, const Json &member);
std::string group_id(const std::string &head_key, const std::string &epoch, const std::string &digest);
Json request_frame(const Config &config, const std::string &epoch, uint64_t sequence, const std::string &op,
                   const Json &payload);
void validate_frame(const Json &frame, const char *kind);

// Pure state machine: no sockets or process spawning. A successful Commit emits
// Start once; replayed identical lifecycle requests return their original reply.
class MemberSession {
 public:
  MemberSession(Config config, Json self, PeerIdentity head_identity);
  ProtocolReply receive(const Json &frame, bool local_ready);
  const std::string &epoch() const {
    return epoch_;
  }
  const Json &roster() const {
    return roster_;
  }
  const std::string &roster_digest() const {
    return roster_digest_;
  }
  const std::string &id() const {
    return group_id_;
  }
  bool ready() const {
    return ready_;
  }
  bool committed() const {
    return committed_;
  }

 private:
  ProtocolReply apply(const Json &frame, bool local_ready);
  Config config_;
  Json self_, head_, roster_;
  PeerIdentity head_identity_;
  std::string epoch_, challenge_, roster_digest_, group_id_;
  uint64_t sequence_ = 0;
  bool prepared_ = false, committed_ = false, ready_ = false, stopped_ = false;
  std::map<std::string, std::pair<Json, Json>> cache_;
};
}  // namespace cocoon::pipeline
