#include "WorkerProxyConnection.h"
#include "WorkerRunner.h"

#include "auto/tl/cocoon_api.h"
#include "cocoon-tl-utils/cocoon-tl-utils.hpp"
#include "errorcode.h"
#include "tl/TlObject.h"

namespace cocoon {

WorkerRunner *WorkerProxyConnection::runner() {
  return static_cast<WorkerRunner *>(ProxyOutboundConnection::runner());
}

void WorkerProxyConnection::pre_close() {
  runner()->cancel_requests(connection_id());
}

/*
 *
 * Sending handshake to proxy 
 *
 * 1. check that proxy hash is registered in root contract
 * 2. send base worker data to proxy. 
 * 3. we don't send is_disabled here, because worker is considered disable
 *    before end of the handshake process
 *
 */

void WorkerProxyConnection::send_handshake() {
  if (runner()->check_image_hashes()) {
    if (!runner()->runner_config()->root_contract_config->has_proxy_hash(remote_app_hash())) {
      fail_connection(td::Status::Error("invalid proxy hash"));
      return;
    }
  }
  {
    auto S = runner()->check_verification_key(remote_app_type(), verified_by());
    if (S.is_error()) {
      return fail_connection(std::move(S));
    }
  }
  auto flags = 3 | (runner()->min_proto_version() >= 4 ? 4 : 0);
  auto params = ton::create_tl_object<cocoon_api::worker_params>(
      flags, runner()->owner_address().rserialize(true), runner()->model_name(), runner()->coefficient(),
      runner()->is_test(), runner()->proxy_targets_number(), runner()->max_active_requests(),
      runner()->min_proto_version(), runner()->max_proto_version(), runner()->machine_description_json());
  auto req = cocoon::create_serialize_tl_object<cocoon_api::worker_connectToProxy>(std::move(params));
  runner()->send_handshake_query_to_connection(
      connection_id(), "send_proxy_handshake", std::move(req), td::Timestamp::in(30.0),
      [runner = actor_id(runner()), runner_ptr = runner(), self = this,
       connection_id = connection_id()](td::Result<td::BufferSlice> R) mutable {
        td::actor::send_lambda(runner, [self, runner_ptr, connection_id, R = std::move(R)]() mutable {
          if (R.is_ok()) {
            self->received_handshake_answer(R.move_as_ok());
          } else {
            runner_ptr->fail_connection(connection_id, R.move_as_error());
          }
        });
      });
}

/*
 *
 * Receive handshake answer from proxy  
 *
 * 1. basic check of message format 
 * 2. register proxy in worker's proxy db. 
 *    It shouldn't fail but may if there is a version mismatch, for example 
 * 3. send our latest payment data. 
 *    It's important to compare it with proxy to mitigate (some) proxy rollback atacks 
 *
 */

void WorkerProxyConnection::received_handshake_answer(td::BufferSlice answer) {
  block::StdAddress proxy_owner_address, proxy_sc_address, worker_sc_address;
  td::Bits256 proxy_public_key;
  auto S = [&]() -> td::Status {
    TRY_RESULT(r, fetch_tl_object<cocoon_api::worker_connectedToProxy>(answer, true));
    if (!(r->params_->flags_ & 3)) {
      return td::Status::Error(ton::ErrorCode::error, "proxy is too old");
    }
    if (r->params_->is_test_ != runner()->is_test()) {
      return td::Status::Error(ton::ErrorCode::error, "test mode mismatch");
    }
    TRY_RESULT_ASSIGN(proxy_owner_address, block::StdAddress::parse(r->params_->proxy_owner_address_));
    TRY_RESULT_ASSIGN(proxy_sc_address, block::StdAddress::parse(r->params_->proxy_sc_address_));
    TRY_RESULT_ASSIGN(worker_sc_address, block::StdAddress::parse(r->worker_sc_address_));
    proxy_public_key = r->params_->proxy_public_key_;
    proto_version_ = r->params_->proto_version_;
    return td::Status::OK();
  }();
  if (S.is_error()) {
    fail_connection(S.move_as_error_prefix("received bad handshake answer: "));
    return;
  }
  proxy_sc_address_str_ = proxy_sc_address.rserialize(true);
  auto R = runner()->register_proxy(connection_id(), proxy_public_key, proxy_owner_address, proxy_sc_address,
                                    worker_sc_address, nullptr);
  if (R.is_error()) {
    fail_connection(R.move_as_error_prefix("cannot register proxy: "));
    return;
  }

  auto P = R.move_as_ok();
  auto req = cocoon::create_serialize_tl_object<cocoon_api::worker_compareBalanceWithProxy>(
      P->earned_tokens_committed_to_blockchain(), P->earned_tokens_committed_to_proxy_db(),
      P->earned_tokens_max_known());
  runner()->send_handshake_query_to_connection(
      connection_id(), "connect", std::move(req), td::Timestamp::in(30.0),
      [runner = actor_id(runner()), runner_ptr = runner(), self = this,
       connection_id = connection_id()](td::Result<td::BufferSlice> R) mutable {
        td::actor::send_lambda(runner, [self, runner_ptr, connection_id, R = std::move(R)]() mutable {
          if (R.is_ok()) {
            self->received_compare_answer(R.move_as_ok());
          } else {
            runner_ptr->fail_connection(connection_id, R.move_as_error());
          }
        });
      });
}

/*
 *
 * Receive proxy payment status
 *
 * 1. payment_info aka committed_to_blockchain. If proxy has a smaller number here
 *    it's virtual machine state has been tampered with. 
 * 2. committed_to_db. If proxy has a smaller number here, it means that 
 *    proxy's db was deleted or modified. We shouldn't work with this proxy
 *    unless we could receive some part of it's stake, but it has to be a proxy's decision 
 * 3. max_tokens. Theoretically proxy could crash and lose this infomation. In this case
 *    we should send information, that will help proxy restore this information. It shoud be
 *    list of transaction information like vector of pairs (tokens, client) for all transaction
 *    after proxy's max_tokens 
 *
 */

void WorkerProxyConnection::received_compare_answer(td::BufferSlice answer) {
  ton::tl_object_ptr<cocoon_api::proxy_SignedPayment> signed_payment;
  td::int64 tokens_committed_to_db, max_tokens;
  td::int32 error_code;
  auto S = [&]() -> td::Status {
    TRY_RESULT(r, fetch_tl_object<cocoon_api::worker_compareBalanceWithProxyResult>(answer, true));
    signed_payment = std::move(r->signed_payment_);
    tokens_committed_to_db = r->tokens_committed_to_db_;
    max_tokens = r->max_tokens_;
    error_code = r->error_code_;
    return td::Status::OK();
  }();
  if (S.is_error()) {
    fail_connection(S.move_as_error_prefix("cannot reconcile payment information: "));
    return;
  }

  auto P = runner()->get_proxy_info(proxy_sc_address_str_);
  if (!P) {
    fail_connection(td::Status::Error(ton::ErrorCode::timeout, "proxy already deleted"));
    return;
  }

  if (!P->update_payment_info(std::move(signed_payment))) {
    fail_connection(td::Status::Error(ton::ErrorCode::protoviolation, "tokens_committed_to_blockchain is too low"));
    return;
  }
  if (!P->update_tokens_committed_to_proxy_db(tokens_committed_to_db)) {
    fail_connection(td::Status::Error(ton::ErrorCode::protoviolation, "tokens_committed_to_db is too low"));
    return;
  }

  P->update_tokens_max_known(max_tokens);

  if (!error_code) {
    CHECK(max_tokens == P->earned_tokens_max_known());
    send_handshake_complete();
    return;
  }

  auto req = cocoon::create_serialize_tl_object<cocoon_api::worker_extendedCompareBalanceWithProxy>(
      P->earned_tokens_committed_to_proxy_db(), P->export_difference_with_db(max_tokens));
  runner()->send_handshake_query_to_connection(
      connection_id(), "connect", std::move(req), td::Timestamp::in(30.0),
      [runner = actor_id(runner()), runner_ptr = runner(), self = this,
       connection_id = connection_id()](td::Result<td::BufferSlice> R) mutable {
        td::actor::send_lambda(runner, [self, runner_ptr, connection_id, R = std::move(R)]() mutable {
          if (R.is_ok()) {
            self->received_extended_compare_answer(R.move_as_ok());
          } else {
            runner_ptr->fail_connection(connection_id, R.move_as_error());
          }
        });
      });
}

/*
 *
 * Receive proxy's updated payment status
 * either proxy could restore state or not
 *
 */

void WorkerProxyConnection::received_extended_compare_answer(td::BufferSlice answer) {
  td::int32 error_code;
  auto S = [&]() -> td::Status {
    TRY_RESULT(r, fetch_tl_object<cocoon_api::worker_extendedCompareBalanceWithProxyResult>(answer, true));
    error_code = r->error_code_;
    return td::Status::OK();
  }();
  if (S.is_error()) {
    fail_connection(S.move_as_error_prefix("cannot reconcile payment information: "));
    return;
  }

  auto P = runner()->get_proxy_info(proxy_sc_address_str_);
  if (!P) {
    fail_connection(td::Status::Error(ton::ErrorCode::timeout, "proxy already deleted"));
    return;
  }

  if (!error_code) {
    send_handshake_complete();
    return;
  }

  fail_connection(td::Status::Error(
      ton::ErrorCode::error, PSTRING() << "cannot reconcile payment information: received error " << error_code));
}

/*
 *
 * Success 
 *
 */

void WorkerProxyConnection::send_handshake_complete() {
  auto req = create_serialize_tl_object<cocoon_api::worker_proxyHandshakeComplete>(runner()->is_disabled());
  runner()->send_handshake_query_to_connection(
      connection_id(), "connect", std::move(req), td::Timestamp::in(30.0),
      [runner = actor_id(runner()), runner_ptr = runner(), self = this,
       connection_id = connection_id()](td::Result<td::BufferSlice> R) mutable {
        td::actor::send_lambda(runner, [self, runner_ptr, connection_id, R = std::move(R)]() mutable {
          if (R.is_ok()) {
            self->received_handshake_complete_answer(R.move_as_ok());
          } else {
            runner_ptr->fail_connection(connection_id, R.move_as_error());
          }
        });
      });
}

/*
 *
 * Success 
 *
 */

void WorkerProxyConnection::received_handshake_complete_answer(td::BufferSlice answer) {
  handshake_completed();
}

}  // namespace cocoon
