#!/usr/bin/env python3
"""Bundled dev-only backend. No GPU, model weights, or tensor transport."""

import argparse
import http.server
import json
import os
import select
import signal
import socket
import socketserver
import subprocess
import sys
import struct
import threading
import time
from pathlib import Path


class State:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.active = 0
        self.prompt_tokens_in_flight = 0
        self.completed = 0
        self.cancelled = 0
        self.warmed = False
        self.slots = threading.BoundedSemaphore(args.max_num_seqs)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, state, path, handler):
        self.state = state
        self.connections = threading.BoundedSemaphore(16)
        super().__init__(path, handler)
        os.chmod(path, 0o600)

    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connections.release()


class HealthHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True

    def connected_wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            readable, _, _ = select.select([self.connection], [], [], max(0, min(0.02, end - time.monotonic())))
            if readable and self.connection.recv(1, socket.MSG_PEEK) == b'':
                raise ConnectionAbortedError('request cancelled by disconnect')

    def do_GET(self):
        state = self.server.state
        try:
            if self.path == '/health':
                if state.warmed and state.args.scenario == 'health-hang':
                    self.connected_wait(30)
                with state.lock:
                    body = {'status': 'ok', 'security_mode': 'dev', 'rank': state.args.rank,
                            'config_digest': state.args.config_digest, 'active_requests': state.active,
                            'prompt_tokens_in_flight': state.prompt_tokens_in_flight,
                            'completed_requests': state.completed, 'cancelled_requests': state.cancelled,
                            'warmup_complete': state.warmed}
                self.reply(200, body)
            else:
                self.reply(404, {'error': 'unknown endpoint'})
        except (OSError, TimeoutError):
            self.close_connection = True

    def do_POST(self):
        try:
            self.reply(404, {'error': 'unknown endpoint'})
        except (OSError, TimeoutError):
            self.close_connection = True


class Handler(HealthHandler):
    def do_GET(self):
        if self.path != '/v1/models':
            return super().do_GET()
        try:
            self.reply(200, {'object': 'list', 'data': [{'id': self.server.state.args.model, 'object': 'model'}]})
        except (OSError, TimeoutError):
            self.close_connection = True

    def do_POST(self):
        state = self.server.state
        acquired = False
        reserved_tokens = 0
        try:
            if self.path != '/v1/chat/completions':
                self.reply(404, {'error': 'unknown endpoint'})
                return
            length = int(self.headers.get('Content-Length', '-1'))
            if self.headers.get('Transfer-Encoding') or not 0 < length <= 8192:
                self.reply(400, {'error': 'Content-Length must be within 1..8192'})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ConnectionAbortedError('incomplete request')
            request = json.loads(raw)
            allowed = {'model', 'messages', 'max_tokens', 'stream', 'stream_options', 'temperature',
                       'seed', 'simulator'}
            if not isinstance(request, dict) or set(request) - allowed:
                raise ValueError('unsupported request fields')
            if request.get('model') != state.args.model:
                raise ValueError('unsupported model')
            messages = request.get('messages')
            if not isinstance(messages, list) or not messages:
                raise ValueError('messages must be a nonempty list')
            for msg in messages:
                if (not isinstance(msg, dict) or set(msg) != {'role', 'content'} or
                        msg['role'] not in ('user', 'system', 'assistant') or not isinstance(msg['content'], str)):
                    raise ValueError('unsupported message')
            count = request.get('max_tokens', 2)
            if type(count) is not int or not 1 <= count <= 64:
                raise ValueError('max_tokens must be within 1..64')
            stream = request.get('stream', False)
            if type(stream) is not bool:
                raise ValueError('stream must be boolean')
            prompt_tokens = sum(len(msg['content'].split()) for msg in messages)
            if prompt_tokens + count > state.args.max_model_len:
                raise ValueError('context limit exceeded')
            sim = request.get('simulator', {})
            if not isinstance(sim, dict) or set(sim) - {'fault', 'token_delay_ms'}:
                raise ValueError('unsupported simulator request options')
            fault = sim.get('fault', 'none')
            delay = sim.get('token_delay_ms', 10)
            if fault not in ('none', 'truncate', 'hang', 'http-error', 'error-event'):
                raise ValueError('unsupported fault')
            if type(delay) is not int or not 0 <= delay <= 1000:
                raise ValueError('invalid token delay')
            acquired = state.slots.acquire(blocking=False)
            if not acquired:
                self.reply(429, {'error': 'simulator capacity exceeded'})
                return
            with state.lock:
                state.active += 1
                over_budget = state.prompt_tokens_in_flight + prompt_tokens > state.args.max_num_batched_tokens
                if not over_budget:
                    reserved_tokens = prompt_tokens
                    state.prompt_tokens_in_flight += reserved_tokens
            if over_budget:
                self.reply(429, {'error': 'simulator token budget exceeded'})
                return
            warmup = messages == [{'role': 'user', 'content': 'pipeline warmup'}]
            if warmup:
                self.connected_wait(state.args.warmup_delay_ms / 1000)
                if state.args.scenario == 'warmup-hang':
                    self.connected_wait(30)
                if state.args.scenario == 'warmup-error':
                    self.reply(500, {'error': 'injected warmup failure'})
                    return
            if fault == 'hang':
                self.connected_wait(30)
            if fault == 'http-error':
                self.reply(503, {'error': 'injected backend failure'})
                return
            words = ['simulated' if i % 2 == 0 else 'reply' for i in range(count)]
            usage = {'prompt_tokens': prompt_tokens, 'completion_tokens': count, 'total_tokens': prompt_tokens + count}
            base = {'id': 'simulator-request', 'model': state.args.model}
            if stream:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True

                def event(value):
                    data = value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))
                    self.wfile.write(('data: ' + data + '\n\n').encode())
                    self.wfile.flush()

                for index, word in enumerate(words):
                    self.connected_wait(delay / 1000)
                    event({**base, 'object': 'chat.completion.chunk', 'choices': [
                        {'index': 0, 'delta': {'content': ('' if index == 0 else ' ') + word}, 'finish_reason': None}]})
                    if fault == 'truncate':
                        return
                    if fault == 'error-event':
                        event({'error': {'message': 'injected stream failure', 'type': 'InternalServerError'}})
                        event('[DONE]')
                        return
                event({**base, 'object': 'chat.completion.chunk', 'choices': [
                    {'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage})
                event('[DONE]')
            else:
                self.connected_wait(count * delay / 1000)
                body = {**base, 'object': 'chat.completion', 'choices': [
                    {'index': 0, 'message': {'role': 'assistant', 'content': ' '.join(words)}, 'finish_reason': 'stop'}],
                        'usage': usage}
                if fault == 'truncate':
                    data = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[:len(data) // 2])
                    self.close_connection = True
                    return
                self.reply(200, body)
            with state.lock:
                state.completed += 1
                if warmup:
                    state.warmed = True
            if warmup and state.args.scenario == 'crash-after-ready':
                threading.Timer(0.6, lambda: os._exit(23)).start()
        except (ValueError, TypeError, KeyError) as error:
            try:
                self.reply(400, {'error': str(error)})
            except OSError:
                pass
        except (OSError, TimeoutError):
            if acquired:
                with state.lock:
                    state.cancelled += 1
        finally:
            self.close_connection = True
            if acquired:
                with state.lock:
                    state.active -= 1
                    state.prompt_tokens_in_flight -= reserved_tokens
                state.slots.release()


class NetworkEcho(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Bounded synthetic engine traffic for namespace/WireGuard tests only."""
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, ip):
        self.connections = threading.BoundedSemaphore(8)
        super().__init__((ip, 29999), EchoHandler)

    def process_request(self, request, address):
        if not self.connections.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.connections.release()


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        def exact(count):
            data = b''
            while len(data) < count:
                part = self.request.recv(count - len(data))
                if not part: raise EOFError()
                data += part
            return data
        try:
            size = struct.unpack('!I', exact(4))[0]
            if 0 < size <= 65536:
                body = exact(size)
                self.request.sendall(struct.pack('!I', size) + body)
        except (OSError, EOFError):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--health-socket', required=True)
    parser.add_argument('--rank', type=int, required=True)
    parser.add_argument('--config-digest', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--max-model-len', type=int, required=True)
    parser.add_argument('--max-num-seqs', type=int, required=True)
    parser.add_argument('--max-num-batched-tokens', type=int, required=True)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--startup-delay-ms', type=int, required=True)
    parser.add_argument('--warmup-delay-ms', type=int, required=True)
    parser.add_argument('--overlay-ip', choices=('10.231.0.1', '10.231.0.2'))
    args = parser.parse_args()
    os.umask(0o077)
    if args.scenario == 'startup-exit':
        return 23
    if args.scenario == 'stubborn-child':
        # Deliberately leave a TERM-resistant descendant to exercise whole-group
        # cleanup even after the direct backend process has exited.
        child_code = ('import os,signal,time,pathlib,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                      'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); '
                      'time.sleep(300)')
        subprocess.Popen([sys.executable, '-I', '-c', child_code, str(Path(args.socket).parent / 'child.pid')])
    time.sleep(args.startup_delay_ms / 1000)
    if args.scenario == 'startup-hang':
        time.sleep(30)
    state = State(args)
    network = NetworkEcho(args.overlay_ip) if args.overlay_ip else None
    if network:
        threading.Thread(target=network.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    with Server(state, args.socket, Handler) as server, Server(state, args.health_socket, HealthHandler) as health:
        # Separate accept loops and connection budgets keep probes responsive
        # when API clients occupy every handler while sending partial requests.
        health_thread = threading.Thread(target=health.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        health_thread.start()
        print(json.dumps({'event': 'listening', 'rank': args.rank, 'pid': os.getpid(), 'security_mode': 'dev'}), flush=True)
        try:
            server.serve_forever(poll_interval=0.05)
        finally:
            health.shutdown()
            health_thread.join()
            if network:
                network.shutdown()
                network.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
